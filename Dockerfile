FROM python:3.11-slim AS base

# ── Metadata ──────────────────────────────────────────────────────────
LABEL maintainer="phoenix@example.com"
LABEL description="Phoenix Agent v1.0.0 - Multi-channel AI Agent Framework"
LABEL org.opencontainers.image.source="https://github.com/huasheng-zhang/phoenix"

# ── Environment ───────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PHOENIX_HOME=/app/.phoenix \
    # AgentPool defaults (can be overridden via config)
    PHOENIX_MAX_AGENTS=100 \
    PHOENIX_IDLE_TIMEOUT=3600

WORKDIR /app

# ── System deps (minimal) ────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

# ── Dependencies (install first for Docker layer caching) ─────────────
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel && \
    pip install -e ".[all]"

# ── Application code ─────────────────────────────────────────────────
COPY src/ ./src/
COPY config.example.yaml ./

# ── Runtime directories ──────────────────────────────────────────────
RUN mkdir -p /app/.phoenix/{downloads,history,skills,data} && \
    mkdir -p /app/skills

# Config & data volume (users mount config.yaml here)
VOLUME ["/app/.phoenix"]

# Optional: custom skills volume
VOLUME ["/app/skills"]

# ── Non-root user for security ───────────────────────────────────────
RUN useradd --create-home --shell /bin/sh appuser && \
    chown -R appuser:appuser /app
USER appuser

# ── Health check (hit the HTTP health endpoint) ──────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8080/health || exit 1

# ── Expose & run ─────────────────────────────────────────────────────
EXPOSE 8080
ENTRYPOINT ["python", "-m", "phoenix_agent"]
CMD ["serve"]
