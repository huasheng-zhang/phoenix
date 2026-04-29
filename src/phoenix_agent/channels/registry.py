"""
Channel Registry

Discovers and stores channel adapters.  All built-in channels are auto-registered
at import time; third-party channels can register themselves via
``ChannelRegistry.get_instance().register(MyChannel)``.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, List, Optional, Type

from phoenix_agent.channels.base import BaseChannel

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Singleton registry that maps channel names to channel classes."""

    _instance: Optional["ChannelRegistry"] = None

    def __init__(self) -> None:
        self._channels: Dict[str, Type[BaseChannel]] = {}
        self._instances: Dict[str, BaseChannel] = {}
        self._load_builtin_channels()

    @classmethod
    def get_instance(cls) -> "ChannelRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton – useful in tests."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, channel_class: Type[BaseChannel]) -> None:
        """
        Register a channel adapter class.

        Args:
            channel_class: A subclass of ``BaseChannel`` with a non-empty ``NAME``.

        Raises:
            ValueError: If ``NAME`` is empty or the class is not a BaseChannel subclass.
        """
        if not issubclass(channel_class, BaseChannel):
            raise ValueError(f"{channel_class} must subclass BaseChannel")
        if not channel_class.NAME:
            raise ValueError(f"{channel_class} must set a non-empty NAME attribute")

        if channel_class.NAME in self._channels:
            logger.debug(
                "Channel %r already registered; overwriting with %s",
                channel_class.NAME,
                channel_class.__name__,
            )
        self._channels[channel_class.NAME] = channel_class
        logger.debug("Registered channel: %s (%s)", channel_class.NAME, channel_class.__name__)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    def create(self, name: str, config: Any = None) -> BaseChannel:
        """
        Instantiate a channel by name.

        Args:
            name:   Channel identifier as registered (e.g. "dingtalk").
            config: Phoenix Config object forwarded to the channel constructor.

        Returns:
            An initialised channel instance.

        Raises:
            KeyError: If no channel with that name is registered.
        """
        if name not in self._channels:
            available = ", ".join(sorted(self._channels))
            raise KeyError(
                f"Unknown channel {name!r}.  Available channels: {available or '(none)'}"
            )

        if config is None:
            from phoenix_agent.core.config import get_config
            config = get_config()

        return self._channels[name](config=config)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_channels(self) -> List[str]:
        """Return sorted list of registered channel names."""
        return sorted(self._channels)

    def get(self, name: str) -> Optional[Type[BaseChannel]]:
        """
        Return the channel class for *name*, or ``None`` if not registered.

        Unlike :meth:`get_class` this method never raises; it is safe to call
        when existence is uncertain.

        Args:
            name: Channel identifier (e.g. ``"dingtalk"``).

        Returns:
            The registered class, or ``None``.
        """
        return self._channels.get(name)

    def set_instance(self, name: str, instance: BaseChannel) -> None:
        """Store a running channel instance for later retrieval (e.g. by scheduler)."""
        self._instances[name] = instance

    def get_instance_channel(self, name: str) -> Optional[BaseChannel]:
        """Return the running channel instance, or None."""
        return self._instances.get(name)

    def get_class(self, name: str) -> Type[BaseChannel]:
        """Return the class registered for *name* (raises KeyError if missing)."""
        return self._channels[name]

    def __contains__(self, name: str) -> bool:
        return name in self._channels

    # ------------------------------------------------------------------
    # Built-in channel loader
    # ------------------------------------------------------------------

    _BUILTIN_MODULES = [
        "phoenix_agent.channels.dingtalk",
        "phoenix_agent.channels.wecom",
        "phoenix_agent.channels.wechat",
        "phoenix_agent.channels.qq",
    ]

    def _load_builtin_channels(self) -> None:
        """Import built-in channel modules so they self-register."""
        for module_path in self._BUILTIN_MODULES:
            try:
                importlib.import_module(module_path)
            except ImportError as exc:
                # Optional dependency missing (e.g. cryptography for WeCom)
                logger.debug(
                    "Could not load built-in channel %s: %s",
                    module_path,
                    exc,
                )
