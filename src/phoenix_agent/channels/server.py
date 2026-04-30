"""
Channel Server
==============

Starts a single Starlette/uvicorn ASGI server that:
  - Mounts all enabled HTTP-webhook channels (DingTalk internal, WeChat, QQ, Telegram)
  - Starts all enabled stream-mode channels (DingTalk stream) as asyncio background tasks

Phoenix Agent's ``phoenix serve`` CLI command calls :func:`run_server`.

Architecture
------------
::

    ┌──────────────────────────────────────────────────────┐
    │  uvicorn (async HTTP server)                        │
    │                                                      │
    │  Starlette (ASGI router)                             │
    │   ├─ /                     → Web UI (chat SPA)       │
    │   ├─ /health               → health check            │
    │   ├─ /dingtalk/webhook     → DingTalkChannel         │
    │   ├─ /wechat/webhook       → WeChatChannel           │
    │   ├─ /qq/webhook           → QQChannel               │
    │   └─ /telegram/webhook     → TelegramChannel         │
    │                                                      │
    │  Stream channels (long connection)                     │
    │   └─ DingTalkStreamClient  → Phoenix Agent           │
    │                                                      │
    │  AgentPool — one Agent per (channel, chat_id)        │
    │   ├─ web/session-abc123   → Agent A                  │
    │   ├─ dingtalk/group:cid-123  → Agent B              │
    │   ├─ telegram/chat:789      → Agent C              │
    │   └─ ...                                                │
    └──────────────────────────────────────────────────────┘

Each conversation gets its own Agent instance via :class:`~phoenix_agent.core.pool.AgentPool`,
ensuring complete context isolation between users and chats.

Usage
-----
.. code-block:: bash

    # With default config (~/.phoenix/config.yaml)
    phoenix serve

    # Custom config
    phoenix serve --config /etc/phoenix/config.yaml

    # Override host/port
    phoenix serve --host 0.0.0.0 --port 9090
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import uvicorn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter (lightweight, in-memory, no external deps)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Token-bucket rate limiter keyed by client IP."""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self._max = max_requests
        self._window = window_seconds
        self._buckets: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        """Return True if *key* has not exceeded the rate limit."""
        now = time.monotonic()
        bucket = self._buckets[key]
        # Prune timestamps outside the window
        cutoff = now - self._window
        self._buckets[key] = bucket = [t for t in bucket if t > cutoff]
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True

    def cleanup(self) -> None:
        """Remove expired entries to prevent memory growth."""
        now = time.monotonic()
        cutoff = now - self._window
        expired = [k for k, v in self._buckets.items()
                   if not v or v[-1] <= cutoff]
        for k in expired:
            del self._buckets[k]


