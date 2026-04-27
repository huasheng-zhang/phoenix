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
    │   ├─ /health                → health check           │
    │   ├─ /dingtalk/webhook      → DingTalkChannel        │
    │   ├─ /wechat/webhook        → WeChatChannel          │
    │   ├─ /qq/webhook            → QQChannel              │
    │   └─ /telegram/webhook      → TelegramChannel        │
    │                                                      │
    │  Stream channels (long connection)                     │
    │   └─ DingTalkStreamClient  → Phoenix Agent           │
    └──────────────────────────────────────────────────────┘

All channels share the same :class:`~phoenix_agent.core.agent.Agent` instance.

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
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import uvicorn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_app(
    config=None,
) -> Tuple[Any, List[Tuple[str, Any]], Any]:
    """
    Build the Starlette ASGI app and return (app, stream_channels, agent).

    Args:
        config: A :class:`~phoenix_agent.core.config.Config` instance.
                If *None*, loads from the default location.

    Returns:
        A 3-tuple:
          - app: A Starlette ASGI application.
          - stream_channels: List of (channel_name, channel_instance) that
            need ``start_stream()`` called.
          - agent: The shared Phoenix Agent instance.

    Raises:
        RuntimeError: If no channels are enabled at all.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    from phoenix_agent.core.config import get_config
    from phoenix_agent.core.agent import Agent
    from phoenix_agent.channels.registry import ChannelRegistry

    cfg = config or get_config()

    # --- Ensure all channel adapters are imported (triggers auto-register) ---
    _import_channel_adapters()

    # --- Create shared Agent instance ---
    agent = Agent(config=cfg)

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

        # Wire the agent as the message handler (for internal/webhook modes)
        def _make_handler(ch=channel, ag=agent):
            async def _handle(msg):
                await _process_message(ch, ag, msg)
            return _handle

        channel.register_handler(_make_handler())

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
    if not http_routes and not stream_channels:
        raise RuntimeError(
            "No channels were successfully mounted or started. "
            "Enable at least one channel in config.yaml under the 'channels:' section."
        )

    # --- Health-check route ---
    async def _health(request: Request):
        return JSONResponse({"status": "ok", "service": "phoenix-agent"})

    http_routes.insert(0, Route("/health", endpoint=_health))

    app = Starlette(routes=http_routes)
    return app, stream_channels, agent


async def _start_stream_channels(
    stream_channels: List[Tuple[str, Any]],
    agent,
) -> List[asyncio.Task]:
    """Start all stream-mode channels as background asyncio tasks."""
    tasks: List[asyncio.Task] = []
    for ch_name, channel in stream_channels:
        task = asyncio.create_task(
            _run_stream_channel(ch_name, channel, agent),
            name=f"stream-{ch_name}",
        )
        tasks.append(task)
        logger.info("Started stream task for channel %r", ch_name)
    return tasks


async def _run_stream_channel(ch_name: str, channel, agent) -> None:
    """Run a single stream-mode channel. Propagates exceptions to the caller."""
    try:
        await channel.start_stream(agent)
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
    Build the ASGI app, start stream channels, and run uvicorn.

    Args:
        host:      Bind address (default ``0.0.0.0``).
        port:      Bind port (default ``8080``).
        config:    Optional pre-loaded Config.
        reload:    Enable auto-reload (development only).
        log_level: uvicorn log level (``debug`` / ``info`` / ``warning`` …).
    """
    from phoenix_agent.core.config import get_config

    cfg = config or get_config()

    # env > config > function args
    env_host = os.environ.get("PHOENIX_CHANNEL_HOST", "").strip()
    env_port = os.environ.get("PHOENIX_CHANNEL_PORT", "").strip()
    host = env_host or cfg.channels.host or host
    port = int(env_port or cfg.channels.port or port)

    app, stream_channels, agent = build_app(cfg)

    # Collect stream tasks so they can be cancelled on shutdown
    stream_tasks: List[asyncio.Task] = []

    # Use uvicorn "startup" event (fires after the event loop is fully running)
    # instead of lifespan startup — avoids sync-HTTP blocking in start_stream().
    async def _on_startup():
        nonlocal stream_tasks
        if stream_channels:
            stream_tasks = await _start_stream_channels(stream_channels, agent)

    async def lifespan(_app):
        yield  # startup is handled by _on_startup above
        await _shutdown_stream_channels(stream_tasks)

    app.add_event_handler("startup", _on_startup)

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


async def _process_message(channel, agent, message) -> None:
    """
    Central message processing coroutine.

    Receives a :class:`~phoenix_agent.channels.base.ChannelMessage`, runs it
    through the agent, then sends the reply back via the channel.
    """
    from phoenix_agent.channels.base import ChannelReply

    chat_id   = message.platform_id
    user_text = message.text.strip()

    if not user_text:
        logger.debug("[server] Empty message from %s — ignoring", chat_id)
        return

    logger.info(
        "[server] Received message from channel=%s chat=%s: %r",
        message.channel, chat_id, user_text[:80],
    )

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
            err_reply = ChannelReply(text=f"⚠️ 处理消息时出错：{exc}")
            await channel.send_message(chat_id, err_reply)
        except Exception:
            pass
