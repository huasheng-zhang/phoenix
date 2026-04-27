"""
Phoenix Agent - Channels Package

Provides channel adapters that connect external chat platforms to the agent.
Each channel handles platform-specific authentication, message parsing, and reply formatting.

Supported channels:
    - dingtalk  : DingTalk (钉钉) — Webhook outbound + HTTP inbound
    - wechat    : WeChat Work (企业微信) / WeChat Official Account (微信公众号)
    - qq        : QQ via OneBot v11 / go-cqhttp / NapCat / LLOneBot
    - telegram  : Telegram Bot API

Start the channel server:
    phoenix serve                     # all enabled channels
    phoenix serve --host 0.0.0.0 --port 8080

Quick start (library usage):
    from phoenix_agent.channels import get_channel, list_channels
    ch = get_channel("dingtalk", config)
    await ch.send_message(chat_id, ChannelReply(text="Hello from Phoenix!"))
"""

from phoenix_agent.channels.base import (
    BaseChannel,
    ChannelError,
    ChannelFile,
    ChannelMessage,
    ChannelReply,
    MessageType,
    SignatureVerificationError,
)
from phoenix_agent.channels.registry import ChannelRegistry
from phoenix_agent.channels.server import build_app, run_server

__all__ = [
    # Data classes
    "BaseChannel",
    "ChannelMessage",
    "ChannelReply",
    "ChannelFile",
    "MessageType",
    # Exceptions
    "ChannelError",
    "SignatureVerificationError",
    # Registry / factory
    "ChannelRegistry",
    "get_channel",
    "list_channels",
    # Server
    "build_app",
    "run_server",
]


def get_channel(name: str, config=None) -> "BaseChannel":
    """
    Get an initialised channel adapter instance by name.

    The channel adapters are imported (and therefore registered) lazily the
    first time this function is called.

    Args:
        name:   Channel identifier, e.g. ``"dingtalk"``, ``"qq"``.
        config: Phoenix ``Config`` object.  Uses global config when *None*.

    Returns:
        Initialised :class:`BaseChannel` subclass instance.

    Raises:
        KeyError: If no channel with the given name is registered.
    """
    # Ensure adapters are registered before lookup
    from phoenix_agent.channels.server import _import_channel_adapters
    _import_channel_adapters()
    return ChannelRegistry.get_instance().create(name, config=config)


def list_channels() -> list:
    """Return the names of all currently-registered channel adapters."""
    from phoenix_agent.channels.server import _import_channel_adapters
    _import_channel_adapters()
    return ChannelRegistry.get_instance().list_channels()

