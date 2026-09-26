"""Per-venue trading cost model (§7.65).

One shared implementation of commission/FX arithmetic used by the paper
executor's fills, the sizing cash-clamp (:func:`buy_cost_factor` /
:func:`calculate_quantity`) and the decision-replay backtester, so paper PnL
and the §4.3 live-readiness gates reflect the *venue being simulated* rather
than one Kraken-era percentage for everything.

A venue's cost schedule has three parts:

``fee_pct``
    Per-side commission as a fraction of the fill notional (e.g. 0.001 = 0.10%,
    OKX EU spot base-tier taker from 2026-09-25).

``min_commission``
    Absolute per-side floor in the settlement currency booked by the executor
    (Saxo charges 0.08% **min $1** per US-stock trade — ~1%/side on a €100
    position, invisible to a percentage-only model). When it differs from the
    account's currency the broker converts it; the paper book is
    single-currency, so configure the value in the book's own currency.

``fx_fee_pct``
    Applied when trades settle in a currency other than the account's (Saxo:
    0.25% per EUR↔USD conversion). It lives on the *profile* chosen for those
    symbols — there is no half-built currency inference in the executor.
"""

from __future__ import annotations


class CostModel:
    """Immutable commission schedule. Pure computation, no I/O."""

    def __init__(
        self,
        *,
        slippage_pct: float = 0.0,
        fee_pct: float = 0.0,
        min_commission: float = 0.0,
        fx_fee_pct: float = 0.0,
    ) -> None:
        for name, value in (
            ("slippage_pct", slippage_pct),
            ("fee_pct", fee_pct),
            ("min_commission", min_commission),
            ("fx_fee_pct", fx_fee_pct),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValueError(f"CostModel.{name} must be a non-negative number")
        self.slippage_pct = float(slippage_pct)
        self.fee_pct = float(fee_pct)
        self.min_commission = float(min_commission)
        self.fx_fee_pct = float(fx_fee_pct)

    @classmethod
    def from_attrs(cls, obj: object) -> CostModel:
        """Build from an executor's live attributes (mocks/venues without them → zeros).

        Reads ``slippage_pct`` / ``fee_pct`` / ``min_commission`` / ``fx_fee_pct``;
        non-numeric attributes (MagicMock executors) count as zero, matching the
        old :func:`buy_cost_factor` behaviour. Re-read per use, so safe-config
        overrides (§7.50) that mutate executor attributes take effect immediately.
        """
        params: dict[str, float] = {}
        for name in ("slippage_pct", "fee_pct", "min_commission", "fx_fee_pct"):
            value = getattr(obj, name, 0.0)
            params[name] = (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else 0.0
            )
        return cls(**params)

    @property
    def is_zero(self) -> bool:
        return self.fee_pct <= 0 and self.min_commission <= 0 and self.fx_fee_pct <= 0

    def commission(self, notional: float) -> float:
        """Per-side commission for a fill of *notional*: percentage floored by the minimum."""
        if notional <= 0:
            return 0.0
        pct = notional * self.fee_pct
        return max(pct, self.min_commission) if self.min_commission > 0 else pct

    def total_fee(self, notional: float) -> float:
        """All fees for one side: commission + FX conversion cost."""
        if notional <= 0:
            return 0.0
        return self.commission(notional) + notional * self.fx_fee_pct

    @property
    def buy_cost_factor(self) -> float:
        """Multiplicative all-in factor of a BUY vs its quoted price (§7.59 L1).

        ``(1 + slippage) × (1 + fee + fx)`` — the legacy shape used when no
        minimum commission is configured; with one, use
        :meth:`max_affordable_fill_notional` instead (a floor is not linear in
        quantity, so it cannot fold into a factor).
        """
        return (1.0 + self.slippage_pct) * (1.0 + self.fee_pct + self.fx_fee_pct)

    def max_affordable_fill_notional(self, cash: float) -> float:
        """Largest fill notional ``N`` with ``N + total_fee(N) <= cash``.

        The commission is piecewise (floored below ``min_commission / fee_pct``),
        continuous and increasing in ``N``, so the inverse has a closed form:
        above the floor regime ``cash / (1 + fee + fx)``; inside it the percentage
        is *replaced* by the flat minimum, giving ``(cash - min_commission) /
        (1 + fx)`` — never negative.
        """
        if cash <= 0:
            return 0.0
        combined = self.fee_pct + self.fx_fee_pct
        if self.min_commission <= 0:
            return cash / (1.0 + combined)
        if self.fee_pct > 0:
            above_floor = cash / (1.0 + combined)
            if above_floor * self.fee_pct >= self.min_commission:
                return above_floor
        # Floor regime (also the fee_pct == 0 case, where the minimum always
        # binds): commission is the flat minimum, not fee_pct × N.
        return max(cash - self.min_commission, 0.0) / (1.0 + self.fx_fee_pct)


def base_currency(symbol: str) -> str:
    """Base half of a ``BASE/QUOTE`` pair (``"BTC/EUR"`` → ``"BTC"``); whole string otherwise."""
    return symbol.split("/", 1)[0].strip().upper() if symbol else ""
