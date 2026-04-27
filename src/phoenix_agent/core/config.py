"""
Configuration Management Module

Phoenix Agent uses a layered configuration system:
1. Default values (lowest priority)
2. YAML configuration file (~/.phoenix/config.yaml)
3. Environment variables (highest priority)

Environment variables take precedence over file settings.
"""

import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from functools import lru_cache

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _get_phoenix_home() -> Path:
    """Get the Phoenix home directory, defaulting to ~/.phoenix."""
    env_path = os.environ.get("PHOENIX_HOME", "").strip()
    if env_path:
        path = Path(env_path)
    else:
        path = Path.home() / ".phoenix"

    # Ensure directory exists
    path.mkdir(parents=True, exist_ok=True)
    return path


@lru_cache(maxsize=1)
def _load_dotenv_cached() -> None:
    """Load environment variables from .env file (cached)."""
    phoenix_home = _get_phoenix_home()
    env_file = phoenix_home / ".env"

    # Also check current working directory
    cwd_env = Path.cwd() / ".env"

    loaded_files = []
    if env_file.exists():
        load_dotenv(env_file, override=False)
        loaded_files.append(str(env_file))
    if cwd_env.exists():
        load_dotenv(cwd_env, override=True)
        loaded_files.append(str(cwd_env))

    if loaded_files:
        logger.debug("Loaded environment from: %s", loaded_files)


@dataclass
class ProviderConfig:
    """Configuration for the LLM provider."""

    type: str = "openai"  # openai, anthropic, openai-compatible
    model: str = "gpt-4o"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_tokens: int = 8192
    timeout: int = 120

    def __post_init__(self):
        """Resolve environment variables and defaults after initialization."""
        # API key from env var takes precedence
        env_key = os.environ.get("PHOENIX_API_KEY", "").strip()
        if env_key:
            self.api_key = env_key

        # Base URL from env var
        env_url = os.environ.get("PHOENIX_BASE_URL", "").strip()
        if env_url:
            self.base_url = env_url

        # Model from env var
        env_model = os.environ.get("PHOENIX_MODEL", "").strip()
        if env_model:
            self.model = env_model

        # Resolve ${ENV_VAR} placeholders in config file values
        self._resolve_placeholders()

    def _resolve_placeholders(self) -> None:
        """Resolve ${ENV_VAR} style placeholders in string fields."""
        for field_name in ["api_key", "base_url"]:
            value = getattr(self, field_name, None)
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                env_var = value[2:-1]
                resolved = os.environ.get(env_var, "")
                setattr(self, field_name, resolved or None)


@dataclass
class AgentConfig:
    """Configuration for the agent behavior."""

    max_iterations: int = 50
    temperature: float = 0.7
    max_retry: int = 3
    retry_delay: float = 1.0
    system_prompt: str = (
        "You are Phoenix, an autonomous AI agent that completes tasks by actively using tools.\n\n"
        "CORE PRINCIPLE: When a user asks you to DO something, DO IT — don't just describe how.\n\n"
        "TOOL USAGE RULES:\n"
        "- To read or edit files: use read_file, write_file, list_directory, search_files\n"
        "- To run commands or scripts: use run_command (shell, python, git, etc.)\n"
        "- To fetch web content: use web_fetch\n"
        "- For math: use calculate\n"
        "- ALWAYS prefer calling a tool over answering from memory when the task involves the real world\n\n"
        "WORKFLOW:\n"
        "1. Understand what the user wants\n"
        "2. Break it into steps\n"
        "3. Execute each step with the right tool\n"
        "4. Report results clearly\n\n"
        "NEVER say 'I can't do that' if a tool exists that could help. Try it.\n"
        "Be direct. Take action. Report what you did and what the result was."
    )
    stream: bool = True
    show_thinking: bool = False

    # Context window management
    max_history_messages: Optional[int] = None
    # Hard token budget for conversation history (excluding system prompt).
    # When set, old messages are trimmed to keep total tokens under this limit.
    # Uses a conservative estimate: ~1 token per 4 chars for English,
    # ~1 token per 1.5 chars for CJK.  Default: None (no limit).
    max_context_tokens: Optional[int] = None


