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
class ModelConfig:
    """Configuration for a single named model entry."""

    name: str = ""            # unique identifier, e.g. "gpt4", "deepseek"
    type: str = "openai"      # openai, anthropic, openai-compatible
    model: str = ""           # actual model name, e.g. "gpt-4o"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_tokens: int = 8192
    timeout: int = 120
    description: str = ""     # human-readable description

    def __post_init__(self):
        """Resolve ${ENV_VAR} placeholders in api_key and base_url."""
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
    temperature: float = 0.2  # Lower = more deterministic (0.2 = stable, 0.7 = creative)
    max_retry: int = 3
    retry_delay: float = 1.0
    system_prompt: str = (
        "You are Phoenix, an autonomous AI agent that completes tasks by actively using tools.\n\n"
        "CURRENT TIME: The current date and time are automatically injected below this prompt.\n"
        "For ANY question about dates, days of week, time, or temporal context, ALWAYS use the\n"
        "injected time values. NEVER guess dates or days from training data — your knowledge is stale.\n\n"
        "CORE PRINCIPLE: When a user asks you to DO something, DO IT — don't just describe how.\n\n"
        "CONSISTENCY RULES (critical for stable output):\n"
        "- Always follow the same tool-calling pattern for the same task type.\n"
        "- For document processing (Excel/PDF/Word/PPT), ALWAYS use the SAME tool with the SAME parameters.\n"
        "- For image analysis, ALWAYS call analyze_image FIRST before answering.\n"
        "- Structure your final answer in the SAME format every time (see OUTPUT FORMAT below).\n"
        "- Do NOT change your approach between attempts — be deterministic.\n\n"
        "TOOL USAGE DISCIPLINE (critical — follow strictly):\n"
        "- ALWAYS call the appropriate tool before answering. Do NOT answer from memory.\n"
        "- For file operations: use read_file, write_file, list_directory, search_files\n"
        "- To run commands or scripts: use run_command (shell, python, git, etc.)\n"
        "- To fetch web content: use web_fetch\n"
        "- To search the web: use web_search\n"
        "- For math: use calculate\n"
        "- For image recognition/analysis: use analyze_image (requires vision-capable model)\n"
        "- For document processing, use the native tools directly:\n"
        "  - read_excel: Read .xlsx/.xls files as Markdown tables\n"
        "  - read_pdf: Extract text and tables from PDF files\n"
        "  - read_docx: Read .docx Word documents (paragraphs + tables)\n"
        "  - read_pptx: Read .pptx PowerPoint presentations (slides + notes)\n"
        "  - image_info: Get image metadata (dimensions, format, EXIF)\n"
        "  - For advanced analysis (Excel statistics, format conversion),\n"
        "    use the document-processor skill tools if available\n"
        "  - For large files, use pagination parameters (max_rows, pages, etc.)\n"
        "  - If dependencies are missing, the tool will tell you how to install them\n"
        "- NEVER say 'I can't do that' if a tool exists that could help. Try it.\n\n"
        "OUTPUT FORMAT (follow strictly for consistency):\n"
        "1. Brief summary of what you did (1-2 sentences)\n"
        "2. Main result (table, list, or formatted text)\n"
        "3. Key insights or next steps (if applicable)\n"
        "NEVER provide inconsistent output structure between similar tasks.\n\n"
        "WORKFLOW:\n"
        "1. Understand what the user wants\n"
        "2. Break it into steps\n"
        "3. Execute each step with the right tool\n"
        "4. Report results clearly\n"
        "Be direct. Take action. Report what you did and what the result was.\n\n"
        "SCHEDULED TASKS (cron-based automation):\n"
        "Your user can ask you to schedule tasks that run automatically at set times.\n"
        "Available tools:\n"
        "  list_scheduled_tasks       — Show all configured scheduled tasks (with next_run times)\n"
        "  add_scheduled_task         — Create a new scheduled task (takes effect immediately)\n"
        "  remove_scheduled_task      — Remove a task by name (takes effect immediately)\n\n"
        "Cron format (5 fields): minute hour day month weekday\n"
        "Examples:\n"
        "  '0 9 * * *'        — Every day at 09:00\n"
        "  '0 */2 * * *'      — Every 2 hours\n"
        "  '0 9 * * 1-5'      — Weekdays at 09:00\n"
        "  '30 8 * * *'       — Every day at 08:30\n\n"
        "When a user asks to schedule something:\n"
        "1. Ask for required info: task name, cron time, prompt content, channel, chat_id\n"
        "2. Confirm with the user before calling add_scheduled_task\n"
        "3. Changes take effect immediately — no server restart needed\n\n"
        "MULTI-AGENT COLLABORATION:\n"
        "You can delegate tasks to specialist agents for better results on complex tasks.\n"
        "Available tools:\n"
        "  list_agent_roles           — List all available specialist agent roles\n"
        "  delegate_to_agent          — Delegate a full task to a specialist (they can use tools)\n"
        "  ask_agent                  — Ask a specialist a quick question (no tool use)\n\n"
        "When to delegate:\n"
        "- A task clearly matches a specialist's expertise (e.g. research, coding, review)\n"
        "- You want a second opinion or cross-check from a different perspective\n"
        "- A task is complex enough to benefit from a focused specialist\n\n"
        "Workflow for delegation:\n"
        "1. Use list_agent_roles to see available specialists\n"
        "2. Use delegate_to_agent(role='specialist_name', task='...', context='...')\n"
        "3. Incorporate the specialist's result into your final response\n\n"
        "SKILLS:\n"
        "When a user's request matches a known skill (e.g. translation, code review, summarization,\n"
        "document processing), the skill is automatically activated and its prompt/tools are injected.\n"
        "You can also manually mention a skill name to activate it for a task."
    )
    stream: bool = True
    show_thinking: bool = False

    # Context window management
    max_history_messages: Optional[int] = None
    # Hard token budget for conversation history (excluding system prompt).
    # When set, old messages are trimmed to keep total tokens under this limit.
    # Uses a conservative estimate: ~1 token per 4 chars for English,
    # ~1 token per 1.5 chars for CJK.
    # Default: 0 means auto-calculate based on model context window (70% budget).
    max_context_tokens: int = 0

    # Memory system
    memory_enabled: bool = True
    # Max tokens for memory injection into system prompt (0 = unlimited)
    max_memory_tokens: int = 2000
    # Auto-save last N assistant messages as memories at session end (0 = disabled)
    auto_save_threshold: int = 0

    # Plan mode: when True, the agent analyzes and proposes a plan without
    # executing any tools.  The user can review and then switch plan_mode off
    # to let the agent execute.
    plan_mode: bool = False

    # Named model entries for multi-model switching.
    # Each entry is a ModelConfig with its own provider settings.
    models: List[ModelConfig] = field(default_factory=list)


