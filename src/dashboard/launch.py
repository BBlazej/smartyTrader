"""Optional process supervision for the dashboard (§7.24).

Lets the dashboard *start* and *stop* local agent runner processes
(``python -m scripts.run_<agent>_agent``) so the whole system can be driven from
the browser on a single host. Safety posture (§7.15 heritage):

* **Opt-in wholesale** — wired only when ``dashboard.allow_launch`` is true
  (default false). The dashboard otherwise stays a pure latch-writer.
* The launcher never places orders and never touches credentials; it spawns the
  *same entry points you would run by hand*, so all enabled-gates, risk rules
  and paper-by-default execution apply unchanged to launched agents.
* **Stop only ever targets processes this dashboard launched or adopted.** A
  running agent started elsewhere (terminal, systemd, another container) can be
  paused via its latch but is never killed by pid guesswork.

Keys are *book* names (§7.78): one per agent × mode, e.g. ``demo_crypto`` — the runner
is spawned with ``--mode demo``, so the dashboard can start paper and demo side by side.
Adoption across dashboard restarts uses a per-book pidfile in the data dir; a
pid is only adopted when it is alive *and* its ``/proc`` cmdline still matches
the expected runner module (and ``--mode`` flag), so a recycled pid is never killed.
Child stdout / stderr are appended to ``data/agent_<key>.out.log``. Children
deliberately *outlive* the dashboard (killing the dashboard must not halt trading);
the pidfile lets a restarted dashboard re-adopt them.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import structlog

from ..core.db_layout import parse_book_key

logger = structlog.get_logger(__name__)


class AgentLauncher:
    """Launches/stops local agent runner processes; tracks its own children."""

    def __init__(
        self,
        data_dir: str | Path,
        command_builder: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._build_command = command_builder or self.default_command
        self._children: dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    def default_command(key: str) -> list[str]:
        """Runner-module invocation for a book key, mirroring the documented CLI (§7.78)."""
        parsed = parse_book_key(key)
        if parsed is None:
            raise ValueError(f"{key!r} is not a <mode>_<agent> book key")
        mode, agent = parsed
        return [sys.executable, "-m", f"scripts.run_{agent}_agent", "--mode", mode]

    # ── pidfiles ────────────────────────────────────────────────

    def _pid_path(self, agent: str) -> Path:
        return self._data_dir / f"{agent}_agent.pid"

    def _log_path(self, agent: str) -> Path:
        return self._data_dir / f"agent_{agent}.out.log"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _cmdline_matches(self, pid: int, key: str) -> bool:
        """Verify a foreign/adopted pid really is this book's runner (Linux)."""
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        argv = raw.decode(errors="replace").split("\x00")
        parsed = parse_book_key(key)
        if parsed is None:
            return False
        mode, agent = parsed
        if not any(f"scripts.run_{agent}_agent" in arg for arg in argv):
            return False
        # A pid may only be adopted as the runner started with this book's --mode (§7.78).
        return any(arg == "--mode" and argv[i + 1 : i + 2] == [mode] for i, arg in enumerate(argv))

    def _adopted_pid(self, agent: str) -> int | None:
        """A live pid from the pidfile whose cmdline matches; prunes stale files."""
        path = self._pid_path(agent)
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return None
        if self._pid_alive(pid) and self._cmdline_matches(pid, agent):
            return pid
        try:  # stale or recycled — never keep a pidfile that can mislead stop()
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - filesystem hiccup stays fail-soft
            pass
        return None

    def _forget(self, agent: str) -> None:
        self._children.pop(agent, None)
        try:
            self._pid_path(agent).unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass

    # ── public API ──────────────────────────────────────────────

    def managed_pid(self, agent: str) -> int | None:
        """Pid of the process this dashboard launched *or adopted*, else ``None``."""
        child = self._children.get(agent)
        if child is not None and child.returncode is None:
            return child.pid
        if child is not None:  # exited since we last looked
            self._forget(agent)
        return self._adopted_pid(agent)

    async def start(self, agent: str) -> int:
        """Spawn the runner for ``agent``; returns its pid. Raises on double-start."""
        if self.managed_pid(agent) is not None:
            raise RuntimeError(f"{agent} agent is already under management (pid)")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        command = self._build_command(agent)
        # The child gets its own dup'd fd at spawn, so the parent handle only needs
        # to stay open until create_subprocess_exec returns — a plain context manager.
        with open(self._log_path(agent), "ab") as log_file:  # noqa: ASYNC230 (one tiny open)
            child = await asyncio.create_subprocess_exec(
                *command, stdout=log_file, stderr=subprocess.STDOUT
            )
        self._children[agent] = child
        self._pid_path(agent).write_text(str(child.pid))
        logger.info("dashboard launched agent", agent=agent, pid=child.pid, command=command)
        return child.pid

    async def stop(self, agent: str) -> bool:
        """Terminate the managed process. Returns ``False`` when nothing is managed."""
        pid = self.managed_pid(agent)
        if pid is None:
            return False
        child = self._children.get(agent)
        try:
            if child is not None and child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), timeout=10.0)
                except TimeoutError:  # pragma: no cover - runner always exits on SIGTERM
                    child.kill()
            else:  # adopted (no Process handle in this dashboard's lifetime)
                os.kill(pid, 15)  # SIGTERM
                for _ in range(100):  # poll up to ~10s for graceful exit
                    if not self._pid_alive(pid):
                        break
                    await asyncio.sleep(0.1)
                else:  # pragma: no cover - defensive against wedged runners
                    if self._cmdline_matches(pid, agent):
                        os.kill(pid, 9)
        except ProcessLookupError:  # already gone — fine
            pass
        self._forget(agent)
        logger.info("dashboard stopped agent", agent=agent, pid=pid)
        return True