@dataclass
class ToolConfig:
    """Configuration for the tool system."""

    # Default: enable all four built-in categories so the agent has full capability.
    # Users can narrow this in config.yaml or by passing disabled=[...].
    enabled: List[str] = field(default_factory=lambda: ["file", "web", "system", "utility"])
    disabled: List[str] = field(default_factory=list)
    sandbox_path: Optional[str] = None
    allow_destructive: bool = True   # Needed for delete_file / run_command to work
    max_file_size: int = 10 * 1024 * 1024  # 10MB


@dataclass
class StorageConfig:
    """Configuration for data persistence."""

    db_path: Optional[str] = None
    history_dir: Optional[str] = None
    max_history: int = 100

    def __post_init__(self):
        """Set default paths relative to PHOENIX_HOME."""
        phoenix_home = _get_phoenix_home()

        if self.db_path is None:
            self.db_path = str(phoenix_home / "phoenix.db")

        if self.history_dir is None:
            self.history_dir = str(phoenix_home / "history")


@dataclass
class ChannelConfig:
    """
    Configuration for a single chat channel.

    Each channel's raw settings are stored in ``settings`` and interpreted
    by the corresponding channel adapter.  Common fields (``enabled``,
    ``webhook_path``) are surfaced here for convenience.
    """

    name: str = ""
    enabled: bool = False
    webhook_path: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelsConfig:
    """
    Top-level channels configuration block.

    Mirrors the ``channels:`` section in config.yaml::

        channels:
          server:
            host: "0.0.0.0"
            port: 8080
          dingtalk:
            enabled: true
            mode: webhook
            webhook_url: "https://..."
          wechat:
            enabled: true
            mode: wecom_webhook
            webhook_url: "https://..."
          qq:
            enabled: true
            api_url: "http://127.0.0.1:5700"
          telegram:
            enabled: true
            bot_token: "123456:ABC..."
    """

    # HTTP server settings for the webhook receiver
    host: str = "0.0.0.0"
    port: int = 8080

    # Per-channel configs keyed by channel name
    channels: Dict[str, ChannelConfig] = field(default_factory=dict)


