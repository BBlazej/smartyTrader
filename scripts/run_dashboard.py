"""Entry point: run the web dashboard (§7.15 P3–P4).

A standalone FastAPI + Jinja2/HTMX server that reads the shared SQLite DB (WAL mode →
safe to read while the agents write) and writes control latches directly, so it works
with or without the agent-side ``control_api``. Launch it alongside the agents:

    python -m scripts.run_dashboard          # http://127.0.0.1:8080 (config-driven)

It never places orders, never touches credentials, and only edits the safe config
whitelist. The DB path comes from ``config/settings.yaml`` — the same file the agents
use — so point it at any environment's database by overriding that path.
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from src.core.config import Settings
from src.core.runner import load_dotenv
from src.dashboard import create_dashboard_app
from src.monitoring import setup_logging


async def run(host: str | None, port: int | None) -> None:
    load_dotenv()
    settings = Settings()
    setup_logging(settings.monitoring.log_level)
    log = structlog.get_logger().bind(component="dashboard")

    from src.core.storage import Storage

    host = host or settings.dashboard.host
    port = port if port is not None else settings.dashboard.port

    storage = Storage(settings.storage.database_path)
    await storage.initialize()

    # Late import keeps the uvicorn dependency scoped to actually serving.
    import uvicorn

    app = create_dashboard_app(storage=storage, settings=settings)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    log.info("dashboard serving", host=host, port=port, db=settings.storage.database_path)
    try:
        await task
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
        pass
    finally:
        server.should_exit = True
        await storage.close()
        log.info("dashboard shut down cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the trading-agent web dashboard.")
    parser.add_argument("--host", default=None, help="Bind host (default: config dashboard.host)")
    parser.add_argument(
        "--port", type=int, default=None, help="Bind port (default: config dashboard.port)"
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
