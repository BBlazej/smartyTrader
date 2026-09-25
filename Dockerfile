# Single slim image for all four services (agent-crypto, agent-stocks, dashboard,
# backtester) — PLAN §7.15 P5; topology in ARCHITECTURE.md. No Node build step:
# the dashboard is server-rendered Jinja2/HTMX with uPlot via CDN.
#
# The compose file sets each service's command; a bare `docker run` defaults to
# the crypto agent (paper by default — no EXCHANGE_API_KEY → PaperExecutor).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# .dockerignore keeps secrets (.env), runtime state (data/) and dev artifacts out.
COPY . .

# Install the package itself + the stocks extra (yfinance); runtime deps come along.
RUN pip install --no-cache-dir ".[stocks]"

# Unprivileged runtime user; /app/data is the shared agent-data volume mount point
# (named volumes inherit this ownership, so the agents can write the SQLite WAL).
RUN useradd --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "scripts.run_crypto_agent"]