@dataclass
class WebSearchConfig:
    """Configuration for the web search tool."""

    # Provider: "tavily" (built-in), "duckduckgo" (no API key needed), or "custom"
    provider: str = "tavily"

    # API key — Tavily: from env TAVILY_API_KEY or config; others: provider-specific
    api_key: Optional[str] = None

    # Max results per search
    max_results: int = 5

    # Search depth: "basic" or "advanced" (Tavily-specific)
    search_depth: str = "basic"

    # Custom search endpoint URL (for "custom" provider)
    custom_endpoint: Optional[str] = None

    # Custom search API key name (for "custom" provider)
    custom_api_key_name: str = "api_key"

    def __post_init__(self):
        """Resolve API key from environment variable."""
        # Tavily: check TAVILY_API_KEY env var
        if self.provider == "tavily":
            env_key = os.environ.get("TAVILY_API_KEY", "").strip()
            if env_key:
                self.api_key = env_key


@dataclass
class SchedulerTaskConfig:
    """Single scheduled task configuration."""

    name: str = ""
    cron: str = "0 9 * * *"
    prompt: str = ""
    channel: str = "dingtalk"
    chat_id: str = ""
    skill: Optional[str] = None
    enabled: bool = True
    timezone: str = "Asia/Shanghai"


@dataclass
class SchedulerConfig:
    """Scheduler configuration."""

    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    tasks: List[SchedulerTaskConfig] = field(default_factory=list)


