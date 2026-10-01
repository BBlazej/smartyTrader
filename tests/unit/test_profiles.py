"""Settings profiles (``--profile``): an overlay for testing the order path, never real money."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.analysis.prompt_builder import DEFAULT_SYSTEM_PROMPT, system_prompt_for
from src.core.config import AgentConfig, Settings
from src.core.runner import ModeMismatch, run_agent
from tests.helpers import StubAgent, make_settings, run_agent_once, runner_patches


def with_profile(tmp_path: Path, name: str, overlay: dict) -> Settings:
    make_settings(tmp_path)  # writes tmp_path/settings.yaml
    (tmp_path / "profiles").mkdir(exist_ok=True)
    (tmp_path / "profiles" / f"{name}.yaml").write_text(yaml.safe_dump(overlay))
    return Settings(str(tmp_path / "settings.yaml"), profile=name)


class TestOverlay:
    def test_deep_merges_over_the_base(self, tmp_path: Path) -> None:
        settings = with_profile(
            tmp_path,
            "t",
            {"risk": {"min_confidence": 0.3}, "crypto_agent": {"playbook": "test"}},
        )
        assert settings.profile == "t"
        assert settings.risk.min_confidence == 0.3
        assert settings.risk.max_position_pct == 0.1  # untouched base value kept
        assert settings.crypto_agent.playbook == "test"
        assert settings.crypto_agent.pairs == ["BTC/EUR"]
        assert settings.risk_baseline.min_confidence == 0.3  # tighten-only is relative to it

    def test_unknown_and_bad_names(self, tmp_path: Path) -> None:
        make_settings(tmp_path)
        with pytest.raises(ValueError, match="unknown settings profile"):
            Settings(str(tmp_path / "settings.yaml"), profile="nope")
        with pytest.raises(ValueError, match="profile name"):
            Settings(str(tmp_path / "settings.yaml"), profile="../etc")

    def test_shipped_test_profile(self) -> None:
        settings = Settings(profile="test")
        assert settings.crypto_agent.playbook == "test"
        assert settings.risk.event_guard_enabled is False
        assert settings.risk.enforce_exit_levels is True  # exits stay under test
        assert Settings().crypto_agent.playbook is None  # the base config is unaffected


class TestPlaybook:
    def test_test_playbook_overrides_the_hold_preference(self) -> None:
        prompt = system_prompt_for("test")
        assert prompt.startswith(DEFAULT_SYSTEM_PROMPT) and "TEST MODE" in prompt
        assert "OVERRIDES" in prompt

    def test_agent_playbook_validated(self) -> None:
        with pytest.raises(ValueError, match="playbook"):
            AgentConfig(enabled=True, playbook="yolo")


class TestRunner:
    async def test_main_pipeline_gets_playbook_and_tag(self, tmp_path: Path) -> None:
        settings = with_profile(tmp_path, "test", {"crypto_agent": {"playbook": "test"}})
        with runner_patches() as pipeline_cls:
            await run_agent_once(settings, StubAgent())
        kwargs = pipeline_cls.call_args.kwargs
        assert kwargs["system_prompt"] == system_prompt_for("test")
        assert kwargs["strategy"] == "profile_test"

    async def test_no_profile_no_tag(self, tmp_path: Path) -> None:
        with runner_patches() as pipeline_cls:
            await run_agent_once(make_settings(tmp_path), StubAgent())
        assert pipeline_cls.call_args.kwargs["strategy"] is None
        assert pipeline_cls.call_args.kwargs["system_prompt"] == DEFAULT_SYSTEM_PROMPT

    async def test_refused_on_a_real_account(self, tmp_path: Path) -> None:
        settings = with_profile(tmp_path, "test", {})
        executor = MagicMock(spec=["venue", "close"])
        executor.venue = "myokx-live"
        executor.close = AsyncMock()
        provider = MagicMock(spec=["close"])
        provider.close = AsyncMock()
        with pytest.raises(ModeMismatch, match="paper/demo testing only"):
            await run_agent(
                settings,
                component="crypto",
                agent_enabled=True,
                interval_minutes=5,
                decision_history_limit=10,
                job_id="crypto_cycle",
                build_components=lambda: (provider, executor),
                build_agent=lambda *a: StubAgent(),
                run_once=True,
            )
        executor.close.assert_awaited()
        assert not (tmp_path / "real_crypto.db").exists()