class Config:
    """
    Main configuration class for Phoenix Agent.

    Loads configuration from YAML file and environment variables.
    Environment variables always take precedence over file settings.

    Example usage:
        config = Config()  # Load from default location
        config = Config(path="/path/to/config.yaml")  # Custom config file
    """

    def __init__(self, path: Optional[str] = None):
        """
        Initialize configuration from file and environment.

        Args:
            path: Optional path to configuration file.
                  Defaults to ~/.phoenix/config.yaml
        """
        _load_dotenv_cached()

        self.phoenix_home = _get_phoenix_home()

        # Determine config file path
        if path:
            self.config_file = Path(path)
        else:
            self.config_file = self.phoenix_home / "config.yaml"

        # Load from file
        self._file_config: Dict[str, Any] = {}
        if self.config_file.exists():
            self._load_from_file()

        # Build configuration objects
        self.provider = self._build_provider_config()
        self.agent = self._build_agent_config()
        self.tools = self._build_tool_config()
        self.storage = self._build_storage_config()
        self.channels = self._build_channels_config()

    def _load_from_file(self) -> None:
        """Load configuration from YAML file."""
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                self._file_config = yaml.safe_load(f) or {}
            logger.debug("Loaded config from: %s", self.config_file)
        except yaml.YAMLError as e:
            logger.warning("Failed to parse config file: %s", e)
            self._file_config = {}
        except IOError as e:
            logger.warning("Failed to read config file: %s", e)
            self._file_config = {}

    def _build_provider_config(self) -> ProviderConfig:
        """Build provider configuration from file and env."""
        file_section = self._file_config.get("provider", {})

        return ProviderConfig(
            type=file_section.get("type", "openai"),
            model=file_section.get("model", "gpt-4o"),
            api_key=file_section.get("api_key"),
            base_url=file_section.get("base_url"),
            max_tokens=file_section.get("max_tokens", 8192),
            timeout=file_section.get("timeout", 120),
        )

    def _build_agent_config(self) -> AgentConfig:
        """Build agent configuration from file and env."""
        file_section = self._file_config.get("agent", {})

        # System prompt from env var
        env_prompt = os.environ.get("PHOENIX_SYSTEM_PROMPT", "").strip()
        system_prompt = env_prompt or file_section.get(
            "system_prompt",
            AgentConfig().system_prompt
        )

        return AgentConfig(
            max_iterations=file_section.get("max_iterations", 50),
            temperature=file_section.get("temperature", 0.7),
            max_retry=file_section.get("max_retry", 3),
            retry_delay=file_section.get("retry_delay", 1.0),
            system_prompt=system_prompt,
            stream=file_section.get("stream", True),
            show_thinking=file_section.get("show_thinking", False),
        )

    def _build_tool_config(self) -> ToolConfig:
        """Build tool configuration from file and env."""
        file_section = self._file_config.get("tools", {})

        return ToolConfig(
            enabled=file_section.get("enabled", ["file", "web", "system", "utility"]),
            disabled=file_section.get("disabled", []),
            sandbox_path=file_section.get("sandbox_path"),
            allow_destructive=file_section.get("allow_destructive", True),
            max_file_size=file_section.get("max_file_size", 10 * 1024 * 1024),
        )

    def _build_storage_config(self) -> StorageConfig:
        """Build storage configuration from file and env."""
        file_section = self._file_config.get("storage", {})

        return StorageConfig(
            db_path=file_section.get("db_path"),
            history_dir=file_section.get("history_dir"),
            max_history=file_section.get("max_history", 100),
        )

    def _build_channels_config(self) -> ChannelsConfig:
        """
        Build channels configuration from the ``channels:`` YAML section.

        The ``channels:`` block has the layout::

            channels:
              server:
                host: "0.0.0.0"
                port: 8080
              dingtalk:
                enabled: true
                ...
              wechat:
                enabled: false
              qq:
                enabled: false
              telegram:
                enabled: false

        Each adapter's full settings dict is preserved in
        ``ChannelConfig.settings`` so the adapter can read any key it needs.
        """
        raw: Dict[str, Any] = self._file_config.get("channels", {})

        # HTTP server settings
        srv = raw.get("server", {})
        host: str = os.environ.get("PHOENIX_CHANNEL_HOST", "").strip() or srv.get("host", "0.0.0.0")
        port: int = int(os.environ.get("PHOENIX_CHANNEL_PORT", "") or srv.get("port", 8080))

        # Known channel names
        known_channels = ["dingtalk", "wechat", "qq", "telegram"]
        channel_cfgs: Dict[str, ChannelConfig] = {}

        for ch_name in known_channels:
            ch_raw: Dict[str, Any] = raw.get(ch_name, {})
            channel_cfgs[ch_name] = ChannelConfig(
                name=ch_name,
                enabled=ch_raw.get("enabled", False),
                webhook_path=ch_raw.get("webhook_path", f"/{ch_name}/webhook"),
                settings=ch_raw,   # pass the entire dict to the adapter
            )

        return ChannelsConfig(
            host=host,
            port=port,
            channels=channel_cfgs,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Export configuration as dictionary (for debugging)."""
        return {
            "provider": asdict(self.provider),
            "agent": asdict(self.agent),
            "tools": asdict(self.tools),
            "storage": asdict(self.storage),
            "channels": asdict(self.channels),
        }

    def validate(self) -> List[str]:
        """
        Validate configuration and return list of warnings.

        Returns:
            List of warning messages (empty if valid).
        """
        warnings = []

        # Check API key
        if not self.provider.api_key:
            warnings.append(
                "No API key configured. Set PHOENIX_API_KEY environment variable "
                "or api_key in config.yaml"
            )

        # Check model name
        if not self.provider.model:
            warnings.append("No model configured. Set PHOENIX_MODEL or model in config.yaml")

        # Check enabled tools
        if not self.tools.enabled:
            warnings.append("No tools enabled. Add tools to 'tools.enabled' in config.yaml")

        return warnings


# Global config instance (lazy loaded)
_global_config: Optional[Config] = None


def get_config(path: Optional[str] = None) -> Config:
    """
    Get the global configuration instance.

    Uses caching to avoid reloading config multiple times.

    Args:
        path: Optional path to configuration file.

    Returns:
        Config instance.
    """
    global _global_config

    if _global_config is None:
        _global_config = Config(path=path)

    return _global_config


def reset_config() -> None:
    """Reset the global configuration (useful for testing)."""
    global _global_config
    _global_config = None