@dataclass
class ToolConfig:
    """Configuration for the tool system."""

    # Default: enable all four built-in categories so the agent has full capability.
    # Users can narrow this in config.yaml or by passing disabled=[...].
    enabled: List[str] = field(default_factory=lambda: ["file", "web", "system", "utility"])
    disabled: List[str] = field(default_factory=list)
    sandbox_path: Optional[str] = None
    allow_destructive: bool = False  # Must be explicitly enabled for safety
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
        self.web_search = self._build_web_search_config()
        self.storage = self._build_storage_config()
        self.channels = self._build_channels_config()
        self.scheduler = self._build_scheduler_config()

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

        # --- Build named model entries ---
        raw_models = file_section.get("models", [])
        model_entries: List[ModelConfig] = []
        for m in raw_models:
            if not isinstance(m, dict) or not m.get("name"):
                continue
            model_entries.append(ModelConfig(
                name=m["name"],
                type=m.get("type", "openai"),
                model=m.get("model", ""),
                api_key=m.get("api_key"),
                base_url=m.get("base_url"),
                max_tokens=m.get("max_tokens", 8192),
                timeout=m.get("timeout", 120),
                description=m.get("description", ""),
            ))

        return AgentConfig(
            max_iterations=file_section.get("max_iterations", 50),
            temperature=file_section.get("temperature", 0.7),
            max_retry=file_section.get("max_retry", 3),
            retry_delay=file_section.get("retry_delay", 1.0),
            system_prompt=system_prompt,
            stream=file_section.get("stream", True),
            show_thinking=file_section.get("show_thinking", False),
            max_history_messages=file_section.get("max_history_messages"),
            max_context_tokens=file_section.get("max_context_tokens") or 0,
            memory_enabled=file_section.get("memory_enabled", True),
            max_memory_tokens=file_section.get("max_memory_tokens", 2000),
            auto_save_threshold=file_section.get("auto_save_threshold", 0),
            plan_mode=file_section.get("plan_mode", False),
            models=model_entries,
        )

    def _build_tool_config(self) -> ToolConfig:
        """Build tool configuration from file and env."""
        file_section = self._file_config.get("tools", {})

        return ToolConfig(
            enabled=file_section.get("enabled", ["file", "web", "system", "utility"]),
            disabled=file_section.get("disabled", []),
            sandbox_path=file_section.get("sandbox_path"),
            allow_destructive=file_section.get("allow_destructive", False),
            max_file_size=file_section.get("max_file_size", 10 * 1024 * 1024),
        )

    def _build_web_search_config(self) -> WebSearchConfig:
        """Build web search configuration from file and env."""
        file_section = self._file_config.get("web_search", {})

        return WebSearchConfig(
            provider=file_section.get("provider", "tavily"),
            api_key=file_section.get("api_key"),
            max_results=file_section.get("max_results", 5),
            search_depth=file_section.get("search_depth", "basic"),
            custom_endpoint=file_section.get("custom_endpoint"),
            custom_api_key_name=file_section.get("custom_api_key_name", "api_key"),
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

    def _build_scheduler_config(self) -> SchedulerConfig:
        """Build scheduler configuration from the ``scheduler:`` YAML section."""
        raw: Dict[str, Any] = self._file_config.get("scheduler", {})

        enabled = raw.get("enabled", False)
        timezone = raw.get("timezone", "Asia/Shanghai")
        task_dicts: List[Dict] = raw.get("tasks", [])

        tasks: List[SchedulerTaskConfig] = []
        for t in task_dicts:
            if not isinstance(t, dict):
                continue
            tasks.append(SchedulerTaskConfig(
                name=t.get("name", ""),
                cron=t.get("cron", "0 9 * * *"),
                prompt=t.get("prompt", ""),
                channel=t.get("channel", "dingtalk"),
                chat_id=t.get("chat_id", ""),
                skill=t.get("skill"),
                enabled=t.get("enabled", True),
                timezone=t.get("timezone", timezone),
            ))

        return SchedulerConfig(
            enabled=enabled,
            timezone=timezone,
            tasks=tasks,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Export configuration as dictionary (for debugging)."""
        return {
            "provider": asdict(self.provider),
            "agent": asdict(self.agent),
            "tools": asdict(self.tools),
            "web_search": asdict(self.web_search),
            "storage": asdict(self.storage),
            "channels": asdict(self.channels),
            "scheduler": asdict(self.scheduler),
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
