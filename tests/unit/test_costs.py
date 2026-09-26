"""Unit tests for the per-venue cost model (§7.65)."""

from __future__ import annotations

import pytest

from src.core.costs import CostModel, base_currency


class TestCommission:
    def test_percentage_only(self) -> None:
        m = CostModel(fee_pct=0.001)
        assert m.commission(1_000.0) == pytest.approx(1.0)
        assert m.total_fee(1_000.0) == pytest.approx(1.0)

    def test_minimum_floor_binds_on_small_notional(self) -> None:
        # Saxo US stocks: 0.08% min $1 (§7.65) — a €100 trade pays 1.0, not 0.08.
        m = CostModel(fee_pct=0.0008, min_commission=1.0)
        assert m.commission(100.0) == pytest.approx(1.0)
        # Above the crossover (notional ≥ 1250) the percentage wins.
        assert m.commission(2_000.0) == pytest.approx(1.6)

    def test_fx_fee_added_on_top_of_commission(self) -> None:
        m = CostModel(fee_pct=0.0008, min_commission=1.0, fx_fee_pct=0.0025)
        # commission 1.0 (floor) + 0.25% FX on the notional.
        assert m.total_fee(100.0) == pytest.approx(1.0 + 0.25)

    def test_zero_or_negative_notional_pays_nothing(self) -> None:
        m = CostModel(fee_pct=0.01, min_commission=1.0)
        assert m.commission(0.0) == 0.0  # no fill → not even the minimum
        assert m.total_fee(-5.0) == 0.0

    def test_flat_minimum_venue(self) -> None:
        # fee_pct 0 with a minimum = pure flat per-side fee (minimum always binds).
        m = CostModel(fee_pct=0.0, min_commission=1.0)
        assert m.commission(10.0) == 1.0
        assert m.commission(100_000.0) == 1.0


class TestBuyCostFactor:
    def test_legacy_shape_without_fx_or_minimum(self) -> None:
        m = CostModel(slippage_pct=0.01, fee_pct=0.0026)
        assert m.buy_cost_factor == pytest.approx(1.01 * 1.0026)

    def test_fx_fee_folds_into_the_factor(self) -> None:
        m = CostModel(slippage_pct=0.001, fee_pct=0.0008, fx_fee_pct=0.0025)
        assert m.buy_cost_factor == pytest.approx(1.001 * (1 + 0.0008 + 0.0025))

    def test_from_attrs_ignores_non_numeric(self) -> None:
        class Mocky:
            fee_pct = "not-a-number"

        m = CostModel.from_attrs(Mocky())
        assert m.buy_cost_factor == 1.0
        assert m.is_zero


class TestMaxAffordableFillNotional:
    def test_matches_brute_force_across_regimes(self) -> None:
        # The closed form must equal the piecewise definition at every cash level,
        # including the crossover where the minimum stops binding.
        m = CostModel(fee_pct=0.0008, min_commission=1.0, fx_fee_pct=0.0025)
        for cash in (0.5, 1.0, 2.0, 137.0, 138.4, 200.0, 1_000.0, 999_999.0):
            n = m.max_affordable_fill_notional(cash)
            assert n + m.total_fee(n) <= cash + 1e-9
            # and N is maximal: a hair more does not fit
            assert n + m.total_fee(n * 1.000001 + 1e-9) > cash - 1e-6

    def test_percentage_only_matches_legacy_clamp(self) -> None:
        m = CostModel(fee_pct=0.01)
        assert m.max_affordable_fill_notional(1_010.0) == pytest.approx(1_000.0)

    def test_cash_below_minimum_affords_nothing_positive_beyond_floor(self) -> None:
        m = CostModel(fee_pct=0.0, min_commission=1.0)
        assert m.max_affordable_fill_notional(1.0) == 0.0
        assert m.max_affordable_fill_notional(0.0) == 0.0

    def test_negative_cash(self) -> None:
        assert CostModel(fee_pct=0.1).max_affordable_fill_notional(-5.0) == 0.0


class TestValidation:
    @pytest.mark.parametrize("field", ["fee_pct", "min_commission", "fx_fee_pct", "slippage_pct"])
    def test_negative_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            CostModel(**{field: -0.1})

    def test_boolean_rejected(self) -> None:
        with pytest.raises(ValueError):
            CostModel(fee_pct=True)


class TestBaseCurrency:
    def test_pairs(self) -> None:
        assert base_currency("BTC/EUR") == "BTC"
        assert base_currency("btc/eur") == "BTC"
        assert base_currency("AAPL") == "AAPL"
        assert base_currency("") == ""
