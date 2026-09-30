"""Entry point: run the web dashboard (§7.15 P3–P4, §7.78).

A standalone FastAPI + Jinja2/HTMX server that opens **every book** it finds in
``storage.data_dir`` (one SQLite file per agent × mode, WAL mode → safe to read while
the agents write) and writes control latches directly into the selected book, so it
works with or without the agent-side ``control_api``. Launch it alongside the agents:

    python -m scripts.run_dashboard          # http://127.0.0.1:8080 (config-driven)

It never places orders, never touches credentials, and only edits the safe config
whitelist.
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

    from src.dashboard.books import open_books

    host = host or settings.dashboard.host
    port = port if port is not None else settings.dashboard.port

    books = await open_books(settings)

    # Late import keeps the uvicorn dependency scoped to actually serving.
    import uvicorn

    app = create_dashboard_app(settings=settings, books=books)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    log.info("dashboard serving", host=host, port=port, books=[b.key for b in books])
    try:
        await task
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
        pass
    finally:
        server.should_exit = True
        for book in books:
            await book.storage.close()
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
