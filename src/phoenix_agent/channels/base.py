"""
Base abstractions for Phoenix Agent channel adapters.

Every chat platform adapter must:
  1. Subclass ``BaseChannel``
  2. Implement ``send_message()``, ``get_webhook_handler()``, and optionally ``verify_signature()``
  3. Register itself via ``ChannelRegistry``

Data flow
---------
External platform  ──POST──►  webhook_handler  ──►  on_message(ChannelMessage)
                                                        │
                                                        ▼
                                                   Agent.run(text)
                                                        │
                                                        ▼
                                                  send_message(ChannelReply)
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MessageType(str, Enum):
    """Canonical message type shared across all channels."""

    TEXT       = "text"
    IMAGE      = "image"
    AUDIO      = "audio"
    VIDEO      = "video"
    FILE       = "file"
    VOICE      = "voice"      # Voice/audio clip (e.g. QQ voice)
    MARKDOWN   = "markdown"
    CARD       = "card"       # Rich interactive card (钉钉 ActionCard, etc.)
    AT_BOT     = "at_bot"     # Message that @-mentions the bot (platform-specific)
    COMMAND    = "command"    # Explicit command like /ask or /run
    EVENT      = "event"      # Platform event (subscribe, unsubscribe, click, etc.)
    UNKNOWN    = "unknown"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ChannelMessage:
    """
    Normalised inbound message from any chat platform.

    Attributes:
        channel:    Channel name, e.g. "dingtalk".
        platform_id: Raw platform-specific conversation/chat identifier.
        sender_id:  Platform-specific user identifier.
        sender_name: Display name of the sender (best-effort).
        text:       Plain-text body of the message.
        msg_type:   Normalised MessageType enum value.
        raw:        The raw payload dict as received from the platform.
        timestamp:  Unix timestamp (seconds).  Defaults to current time.
        attachments: List of attachment dicts (URL, type, size, …).
        reply_to:   Message ID this message is replying to (optional).
        extra:      Arbitrary extra data set by the channel adapter.
    """

    channel:     str
    platform_id: str
    sender_id:   str
    sender_name: str
    text:        str
    msg_type:    MessageType = MessageType.TEXT
    raw:         Dict[str, Any] = field(default_factory=dict)
    timestamp:   float = field(default_factory=time.time)
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    reply_to:    Optional[str] = None
    extra:       Dict[str, Any] = field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        return self.msg_type in (MessageType.TEXT, MessageType.AT_BOT, MessageType.COMMAND)


@dataclass
class ChannelFile:
    """
    File attachment for outbound replies.

    Attributes:
        path:     Local file path (image, document, video, etc.).
        url:      Download URL (alternative to path).
        file_type: MIME type hint (e.g. "image/png", "application/pdf").
        file_name: Original file name for display.
    """

    path:      Optional[str] = None
    url:       Optional[str] = None
    file_type: Optional[str] = None
    file_name: Optional[str] = None


@dataclass
class ChannelReply:
    """
    Normalised outbound reply from the agent.

    Attributes:
        text:     Plain-text content (always provided).
        markdown: Markdown-formatted content when the platform supports it.
        card:     Rich card payload (platform-specific dict).
        images:   List of image URLs / base64 strings.
        files:    List of ChannelFile attachments (images, documents, etc.).
        at_users: List of user IDs to @-mention in the reply.
    """

    text:     str
    markdown: Optional[str] = None
    card:     Optional[Dict[str, Any]] = None
    images:   List[str] = field(default_factory=list)
    files:    List[ChannelFile] = field(default_factory=list)
    at_users: List[str] = field(default_factory=list)

    @classmethod
    def from_text(cls, text: str) -> "ChannelReply":
        """Convenience constructor for plain-text replies."""
        return cls(text=text)

    @classmethod
    def from_markdown(cls, md: str) -> "ChannelReply":
        """Convenience constructor for Markdown replies."""
        return cls(text=md, markdown=md)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChannelError(Exception):
    """Raised when a channel-level operation fails."""

    def __init__(self, channel: str, message: str, cause: Optional[Exception] = None):
        self.channel = channel
        self.cause = cause
        super().__init__(f"[{channel}] {message}")


class SignatureVerificationError(ChannelError):
    """Raised when a webhook signature check fails."""


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


# Type alias for the on_message callback
OnMessageCallback = Callable[[ChannelMessage], Awaitable[Optional[ChannelReply]]]


class BaseChannel(abc.ABC):
    """
    Abstract base class for all Phoenix Agent channel adapters.

    Subclasses must implement:
        - ``send_message``       : Push a reply to the platform.
        - ``get_webhook_handler``: Return a platform-specific ASGI/WSGI handler
                                   that receives incoming messages and calls
                                   ``self._dispatch(msg)`` for each one.

    Optionally override:
        - ``verify_signature``   : Validate inbound webhook signatures.
        - ``start``              : Perform async setup (e.g. long-poll loop).
        - ``stop``               : Clean up resources.
    """

    #: Unique identifier for this channel type, e.g. "dingtalk".
    #: Must be set by subclasses.
    NAME: str = ""

    def __init__(self, config: Any = None):
        """
        Initialise the channel.

        Args:
            config: Full Phoenix ``Config`` object.  Subclasses should call
                    ``super().__init__(config)`` and then read their own section
                    from ``config.channels.<NAME>``.
        """
        self._config = config
        self._on_message: Optional[OnMessageCallback] = None
        self._channel_cfg: Dict[str, Any] = self._load_channel_config()

    def _load_channel_config(self) -> Dict[str, Any]:
        """
        Extract the channel-specific settings dict from the config.

        Supports three calling conventions:

        1. ``config`` is a Phoenix ``Config`` object that has a
           ``channels.channels`` dict → look up ``NAME`` in that dict.
        2. ``config`` is a plain ``dict`` that contains the channel's own
           settings (e.g. passed directly in tests or programmatic use).
        3. ``config`` is ``None`` → return empty dict.
        """
        if self._config is None:
            return {}

        # Plain dict: treat it directly as this channel's settings
        if isinstance(self._config, dict):
            return self._config

        # Phoenix Config object with channels attribute
        channels = getattr(self._config, "channels", None)
        if channels is None:
            return {}

        if isinstance(channels, dict):
            return channels.get(self.NAME, {})

        # Support ChannelsConfig dataclass
        ch_cfgs = getattr(channels, "channels", None)
        if isinstance(ch_cfgs, dict):
            ch_cfg = ch_cfgs.get(self.NAME)
            if ch_cfg is not None:
                return getattr(ch_cfg, "settings", {}) or {}
        return {}

    def register_handler(self, callback: OnMessageCallback) -> None:
        """
        Register an async callback to be called for every inbound message.

        The callback receives a ``ChannelMessage`` and may return a
        ``ChannelReply`` (or ``None`` to suppress the auto-reply).

        Args:
            callback: ``async def handler(msg: ChannelMessage) -> Optional[ChannelReply]``
        """
        self._on_message = callback

    async def _dispatch(self, msg: ChannelMessage) -> None:
        """
        Internal: invoke the registered handler and send the reply.

        This is called by the channel's webhook handler for each received
        message.  Override only when you need custom dispatch logic.
        """
        if self._on_message is None:
            logger.warning("[%s] No message handler registered", self.NAME)
            return

        try:
            reply = await self._on_message(msg)
        except Exception:
            logger.exception("[%s] on_message handler raised an exception", self.NAME)
            reply = ChannelReply.from_text(
                "抱歉，处理您的消息时发生了内部错误，请稍后重试。"
            )

        if reply is not None:
            try:
                await self.send_message(msg.platform_id, reply)
            except ChannelError:
                logger.exception("[%s] Failed to send reply", self.NAME)

    # ------------------------------------------------------------------
    # Abstract interface – must implement in every subclass
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def send_message(self, chat_id: str, reply: ChannelReply) -> None:
        """
        Send a reply message to the specified chat/conversation.

        Args:
            chat_id: Platform conversation identifier (sessionWebhook for
                     DingTalk, touser for WeCom, group_id for QQ, etc.).
            reply:   Normalised ``ChannelReply`` object.

        Raises:
            ChannelError: If the platform API returns an error.
        """

    @abc.abstractmethod
    def get_webhook_handler(self):
        """
        Return an ASGI application (or a Flask/Starlette route factory) that
        handles inbound HTTP callbacks from the platform.

        The returned callable will be mounted at the path configured in
        ``channels.<NAME>.webhook_path`` (default ``/<NAME>/webhook``).
        """

    # ------------------------------------------------------------------
    # Optional interface – override as needed
    # ------------------------------------------------------------------

    def verify_signature(self, payload: bytes, signature: str, timestamp: str = "") -> bool:
        """
        Verify the HMAC signature of an inbound webhook request.

        Default implementation returns ``True`` (no verification).
        Override in subclasses that support signature verification.

        Args:
            payload:   Raw request body bytes.
            signature: Signature string from the request header.
            timestamp: Request timestamp string (used by some platforms).

        Returns:
            ``True`` if the signature is valid.
        """
        return True

    async def start(self) -> None:
        """
        Perform any async setup required before the channel is ready.
        Called by the serve command before the HTTP server starts.
        Override for long-polling channels, etc.
        """

    async def stop(self) -> None:
        """
        Clean up resources when the server is shutting down.
        """

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _hmac_sha256(secret: str, message: str) -> str:
        """Compute HMAC-SHA256 and return hex digest."""
        return hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _hmac_sha256_bytes(secret: bytes, message: bytes) -> bytes:
        """Compute HMAC-SHA256 and return raw bytes."""
        return hmac.new(secret, message, digestmod=hashlib.sha256).digest()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.NAME!r}>"