# Singleton rate limiter instance (60 requests / 60 seconds per IP)
_rate_limiter = _RateLimiter(max_requests=60, window_seconds=60)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_app(
    config=None,
) -> Tuple[Any, List[Tuple[str, Any]], Any]:
    """
    Build the Starlette ASGI app and return (app, stream_channels, pool, starlette_app).

    Args:
        config: A :class:`~phoenix_agent.core.config.Config` instance.
                If *None*, loads from the default location.

    Returns:
        A 3-tuple:
          - app: A Starlette ASGI application.
          - stream_channels: List of (channel_name, channel_instance) that
            need ``start_stream()`` called.
          - pool: The :class:`~phoenix_agent.core.pool.AgentPool` instance.

    Raises:
        RuntimeError: If no channels are enabled at all.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    from phoenix_agent.core.config import get_config
    from phoenix_agent.core.pool import AgentPool
    from phoenix_agent.channels.registry import ChannelRegistry

    cfg = config or get_config()

    # --- Ensure all channel adapters are imported (triggers auto-register) ---
    _import_channel_adapters()

    # --- Create AgentPool (manages per-conversation Agent instances) ---
    pool = AgentPool(config=cfg)

    # --- Classify channels by mode ---
    registry = ChannelRegistry.get_instance()
    http_routes: List[Any] = []
    stream_channels: List[Tuple[str, Any]] = []

    channels_cfg = cfg.channels
    for ch_name, ch_cfg in channels_cfg.channels.items():
        if not ch_cfg.enabled:
            logger.debug("Channel %r is disabled — skipping", ch_name)
            continue

        channel_cls = registry.get(ch_name)
        if channel_cls is None:
            logger.warning(
                "Channel %r is enabled in config but its adapter is not registered.",
                ch_name,
            )
            continue

        # Instantiate channel with the raw settings dict
        channel = channel_cls(config=ch_cfg.settings)

        # Wire the pool as the message handler (for internal/webhook modes)
        def _make_handler(ch=channel, pl=pool):
            async def _handle(msg):
                await _process_message(ch, pl, msg)
            return _handle

        channel.register_handler(_make_handler())

        # Store instance in registry so scheduler can access it for push
        registry.set_instance(ch_name, channel)

        # --- Route by mode ---
        if hasattr(channel, "start_stream") and callable(getattr(channel, "start_stream")):
            # Stream-mode channel — handled separately (no HTTP route needed)
            stream_channels.append((ch_name, channel))
            logger.info("Registered stream channel %r", ch_name)
        else:
            # HTTP webhook channel
            handler = channel.get_webhook_handler()
            if handler is None:
                logger.warning(
                    "Channel %r is enabled but returned no webhook handler.",
                    ch_name,
                )
                continue

            http_routes.append(Mount(ch_cfg.webhook_path, app=handler))
            logger.info("Mounted channel %r at %s", ch_name, ch_cfg.webhook_path)

    # --- At least one type of channel must be present ---
    # NOTE: The Web UI is always mounted (see below), so we always have
    # at least one route.  We only raise if no *messaging* channels are
    # configured and the user hasn't explicitly opted in to web-only mode.
    if not http_routes and not stream_channels:
        logger.warning(
            "No messaging channels configured. "
            "The Web UI is still available at http://%s:%d/",
            cfg.channels.host, cfg.channels.port,
        )

    # --- Health-check route ---
    async def _health(request: Request):
        return JSONResponse({"status": "ok", "service": "phoenix-agent"})

    http_routes.insert(0, Route("/health", endpoint=_health))

    # --- Web UI (inline routes, no sub-app mount) ---
    try:
        from phoenix_agent.web.routes import build_web_routes
        web_ui_routes = build_web_routes(pool=pool, config=cfg)
        http_routes.extend(web_ui_routes)
        logger.info("Web UI routes registered")
    except Exception as exc:
        logger.warning("Failed to mount Web UI: %s", exc)

    app = Starlette(routes=http_routes)

    # --- Rate-limiting middleware for webhook endpoints ---
    _starlette_app = app  # keep reference for event handlers

    async def _rate_limit_middleware(scope, receive, send):
        if scope["type"] == "http":
            # Skip rate limiting for health check and web UI
            path = scope.get("path", "")
            if path == "/health" or path == "/" or path.startswith("/api/"):
                await _starlette_app(scope, receive, send)
                return

            client_ip = (
                scope.get("client", [None])[0]
                or scope.get("headers", {})
                    .get(b"x-forwarded-for", [b""])
                    [0]
                    .decode("utf-8", errors="ignore")
                    .split(",")[0]
                    .strip()
                or "unknown"
            )

            if not _rate_limiter.is_allowed(client_ip):
                from starlette.responses import JSONResponse as _JR
                response = _JR(
                    {"status": "too_many_requests",
                     "error": "Rate limit exceeded. Try again later."},
                    status_code=429,
                )
                await response(scope, receive, send)
                return

            # Periodic cleanup (roughly every ~1000 requests to this path)
            if hash(client_ip) % 1000 == 0:
                _rate_limiter.cleanup()

        await _starlette_app(scope, receive, send)

    app = _rate_limit_middleware  # wrap the ASGI app

    return app, stream_channels, pool, _starlette_app


async def _start_stream_channels(
    stream_channels: List[Tuple[str, Any]],
    pool,
) -> List[asyncio.Task]:
    """Start all stream-mode channels as background asyncio tasks."""
    tasks: List[asyncio.Task] = []
    for ch_name, channel in stream_channels:
        task = asyncio.create_task(
            _run_stream_channel(ch_name, channel, pool),
            name=f"stream-{ch_name}",
        )
        tasks.append(task)
        logger.info("Started stream task for channel %r", ch_name)
    return tasks


async def _run_stream_channel(ch_name: str, channel, pool) -> None:
    """Run a single stream-mode channel. Propagates exceptions to the caller."""
    try:
        # Stream channels receive the pool so they can route per-conversation
        await channel.start_stream(pool)
    except asyncio.CancelledError:
        logger.info("[%s] Stream task cancelled", ch_name)
    except Exception as exc:
        logger.exception("[%s] Stream task crashed: %s", ch_name, exc)


async def _shutdown_stream_channels(tasks: List[asyncio.Task]) -> None:
    """Cancel and wait for all stream tasks to shut down cleanly."""
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("All stream channels shut down.")


def run_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    config=None,
    reload: bool = False,
    log_level: str = "info",
) -> None:
    """
    Build the ASGI app, start stream channels, start scheduler, and run uvicorn.

    Args:
        host:      Bind address (default ``0.0.0.0``).
        port:      Bind port (default ``8080``).
        config:    Optional pre-loaded Config.
        reload:    Enable auto-reload (development only).
        log_level: uvicorn log level (``debug`` / ``info`` / ``warning`` …).
    """
    from phoenix_agent.core.config import get_config
    from phoenix_agent.core.scheduler import PhoenixScheduler

    cfg = config or get_config()

    # --- Scheduler singleton (always created so Agent tools can add/remove tasks) ---
    scheduler = None
    try:
        scheduler = PhoenixScheduler(config=cfg)
        # Only start the APScheduler loop if tasks exist in config
        if scheduler._task_configs:
            scheduler.start()
            logger.info("Scheduler started with %d task(s).", len(scheduler.list_tasks()))
        else:
            logger.info("Scheduler singleton registered (no tasks in config yet).")
    except Exception as exc:
        logger.warning("Failed to initialize scheduler: %s", exc)
        scheduler = None

    # env > config > function args
    env_host = os.environ.get("PHOENIX_CHANNEL_HOST", "").strip()
    env_port = os.environ.get("PHOENIX_CHANNEL_PORT", "").strip()
    host = env_host or cfg.channels.host or host
    port = int(env_port or cfg.channels.port or port)

    app, stream_channels, pool, _starlette_app = build_app(cfg)

    # Collect stream tasks so they can be cancelled on shutdown
    stream_tasks: List[asyncio.Task] = []

    # Use uvicorn "startup" event (fires after the event loop is fully running)
    # instead of lifespan startup — avoids sync-HTTP blocking in start_stream().
    async def _on_startup():
        nonlocal stream_tasks
        if stream_channels:
            stream_tasks = await _start_stream_channels(stream_channels, pool)

    # Keep scheduler reference so lifespan can shut it down
    scheduler_ref = [scheduler]  # use a list as a mutable cell

    async def lifespan(_app):
        yield  # startup is handled by _on_startup above
        await _shutdown_stream_channels(stream_tasks)
        pool.shutdown()
        # Stop scheduler on shutdown
        if scheduler_ref[0] is not None:
            try:
                scheduler_ref[0].stop()
                logger.info("Scheduler stopped.")
            except Exception as exc:
                logger.warning("Error stopping scheduler: %s", exc)

    _starlette_app.add_event_handler("startup", _on_startup)

    logger.info("Starting Phoenix channel server on %s:%d", host, port)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        reload=reload,
        lifespan="on",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _import_channel_adapters() -> None:
    """
    Import all built-in channel adapter modules so their
    ``ChannelRegistry.get_instance().register(...)`` lines run.
    """
    adapters = [
        "phoenix_agent.channels.dingtalk",
        "phoenix_agent.channels.wechat",
        "phoenix_agent.channels.qq",
        "phoenix_agent.channels.telegram",
    ]
    for module_path in adapters:
        try:
            importlib.import_module(module_path)
        except ImportError as exc:
            logger.debug("Could not import channel adapter %s: %s", module_path, exc)


async def _process_message(channel, pool, message) -> None:
    """
    Central message processing coroutine.

    Receives a :class:`~phoenix_agent.channels.base.ChannelMessage`, looks up
    or creates a per-conversation Agent from the pool, runs it, then sends
    the reply back via the channel.

    Each ``(channel_name, platform_id)`` pair gets its own Agent, ensuring
    complete context isolation between conversations.
    """
    from phoenix_agent.channels.base import ChannelReply

    chat_id   = message.platform_id
    user_text = message.text.strip()

    if not user_text:
        logger.debug("[server] Empty message from %s — ignoring", chat_id[:8])
        return

    logger.info(
        "[server] Received message from channel=%s chat=%s: %r",
        message.channel, chat_id, user_text[:80],
    )

    # Get or create a per-conversation agent
    agent = pool.get_agent(message.channel, chat_id)

    try:
        loop = asyncio.get_event_loop()
        response_text = await loop.run_in_executor(
            None,
            lambda: agent.run(user_text),
        )
        reply = ChannelReply(text=response_text or "(no response)")
        await channel.send_message(chat_id, reply)
    except Exception as exc:
        logger.exception("[server] Error processing message: %s", exc)
        try:
            err_reply = ChannelReply(text="⚠️ 处理消息时出错，请稍后重试")
            await channel.send_message(chat_id, err_reply)
        except Exception:
            pass
