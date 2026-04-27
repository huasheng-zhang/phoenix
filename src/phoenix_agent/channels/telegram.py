"""
Telegram Channel Adapter
========================

Integrates Phoenix Agent with Telegram via the `Bot API
<https://core.telegram.org/bots/api>`_.

Two operating modes
-------------------

1. **Webhook** (``mode: webhook``, *default & recommended in production*)
   - Telegram pushes updates to ``webhook_path`` (default ``/telegram/webhook``).
   - Requires a public HTTPS URL; set it with ``phoenix serve`` behind a reverse-proxy.
   - Set the webhook once:
     ``curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your.domain/telegram/webhook"``

2. **Long-polling** (``mode: polling``, *simpler for local dev*)
   - Phoenix calls ``getUpdates`` in a background loop.
   - No public URL needed.
   - Start with ``phoenix serve`` — polling starts automatically.

Configuration (config.yaml)
---------------------------
.. code-block:: yaml

    channels:
      telegram:
        enabled: true
        bot_token: "123456789:AAEXAMPLE-TOKEN"
        mode: webhook                  # webhook | polling
        webhook_path: /telegram/webhook
        allowed_updates:               # optional — defaults to ["message"]
          - message
          - callback_query
        superusers: [987654321]        # Telegram user IDs with admin access
        command_prefix: "/"            # Prefix for command detection

Dependencies
------------
- ``aiohttp`` (HTTP client) — ``pip install aiohttp``
- ``starlette`` (webhook mode) — ``pip install starlette``
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

try:
    from aiohttp import ClientSession as AiohttpSession
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    _HAS_STARLETTE = True
except ImportError:
    _HAS_STARLETTE = False

from phoenix_agent.channels.base import (
    BaseChannel,
    ChannelError,
    ChannelMessage,
    ChannelReply,
    MessageType,
)
from phoenix_agent.channels.registry import ChannelRegistry

logger = logging.getLogger(__name__)

# Telegram Bot API base URL template
_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramChannel(BaseChannel):
    """
    Telegram channel adapter.

    Supports text, photo captions, and command messages.
    Long messages are automatically split to stay within Telegram's 4096-char limit.
    """

    NAME = "telegram"
    _MAX_MESSAGE_LEN = 4096   # Telegram hard limit per message

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)

        cfg = self._channel_cfg
        self._enabled         : bool          = cfg.get("enabled", False)
        self._bot_token       : Optional[str] = cfg.get("bot_token")
        self._mode            : str           = cfg.get("mode", "webhook")
        self._webhook_path    : str           = cfg.get("webhook_path", "/telegram/webhook")
        self._allowed_updates : List[str]     = cfg.get("allowed_updates", ["message"])
        self._superusers      : List[int]     = [int(x) for x in cfg.get("superusers", [])]
        self._command_prefix  : str           = cfg.get("command_prefix", "/")

        # Long-polling state
        self._polling_task: Optional[asyncio.Task] = None
        self._last_update_id: int = 0

    # ------------------------------------------------------------------
    # Bot API helpers
    # ------------------------------------------------------------------

    def _api_url(self, method: str) -> str:
        """Build a Telegram Bot API URL."""
        if not self._bot_token:
            raise ChannelError(self.NAME, "bot_token is not configured")
        return _API_BASE.format(token=self._bot_token, method=method)

    async def _call(self, method: str, **params: Any) -> Dict[str, Any]:
        """
        Call a Telegram Bot API method.

        Args:
            method: API method name, e.g. ``sendMessage``.
            **params: Method parameters passed as JSON body.

        Returns:
            The ``result`` field of the API response.

        Raises:
            ChannelError: On HTTP errors or API-level failures.
        """
        if not _HAS_AIOHTTP:
            raise ChannelError(self.NAME, "aiohttp required: pip install aiohttp")

        url = self._api_url(method)
        async with AiohttpSession() as session:
            async with session.post(url, json=params, timeout=30) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise ChannelError(
                        self.NAME, f"Failed to parse API response: {exc}"
                    ) from exc

        if not data.get("ok"):
            raise ChannelError(
                self.NAME,
                f"Telegram API error {data.get('error_code')}: {data.get('description')}",
            )

        return data.get("result", {})

    # ------------------------------------------------------------------
    # send_message
    # ------------------------------------------------------------------

    async def send_message(self, chat_id: str, reply: ChannelReply) -> None:
        """
        Send a message to a Telegram chat.

        Long text is automatically chunked into multiple messages so each
        chunk stays within Telegram's 4096-character limit.

        Args:
            chat_id: Telegram chat ID (integer or ``"@channel_username"``).
            reply:   Normalised :class:`~phoenix_agent.channels.base.ChannelReply`.
        """
        text = reply.markdown or reply.text

        # Choose parse_mode based on whether markdown content is provided
        parse_mode = "Markdown" if reply.markdown else None

        chunks = self._split_text(text)
        for chunk in chunks:
            params: Dict[str, Any] = {
                "chat_id": chat_id,
                "text":    chunk,
            }
            if parse_mode:
                params["parse_mode"] = parse_mode

            # @-mention using reply_markup or inline text — Telegram uses
            # a different mechanism; just prepend plain @username if needed.
            if reply.at_users:
                mentions = " ".join(f"[user](tg://user?id={uid})"
                                    for uid in reply.at_users)
                params["text"] = mentions + "\n" + chunk
                params["parse_mode"] = "Markdown"

            try:
                await self._call("sendMessage", **params)
            except ChannelError as exc:
                # Retry without parse_mode (sometimes markdown causes issues)
                if parse_mode:
                    logger.warning(
                        "[telegram] Markdown send failed (%s), retrying as plain text", exc
                    )
                    params.pop("parse_mode", None)
                    params["text"] = reply.text
                    await self._call("sendMessage", **params)
                else:
                    raise

        logger.debug("[telegram] Message sent to chat_id=%s", chat_id)

    def _split_text(self, text: str) -> List[str]:
        """Split *text* into chunks no longer than ``_MAX_MESSAGE_LEN`` chars."""
        if len(text) <= self._MAX_MESSAGE_LEN:
            return [text]
        chunks = []
        while text:
            chunks.append(text[: self._MAX_MESSAGE_LEN])
            text = text[self._MAX_MESSAGE_LEN :]
        return chunks

    # ------------------------------------------------------------------
    # Webhook handler (inbound via HTTP POST)
    # ------------------------------------------------------------------

    def get_webhook_handler(self):
        """
        Return a Starlette ASGI app that receives Telegram webhook updates.

        Telegram POSTs JSON update objects to this endpoint.
        """
        channel = self

        if not _HAS_STARLETTE:
            logger.warning(
                "[telegram] Starlette not installed; webhook handler unavailable"
            )
            return None

        async def _handle(request: Request):
            try:
                payload: Dict = await request.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)

            msg = channel._parse_update(payload)
            if msg:
                await channel._dispatch(msg)

            # Always return 200 so Telegram doesn't retry
            return JSONResponse({"ok": True})

        return Starlette(routes=[
            Route("/", endpoint=_handle, methods=["POST"])
        ])

    # ------------------------------------------------------------------
    # Long-polling (development mode)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the long-polling loop (only used when ``mode == "polling"``)."""
        if self._mode == "polling":
            if not _HAS_AIOHTTP:
                logger.error("[telegram] aiohttp required for polling: pip install aiohttp")
                return
            logger.info("[telegram] Starting long-polling loop …")
            self._polling_task = asyncio.create_task(self._polling_loop())

    async def stop(self) -> None:
        """Stop the long-polling loop."""
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        logger.info("[telegram] Polling stopped")

    async def _polling_loop(self) -> None:
        """Continuously call getUpdates and dispatch each update."""
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    offset=self._last_update_id + 1,
                    timeout=30,
                    allowed_updates=self._allowed_updates,
                )
                for update in updates:
                    self._last_update_id = max(
                        self._last_update_id, update.get("update_id", 0)
                    )
                    msg = self._parse_update(update)
                    if msg:
                        asyncio.create_task(self._dispatch(msg))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[telegram] Polling error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_update(self, update: Dict[str, Any]) -> Optional[ChannelMessage]:
        """
        Convert a raw Telegram update dict into a normalised :class:`ChannelMessage`.

        Currently handles ``message`` updates only; callback_query and other
        update types are silently ignored.
        """
        raw_msg: Optional[Dict] = update.get("message")
        if not raw_msg:
            return None  # Ignore callback_query, edited_message, etc. for now

        chat:      Dict = raw_msg.get("chat",   {})
        sender:    Dict = raw_msg.get("from",   {})
        text:       str = raw_msg.get("text",   "").strip()
        caption:    str = raw_msg.get("caption", "").strip()
        timestamp: int  = raw_msg.get("date", int(time.time()))

        chat_id    = str(chat.get("id", ""))
        user_id    = str(sender.get("id", ""))
        first_name = sender.get("first_name", "")
        last_name  = sender.get("last_name", "")
        username   = sender.get("username", "")
        sender_name = f"{first_name} {last_name}".strip() or username or user_id

        # Determine message type
        if text:
            if text.startswith(self._command_prefix):
                msg_type = MessageType.COMMAND
            else:
                msg_type = MessageType.TEXT
        elif raw_msg.get("photo"):
            text     = caption or "[photo]"
            msg_type = MessageType.IMAGE
        elif raw_msg.get("voice") or raw_msg.get("audio"):
            text     = caption or "[audio]"
            msg_type = MessageType.AUDIO
        elif raw_msg.get("video"):
            text     = caption or "[video]"
            msg_type = MessageType.VIDEO
        elif raw_msg.get("document"):
            text     = caption or "[file]"
            msg_type = MessageType.FILE
        else:
            text     = "[unsupported message type]"
            msg_type = MessageType.UNKNOWN

        is_su = int(user_id) in self._superusers if user_id.isdigit() else False

        return ChannelMessage(
            channel=self.NAME,
            platform_id=chat_id,
            sender_id=user_id,
            sender_name=sender_name,
            text=text,
            msg_type=msg_type,
            raw=raw_msg,
            timestamp=float(timestamp),
            extra={
                "update_id":    update.get("update_id"),
                "username":     username,
                "chat_type":    chat.get("type", "private"),
                "is_superuser": is_su,
            },
        )


# Auto-register
ChannelRegistry.get_instance().register(TelegramChannel)
