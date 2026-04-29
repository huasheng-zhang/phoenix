"""
QQ Channel Adapter (OneBot v11 Protocol)
==========================================

Implements the `OneBot v11 <https://11.onebot.dev/>`_ HTTP adapter protocol,
which is supported by popular QQ bot frameworks such as:

- **go-cqhttp** (https://github.com/Mrs4s/go-cqhttp)
- **NapCat** (https://github.com/NapNeko/NapCatQQ)
- **LLOneBot** (https://github.com/LLOneBot/LLOneBot)
- **Lagrange.OneBot**

Integration modes
-----------------

1. **HTTP + HTTP Callback** (推荐, 默认)
   - go-cqhttp runs an HTTP API server (default port 5700).
   - Phoenix exposes ``webhook_path`` (default ``/qq/webhook``) to receive events.
   - Configure ``api_url`` to point at go-cqhttp's HTTP API.

2. **反向 WebSocket** (reverse WebSocket) — *TODO: future enhancement*

Configuration (config.yaml)::

    channels:
      qq:
        enabled: true
        api_url: "http://127.0.0.1:5700"   # go-cqhttp HTTP API endpoint
        access_token: ""                    # Optional: HTTP API access token
        webhook_path: /qq/webhook           # Receive events here
        self_id: 123456789                  # Bot's QQ number (for @-detection)
        superusers: [987654321]             # QQ numbers with admin access
        command_prefix: "/"                 # Prefix for commands (default /)
"""

from __future__ import annotations

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


