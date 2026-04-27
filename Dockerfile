FROM python:3.11-slim AS base

# ── Metadata ──────────────────────────────────────────────────────────
LABEL maintainer="phoenix@example.com"
LABEL description="Phoenix Agent - A lightweight, extensible AI agent framework"

# ── Environment ───────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PHOENIX_HOME=/app/.phoenix

WORKDIR /app

# ── System deps (minimal) ────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# ── Dependencies (install first for Docker layer caching) ─────────────
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel && \
    pip install -e ".[all]"

# ── Application code ─────────────────────────────────────────────────
COPY src/ ./src/
COPY config.example.yaml ./

# ── Runtime config volume ────────────────────────────────────────────
# Users should mount their config at /app/.phoenix/config.yaml
RUN mkdir -p /app/.phoenix /app/.phoenix/downloads /app/.phoenix/history
VOLUME ["/app/.phoenix"]

# ── Health check ──────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from phoenix_agent import Agent; print('ok')" || exit 1

# ── Default command ───────────────────────────────────────────────────
EXPOSE 8080
ENTRYPOINT ["python", "-m", "phoenix_agent"]
CMD ["serve"]
