"""Standalone web dashboard for the trading agents (§7.15 P3–P4).

FastAPI + Jinja2/HTMX, server-rendered with uPlot charts via CDN (no Node build).
It reads the shared SQLite DB as a *reader* (WAL mode lets it read while the agents
write) and writes control latches directly through :class:`~src.core.storage.Storage`
— the same writes the agent-side control API makes — so pause/resume/close-all work
whether or not ``control_api.enabled``. Credentials are structurally absent from every
page; config edits go through the :class:`~src.core.control_config.SafeConfigOverrides`
whitelist only.
"""

from __future__ import annotations

from .app import create_dashboard_app

__all__ = ["create_dashboard_app"]