class QQChannel(BaseChannel):
    """
    QQ channel adapter using the OneBot v11 HTTP API protocol.

    Supports private chats, group chats, and @-mentions.
    """

    NAME = "qq"

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)

        cfg = self._channel_cfg
        self._enabled        : bool            = cfg.get("enabled", False)
        self._api_url        : str             = cfg.get("api_url", "http://127.0.0.1:5700")
        self._access_token   : Optional[str]   = cfg.get("access_token")
        self._webhook_path   : str             = cfg.get("webhook_path", "/qq/webhook")
        self._self_id        : Optional[int]   = cfg.get("self_id")
        self._superusers     : List[int]       = cfg.get("superusers", [])
        self._command_prefix : str             = cfg.get("command_prefix", "/")

    # ------------------------------------------------------------------
    # HTTP API helpers
    # ------------------------------------------------------------------

    def _api_headers(self) -> Dict[str, str]:
        """Build HTTP headers for go-cqhttp API calls."""
        headers = {"Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _call_api(self, action: str, **params: Any) -> Dict[str, Any]:
        """
        Call a go-cqhttp OneBot v11 API action.

        Args:
            action: API endpoint name, e.g. ``send_private_msg``.
            **params: Keyword arguments forwarded as the JSON body.

        Returns:
            Parsed JSON response dict.

        Raises:
            ChannelError: If the HTTP request fails or the API returns a failure status.
        """
        if not _HAS_AIOHTTP:
            raise ChannelError(self.NAME, "aiohttp required: pip install aiohttp")

        url = f"{self._api_url.rstrip('/')}/{action}"

        async with AiohttpSession() as session:
            async with session.post(
                url,
                json=params,
                headers=self._api_headers(),
                timeout=30,
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise ChannelError(self.NAME, f"Failed to parse API response: {exc}") from exc

        status = data.get("status", "")
        if status == "failed":
            raise ChannelError(
                self.NAME,
                f"OneBot API error {data.get('retcode')}: {data}",
            )

        return data.get("data") or {}

    # ------------------------------------------------------------------
    # send_message
    # ------------------------------------------------------------------

    async def send_message(self, chat_id: str, reply: ChannelReply) -> None:
        """
        Send a message to QQ.

        ``chat_id`` format:
          - Private chat:  ``"private:{user_qq}"``, e.g. ``"private:123456"``
          - Group chat:    ``"group:{group_id}"``,  e.g. ``"group:987654"``

        Args:
            chat_id: Target identifier (see format above).
            reply:   Content to send.
        """
        # Build CQ-code message string
        message = self._build_message(reply)

        if chat_id.startswith("private:"):
            user_id = int(chat_id.split(":", 1)[1])
            await self._call_api("send_private_msg", user_id=user_id, message=message)

        elif chat_id.startswith("group:"):
            group_id = int(chat_id.split(":", 1)[1])
            await self._call_api("send_group_msg", group_id=group_id, message=message)

        else:
            # Fallback: try to parse as a raw user_id for private chat
            try:
                user_id = int(chat_id)
                await self._call_api("send_private_msg", user_id=user_id, message=message)
            except ValueError:
                raise ChannelError(
                    self.NAME,
                    f"Cannot determine message target from chat_id: {chat_id!r}. "
                    "Use 'private:<user_id>' or 'group:<group_id>'.",
                )

        logger.debug("[qq] Message sent to %s", chat_id)

    def _build_message(self, reply: ChannelReply) -> str:
        """
        Build a OneBot v11 message string (with optional CQ codes).

        Wraps @-mentions as ``[CQ:at,qq=<id>]`` codes.
        """
        parts: List[str] = []

        # Add @-mentions
        for user_id in reply.at_users:
            parts.append(f"[CQ:at,qq={user_id}]")

        if parts:
            parts.append(" ")   # space after @mentions

        # Use markdown text if provided, else plain text
        parts.append(reply.markdown or reply.text)

        return "".join(parts)

    # ------------------------------------------------------------------
    # Webhook handler (inbound)
    # ------------------------------------------------------------------

    def get_webhook_handler(self):
        """
        Return a Starlette ASGI app that receives OneBot v11 HTTP POST events.

        Mount at ``self._webhook_path`` in the main server application.
        """
        channel = self

        if not _HAS_STARLETTE:
            logger.warning("[qq] Starlette not installed; webhook handler unavailable")
            return None

        async def _handle(request: Request):
            # Verify access token (required for security)
            auth = request.headers.get("Authorization", "")
            if not channel._access_token:
                logger.warning(
                    "[qq] No access_token configured — webhook is unprotected! "
                    "Set 'access_token' in config.yaml channels.qq section."
                )
            else:
                expected = f"Bearer {channel._access_token}"
                if auth != expected:
                    return JSONResponse({"status": "forbidden"}, status_code=403)

            try:
                payload: Dict = await request.json()
            except Exception:
                return JSONResponse({"status": "bad_request"}, status_code=400)

            msg = channel._parse_event(payload)
            if msg:
                await channel._dispatch(msg)

            return JSONResponse({"status": "ok"})

        return Starlette(routes=[
            Route("/", endpoint=_handle, methods=["POST"])
        ])

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_event(self, payload: Dict[str, Any]) -> Optional[ChannelMessage]:
        """
        Parse a OneBot v11 event payload into a ChannelMessage.

        Only ``message`` events (private/group) are processed; others are
        silently ignored (meta, notice, request events).
        """
        post_type = payload.get("post_type", "")

        if post_type != "message":
            return None  # Ignore meta/notice/request events

        msg_type_raw: str = payload.get("message_type", "")
        user_id:      int = payload.get("user_id", 0)
        group_id:     int = payload.get("group_id", 0)
        raw_message:  str = payload.get("raw_message", "")
        sender:       Dict= payload.get("sender", {})
        timestamp:    int = payload.get("time", int(time.time()))

        # Determine chat_id
        if msg_type_raw == "group":
            chat_id = f"group:{group_id}"
        else:
            chat_id = f"private:{user_id}"

        # Strip @-bot CQ code from message text
        bot_id = self._self_id
        if bot_id:
            raw_message = raw_message.replace(f"[CQ:at,qq={bot_id}]", "").strip()

        # Detect message type
        if "[CQ:image" in raw_message:
            msg_type = MessageType.IMAGE
        elif "[CQ:record" in raw_message:
            msg_type = MessageType.VOICE
        else:
            msg_type = MessageType.TEXT

        # Resolve plain text from CQ codes (strip other CQ codes)
        import re
        plain_text = re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()
        if not plain_text:
            plain_text = raw_message  # Keep raw if nothing left

        is_su = int(user_id) in self._superusers

        return ChannelMessage(
            channel=self.NAME,
            platform_id=chat_id,
            sender_id=str(user_id),
            sender_name=sender.get("nickname", str(user_id)),
            text=plain_text,
            msg_type=msg_type,
            raw=payload,
            timestamp=float(timestamp),
            extra={
                "group_id":  group_id,
                "user_id":   user_id,
                "is_superuser": is_su,
                "message_type": msg_type_raw,
            },
        )


# Auto-register
ChannelRegistry.get_instance().register(QQChannel)
