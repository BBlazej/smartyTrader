"""Unit tests for the dashboard's opt-in process supervision (§7.24).

Real subprocesses are used but they're harmless sleeps (short by design so a
missed stop can never outlive the test session meaningfully). The runner-command
builder is injected, so nothing here ever spawns a trading agent.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from src.dashboard.launch import AgentLauncher


def _sleep_builder(seconds: float = 2.0):
    def build(agent: str) -> list[str]:
        # Extra argv keeps the cmdline marker testable for adoption matching.
        return [sys.executable, "-c", f"import time; time.sleep({seconds})", "sleep-only"]

    return build


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class TestStartStop:
    async def test_start_then_stop_lifecycle(self, tmp_path) -> None:
        launcher = AgentLauncher(tmp_path, command_builder=_sleep_builder())
        pid = await launcher.start("paper_crypto")
        assert _alive(pid)
        assert launcher.managed_pid("paper_crypto") == pid
        assert int((tmp_path / "paper_crypto_agent.pid").read_text()) == pid

        assert await launcher.stop("paper_crypto") is True
        assert launcher.managed_pid("paper_crypto") is None
        assert not (tmp_path / "paper_crypto_agent.pid").exists()
        assert not _alive(pid)

    async def test_double_start_refused(self, tmp_path) -> None:
        launcher = AgentLauncher(tmp_path, command_builder=_sleep_builder())
        await launcher.start("paper_crypto")
        with pytest.raises(RuntimeError):
            await launcher.start("paper_crypto")
        await launcher.stop("paper_crypto")

    async def test_stop_without_child_is_false(self, tmp_path) -> None:
        launcher = AgentLauncher(tmp_path, command_builder=_sleep_builder())
        assert await launcher.stop("paper_crypto") is False

    async def test_exited_child_forgotten(self, tmp_path) -> None:
        launcher = AgentLauncher(tmp_path, command_builder=_sleep_builder(0.1))
        pid = await launcher.start("paper_crypto")
        while _alive(pid):  # wait for the short sleep to end on its own
            await asyncio.sleep(0.05)
        assert launcher.managed_pid("paper_crypto") is None


class TestAdoption:
    async def test_live_matching_pid_is_adopted(self, tmp_path) -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
            "scripts.run_crypto_agent",  # markers the launcher verifies via /proc cmdline
            "--mode",
            "paper",
        )
        (tmp_path / "paper_crypto_agent.pid").write_text(str(child.pid))
        launcher = AgentLauncher(tmp_path)
        assert launcher.managed_pid("paper_crypto") == child.pid
        try:
            assert await launcher.stop("paper_crypto") is True  # SIGTERM path, no handle
            assert not _alive(child.pid)
        finally:
            if child.returncode is None:  # pragma: no cover - only if stop failed
                child.kill()

    async def test_foreign_pid_never_adopted_or_killed(self, tmp_path) -> None:
        # Live process whose cmdline does NOT match the runner → pidfile must be
        # pruned and the foreign process left untouched (no kill-by-pid guesswork).
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(2)", "totally-unrelated"
        )
        (tmp_path / "paper_crypto_agent.pid").write_text(str(child.pid))
        launcher = AgentLauncher(tmp_path)
        try:
            assert launcher.managed_pid("paper_crypto") is None
            assert not (tmp_path / "paper_crypto_agent.pid").exists()  # stale file pruned
            assert await launcher.stop("paper_crypto") is False
            assert _alive(child.pid)
        finally:
            child.terminate()
            await child.wait()

    async def test_dead_pid_is_not_adopted(self, tmp_path) -> None:
        (tmp_path / "paper_crypto_agent.pid").write_text("99999999")  # certainly not ours
        launcher = AgentLauncher(tmp_path)
        assert launcher.managed_pid("paper_crypto") is None
        assert not (tmp_path / "paper_crypto_agent.pid").exists()


class TestBookKeys:
    """§7.78: compound ``<mode>_<agent>`` keys spawn/adopt mode-flagged runners."""

    def test_default_command_adds_mode_flag(self) -> None:
        with pytest.raises(ValueError, match="book key"):
            AgentLauncher.default_command("crypto")  # bare agent names are not books
        demo = AgentLauncher.default_command("demo_crypto")
        assert demo[1:3] == ["-m", "scripts.run_crypto_agent"]
        assert demo[3:] == ["--mode", "demo"]

    async def test_mode_key_never_adopts_wrong_mode_runner(self, tmp_path) -> None:
        # A paper runner must not be adopted as the demo book's process (§7.52 twin).
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
            "scripts.run_crypto_agent",
            "--mode",
            "paper",
        )
        (tmp_path / "demo_crypto_agent.pid").write_text(str(child.pid))
        launcher = AgentLauncher(tmp_path)
        try:
            assert launcher.managed_pid("demo_crypto") is None  # wrong --mode
            assert not (tmp_path / "demo_crypto_agent.pid").exists()
            assert _alive(child.pid)  # foreign process untouched
        finally:
            child.terminate()
            await child.wait()

    async def test_mode_key_adopts_matching_runner(self, tmp_path) -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
            "scripts.run_crypto_agent",
            "--mode",
            "demo",
        )
        (tmp_path / "demo_crypto_agent.pid").write_text(str(child.pid))
        launcher = AgentLauncher(tmp_path)
        try:
            assert launcher.managed_pid("demo_crypto") == child.pid
            assert await launcher.stop("demo_crypto") is True
            assert not _alive(child.pid)
        finally:
            if child.returncode is None:  # pragma: no cover - only if stop failed
                child.kill()
