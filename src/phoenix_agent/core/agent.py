"""
Agent Core Module

The main Phoenix Agent implementation that orchestrates:
- LLM communication
- Tool execution
- State management
- Conversation flow
- Skill management
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Callable, Iterator, TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from phoenix_agent.core.config import Config, get_config
from phoenix_agent.core.message import Message, Role, MessageHistory
from phoenix_agent.core.state import SessionState, MemoryStore
from phoenix_agent.providers.base import LLMResponse
from phoenix_agent.providers.openai import create_provider
from phoenix_agent.tools.registry import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from phoenix_agent.skills.skill import Skill

logger = logging.getLogger(__name__)


class AgentCancelled(Exception):
    """Raised when the agent is cancelled mid-execution by an external signal."""
    pass


class Agent:
    """
    Main Phoenix Agent class.

    Orchestrates the conversation loop between user, LLM, and tools.

    Example:
        agent = Agent()
        response = agent.run("What is the capital of France?")
        print(response)
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ):
        """
        Initialize the Phoenix Agent.

        Args:
            config: Configuration object. Uses default if not provided.
            session_id: Optional session ID for state persistence.
            system_prompt: Optional system prompt override.
        """
        self.config = config or get_config()
        self.session = SessionState(session_id=session_id)

        # System prompt
        self.system_prompt = system_prompt or self.config.agent.system_prompt

        # Initialize LLM provider
        self._init_provider()

        # Tool registry
        self.tools = ToolRegistry.get_instance()

        # Message history
        self.history = MessageHistory()

        # Memory system (cross-session persistent knowledge)
        self.memory: Optional[MemoryStore] = None
        if self.config.agent.memory_enabled:
            try:
                from phoenix_agent.core.state import Database
                db = Database(self.config.storage.db_path)
                self.memory = MemoryStore(db)
                logger.info("Memory system enabled (%d memories loaded)", self.memory.count())
            except Exception as exc:
                logger.warning("Failed to initialise memory system: %s", exc)

        # Iteration tracking
        self.iteration_count = 0
        self.max_iterations = self.config.agent.max_iterations

        # Token usage tracking (accumulated across iterations in one run)
        self.accumulated_tokens: Dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.last_token_usage: Optional[Dict[str, int]] = None  # Last LLM call only

        # Plan mode: analyze without executing tools
        self.plan_mode: bool = self.config.agent.plan_mode

        # Multi-model support: track current model name and available models
        self._current_model_name: str = "default"
        self._model_configs: Dict[str, Any] = {"default": {
            "type": self.config.provider.type,
            "model": self.config.provider.model,
            "api_key": self.config.provider.api_key or "",
            "base_url": self.config.provider.base_url,
            "timeout": self.config.provider.timeout,
        }}
        # Register named models from config
        for mc in self.config.agent.models:
            self._model_configs[mc.name] = {
                "type": mc.type,
                "model": mc.model,
                "api_key": mc.api_key or self.config.provider.api_key or "",
                "base_url": mc.base_url or self.config.provider.base_url,
                "timeout": mc.timeout,
                "description": mc.description,
            }

        # Skill support
        self._active_skill: Optional["Skill"] = None
        self._skill_registry = None  # lazy-init

        # Callbacks
        self.on_tool_call: Optional[Callable] = None      # fired AFTER tool completes
        self.on_tool_start: Optional[Callable] = None     # fired BEFORE tool starts
        self.on_iteration: Optional[Callable] = None      # fired at each iteration (LLM call)
        self.on_response: Optional[Callable] = None
        self.on_skill_change: Optional[Callable] = None

        logger.info("Phoenix Agent initialized with model: %s", self.config.provider.model)

    def _init_provider(self) -> None:
        """Initialize the LLM provider from config."""
        provider_config = {
            "api_key": self.config.provider.api_key or "",
            "model": self.config.provider.model,
            "base_url": self.config.provider.base_url,
            "timeout": self.config.provider.timeout,
        }

        self.provider = create_provider(self.config.provider.type, provider_config)

        # Validate provider
        errors = self.provider.validate_config()
        if errors:
            logger.warning("Provider configuration issues: %s", errors)

    # ==================================================================
    # Multi-model switching
    # ==================================================================

    def list_models(self) -> List[Dict[str, str]]:
        """
        List all available model configurations.

        Returns:
            List of dicts with keys: name, model, type, description, is_active.
        """
        result = []
        for name, cfg in self._model_configs.items():
            result.append({
                "name": name,
                "model": cfg.get("model", ""),
                "type": cfg.get("type", ""),
                "description": cfg.get("description", ""),
                "is_active": (name == self._current_model_name),
            })
        return result

    def switch_model(self, model_name: str) -> bool:
        """
        Switch to a named model configuration.

        If *model_name* is not found in the registered configs, this method
        tries to match it against any config whose ``model`` field equals
        *model_name* (useful for switching to a discovered model by ID
        without explicitly registering it first).

        Args:
            model_name: Name of the model to switch to (as defined in
                        config.yaml ``agent.models``, ``"default"``, or
                        a raw model ID that matches a config's model field).

        Returns:
            True if the switch succeeded, False if the model name was not found.
        """
        if model_name in self._model_configs:
            cfg = self._model_configs[model_name]
        else:
            # Try to find a config whose model ID matches
            matched = None
            for name, cfg in self._model_configs.items():
                if cfg.get("model") == model_name:
                    matched = name
                    break
            if matched is None:
                logger.warning("Unknown model name: %s (available: %s)",
                               model_name, list(self._model_configs.keys()))
                return False
            model_name = matched
            cfg = self._model_configs[model_name]

        try:
            self.provider = create_provider(cfg["type"], cfg)
            self._current_model_name = model_name
            logger.info("Switched to model '%s' (%s / %s)",
                        model_name, cfg["type"], cfg.get("model", ""))
            return True
        except Exception as exc:
            logger.error("Failed to switch to model '%s': %s", model_name, exc)
            return False

    @property
    def current_model(self) -> str:
        """Return the name of the currently active model."""
        return self._current_model_name

    def discover_models(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        provider_type: str = "openai",
    ) -> List[Dict[str, str]]:
        """
        Discover available models from an OpenAI-compatible endpoint.

        Calls ``GET {base_url}/v1/models`` and returns the list of models.
        Discovered models are NOT automatically registered — the caller
        should pick one and call :meth:`register_model` or :meth:`switch_model`
        to use it.

        Args:
            base_url: Base URL of the service (e.g. ``http://localhost:8080``).
            api_key: Optional API key for the endpoint.
            provider_type: Provider type (default ``"openai"``).

        Returns:
            List of dicts with keys: ``id``, ``object``, ``owned_by``,
            plus ``base_url``, ``api_key``, ``provider_type`` for convenience.
            Empty list on failure.
        """
        import urllib.request
        import urllib.error
        import ssl

        # Normalize base_url — strip trailing slash
        base_url = base_url.rstrip("/")

        # Build the models endpoint URL
        # OpenAI-compatible APIs expose /v1/models
        models_url = base_url + "/v1/models"

        logger.info("Discovering models from %s", models_url)

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(models_url, headers=headers)

        try:
            # Allow self-signed certs for local deployments
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            models_list = data.get("data", [])
            result = []
            for m in models_list:
                model_id = m.get("id", "")
                if model_id:
                    result.append({
                        "id": model_id,
                        "object": m.get("object", "model"),
                        "owned_by": m.get("owned_by", ""),
                        "base_url": base_url,
                        "api_key": api_key or "",
                        "provider_type": provider_type,
                    })

            logger.info("Discovered %d models from %s", len(result), base_url)
            return result

        except urllib.error.URLError as exc:
            logger.error("Failed to connect to %s: %s", models_url, exc)
            return []
        except Exception as exc:
            logger.error("Model discovery failed: %s", exc)
            return []

    def register_model(
        self,
        name: str,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        provider_type: Optional[str] = None,
        description: str = "",
    ) -> bool:
        """
        Register a discovered model for later switching.

        Args:
            name: Unique name for the model entry.
            model: Model identifier (e.g. ``deepseek-chat``).
            base_url: Optional base URL override.
            api_key: Optional API key override.
            provider_type: Optional provider type override.
            description: Human-readable description.

        Returns:
            True if registration succeeded.
        """
        if name in self._model_configs:
            logger.warning("Model name '%s' already exists, updating", name)

        self._model_configs[name] = {
            "type": provider_type or self.config.provider.type,
            "model": model,
            "api_key": api_key or self.config.provider.api_key or "",
            "base_url": base_url or self.config.provider.base_url,
            "timeout": self.config.provider.timeout,
            "description": description,
        }
        logger.info("Registered model '%s' -> %s (%s)", name, model, provider_type)
        return True

    # ==================================================================
    # Plan mode
    # ==================================================================

    def set_plan_mode(self, enabled: bool) -> None:
        """
        Enable or disable plan mode.

        In plan mode, the agent analyzes the task and produces a structured
        plan but does NOT execute any tools.
        """
        self.plan_mode = enabled
        logger.info("Plan mode %s", "enabled" if enabled else "disabled")

    def plan(self, user_input: str) -> str:
        """
        Run a single-turn plan-only analysis.

        The agent receives the user's request and produces a plan without
        executing any tools.  This is a convenience wrapper that temporarily
        forces plan_mode for one turn.

        Args:
            user_input: The task or question to analyze.

        Returns:
            The agent's plan as a text string.
        """
        was_plan_mode = self.plan_mode
        self.plan_mode = True
        try:
            return self.run(user_input, stream=False)
        finally:
            self.plan_mode = was_plan_mode

    def run(
        self,
        user_input: str,
        stream: Optional[bool] = None,
        max_iterations: Optional[int] = None,
        images: Optional[List[Dict[str, Any]]] = None,
        cancel_event: Optional["threading.Event"] = None,
    ) -> str:
        """
        Run a conversation turn with the agent.

        Args:
            user_input: User message.
            stream: Whether to stream output (default from config).
            max_iterations: Override max tool call iterations.
            images: Optional list of image dicts for multimodal input.
                    Each: {"path": str}, {"url": str}, or {"base64": str, "mime": str}.
            cancel_event: Optional threading.Event. If set, the agent loop
                          checks ``is_set()`` before each LLM call and tool
                          execution and raises ``AgentCancelled`` if True.

        Returns:
            Final text response from the agent.
        """
        if stream is None:
            stream = self.config.agent.stream

        if max_iterations is not None:
            self.max_iterations = max_iterations

        # Reset iteration counter for this turn
        self.iteration_count = 0

        # Auto-match skill if none is active
        if not self._active_skill:
            try:
                registry = self._get_skill_registry()
                registry.discover()
                matched = registry.match(user_input)
                if matched:
                    self.use_skill(matched)
            except Exception:
                logger.debug("Skill auto-match failed, continuing without skill", exc_info=True)

        # Add user message to history
        user_msg = Message.user(user_input, images=images)
        self.history.add_message(user_msg)
        self.session.add_message(role="user", content=user_input)

        # Build system message (skill-aware)
        effective_prompt = self._build_effective_system_prompt()
        system_msg = Message.system(effective_prompt)

        # Get tool definitions (skill-aware)
        enabled_tools = self._build_effective_tool_filter()
        tool_definitions = self.tools.get_definitions(
            enabled=enabled_tools if enabled_tools else None,
            disabled=self.config.tools.disabled,
        )

        # --- Calculate token budget for history (reserve overhead) -----------
        overhead_tokens = self._estimate_overhead_tokens(
            system_prompt=effective_prompt,
            tool_definitions=tool_definitions,
        )
        history_budget = self._resolve_history_budget(overhead_tokens)

        # Build truncated history (respect context limits)
        history_messages = self.history.get_truncated_messages(
            max_messages=self.config.agent.max_history_messages,
            max_tokens=history_budget,
        )
        messages_for_api = (
            [system_msg.to_dict()]
            + [msg.to_dict() for msg in history_messages]
        )

        # Main agent loop
        final_response = ""
        context_retry_count = 0  # Track context-exceeded retries

        while self.iteration_count < self.max_iterations:
            try:
                # --- Check cancellation before each iteration ---
                if cancel_event and cancel_event.is_set():
                    raise AgentCancelled("Agent cancelled by user")
                # --- Fire iteration callback (UI progress) ---
                if self.on_iteration:
                    try:
                        self.on_iteration(
                            iteration=self.iteration_count + 1,
                            max_iterations=self.max_iterations,
                        )
                    except Exception:
                        pass

                # Make LLM call
                response = self.provider.complete(
                    messages=messages_for_api,
                    tools=tool_definitions if tool_definitions else None,
                    temperature=self.config.agent.temperature,
                    max_tokens=self.config.provider.max_tokens,
                )

                # --- Accumulate token usage --------------------------------
                if response.usage:
                    self.last_token_usage = response.usage
                    self.accumulated_tokens["prompt_tokens"] += response.usage.get("prompt_tokens", 0)
                    self.accumulated_tokens["completion_tokens"] += response.usage.get("completion_tokens", 0)
                    self.accumulated_tokens["total_tokens"] += response.usage.get("total_tokens", 0)
                    logger.debug(
                        "Token usage: prompt=%d, completion=%d, total=%d (accumulated: %d)",
                        response.usage.get("prompt_tokens", 0),
                        response.usage.get("completion_tokens", 0),
                        response.usage.get("total_tokens", 0),
                        self.accumulated_tokens["total_tokens"],
                    )

                # ----------------------------------------------------------------
                # Build the assistant message dict to append to messages_for_api.
                # OpenAI protocol requires that when the assistant returns tool
                # calls, the subsequent tool-result messages are preceded by the
                # assistant message that contained those tool_calls.  If we omit
                # it, the API raises a validation error and tool calling breaks.
                # ----------------------------------------------------------------
                if response.tool_calls:
                    # Assistant message with tool_calls (may have empty content)
                    assistant_api_msg: Dict[str, Any] = {
                        "role": "assistant",
                        "content": response.content or None,
                        "tool_calls": response.tool_calls,
                    }
                else:
                    assistant_api_msg = {
                        "role": "assistant",
                        "content": response.content or "",
                    }

                # Append assistant turn to the running messages list
                messages_for_api.append(assistant_api_msg)

                # Handle streaming display
                if stream and response.content:
                    for chunk in self._stream_response(response):
                        print(chunk, end="", flush=True)
                        final_response += chunk
                    print()  # Newline after streaming
                else:
                    if response.content:
                        final_response = response.content

                # Store in internal history
                assistant_msg = Message.assistant(content=response.content or "")
                self.history.add_message(assistant_msg)
                self.session.add_message(role="assistant", content=response.content or "")

                # Check for tool calls
                if response.has_tool_calls():
                    # In plan mode, don't execute tools — return the plan text
                    if self.plan_mode:
                        # Build a plan summary instead of executing
                        tool_names = [tc.get("function", {}).get("name", "unknown")
                                      for tc in (response.tool_calls or [])]
                        plan_note = (
                            f"\n\n> **[Plan Mode]** The following tools would be used: "
                            f"{', '.join(tool_names)}. "
                            f"Disable plan mode to execute."
                        )
                        final_response = (response.content or "") + plan_note
                        # Store plan message
                        plan_msg = Message.assistant(content=final_response)
                        self.history.add_message(plan_msg)
                        self.session.add_message(role="assistant", content=final_response)
                        break

                    tool_result_msgs = self._execute_tool_calls(response.tool_calls, cancel_event=cancel_event)
                    # Append tool results AFTER the assistant message (already done above)
                    messages_for_api.extend(tool_result_msgs)
                    self.iteration_count += 1
                else:
                    # No more tool calls — agent is done
                    break

            except AgentCancelled:
                logger.info("Agent task cancelled")
                # Remove the orphaned user message from history
                if self.history.messages and self.history.messages[-1].role == Role.USER:
                    self.history.messages.pop()
                return "[任务已取消]"

            except Exception as e:
                err_str = str(e).lower()
                # --- Handle context window exceeded gracefully ----------------
                if context_retry_count < 2 and (
                    "context" in err_str and ("window" in err_str or "exceeded" in err_str or "too long" in err_str)
                    or "input_tokens" in err_str
                    or "badrequesterror" in err_str
                ):
                    context_retry_count += 1
                    logger.warning(
                        "Context window exceeded (attempt %d/2), trimming history aggressively",
                        context_retry_count,
                    )
                    # Cut history budget by 50% each retry
                    new_budget = max(history_budget // 2, 1000)
                    history_messages = self.history.get_truncated_messages(
                        max_messages=self.config.agent.max_history_messages,
                        max_tokens=new_budget,
                    )
                    messages_for_api = (
                        [system_msg.to_dict()]
                        + [msg.to_dict() for msg in history_messages]
                    )
                    # Also rebuild tool definitions in case they contributed
                    # (tools are fixed size, but rebuild for consistency)
                    continue  # Retry the LLM call with trimmed context

                logger.exception("Error in agent loop")
                final_response = "I encountered an internal error while processing your request. Please try again."
                break

        return final_response

    def get_token_usage(self) -> Dict[str, int]:
        """
        Return accumulated token usage for the last run() call.

        Returns:
            Dict with keys: prompt_tokens, completion_tokens, total_tokens.
        """
        return dict(self.accumulated_tokens)

    def get_last_call_usage(self) -> Optional[Dict[str, int]]:
        """
        Return token usage for the LAST LLM call only (not accumulated).

        Returns:
            Dict with keys: prompt_tokens, completion_tokens, total_tokens.
            None if no LLM call has been made yet.
        """
        return self.last_token_usage

    def reset_token_usage(self):
        """Reset accumulated token counters (call before run() if you want per-turn stats)."""
        self.accumulated_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_token_usage = None

    # ==================================================================
    # Context window management
    # ==================================================================

    # Default model context window sizes (tokens) for common models.
    # Used when max_context_tokens=0 (auto) and no explicit config.
    _DEFAULT_CONTEXT_WINDOWS: Dict[str, int] = {
        "qwen3": 131072,
        "qwen3.5": 131072,
        "qwen2.5": 131072,
        "deepseek": 65536,
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
        "gpt-4-turbo": 128000,
        "gpt-4": 8192,
        "gpt-3.5": 16385,
        "claude-3": 200000,
        "claude-3.5": 200000,
        "claude-sonnet": 200000,
        "claude-opus": 200000,
    }

    def _get_model_context_window(self) -> int:
        """Guess the current model's context window size from the model name."""
        model_id = self._model_configs.get("default", {}).get("model", "")
        model_lower = model_id.lower()
        # Try exact prefix match
        for prefix, window in self._DEFAULT_CONTEXT_WINDOWS.items():
            if prefix in model_lower:
                return window
        # Fallback: 128k is a safe default for modern models
        return 128000

    def _estimate_overhead_tokens(
        self,
        system_prompt: str,
        tool_definitions: Optional[List[Dict[str, Any]]],
    ) -> int:
        """
        Estimate the token cost of system prompt + tool definitions.

        These are sent every turn and must be reserved from the history budget.
        """
        total = MessageHistory.estimate_tokens(system_prompt) + 4
        if tool_definitions:
            import json
            tools_json = json.dumps(tool_definitions, ensure_ascii=False)
            total += MessageHistory.estimate_tokens(tools_json) + 4
        # Reserve space for output tokens
        output_reserve = self.config.provider.max_tokens or 4096
        return total + output_reserve

    def _resolve_history_budget(self, overhead_tokens: int) -> Optional[int]:
        """
        Resolve the effective token budget for conversation history.

        - If max_context_tokens > 0: use that value directly
        - If max_context_tokens is 0, None, or not set: calculate as 70% of
          model context window, minus overhead
        - Returns None only if we truly can't determine any window (fallback: 128k)
        """
        configured = self.config.agent.max_context_tokens
        if configured and isinstance(configured, (int, float)) and configured > 0:
            # User explicitly set a budget — use it as-is (they control their own overhead)
            return int(configured)

        # Auto mode: use 70% of model context window, minus overhead
        window = self._get_model_context_window()
        budget = int(window * 0.70) - overhead_tokens
        if budget < 2000:
            budget = 2000  # Minimum usable budget
        logger.debug(
            "Auto history budget: model_window=%d, overhead=%d, budget=%d",
            window, overhead_tokens, budget,
        )
        return budget

    def _stream_response(self, response: LLMResponse) -> Iterator[str]:
        """
        Stream a response to the user.

        Args:
            response: LLMResponse object.

        Yields:
            Text chunks.
        """
        # For now, just yield the full content
        # In production, implement actual streaming
        if response.content:
            yield response.content

    def _execute_tool_calls(self, tool_calls: List[Dict[str, Any]], cancel_event=None) -> List[Dict[str, Any]]:
        """
        Execute tool calls from the LLM and return tool-result messages.

        Each tool call:
          1. Parses the LLM-supplied JSON arguments
          2. Dispatches to the registered tool handler
          3. Fires the on_tool_call callback (for UI progress updates)
          4. Appends the result to conversation history
          5. Returns an OpenAI-format tool-result message for the next API call

        Args:
            tool_calls: List of tool call dicts as returned by the LLM.

        Returns:
            List of ``{"role": "tool", ...}`` message dicts ready for the API.
        """
        results = []

        for tc in tool_calls:
            func_info     = tc.get("function", {})
            tool_name     = func_info.get("name", "")
            tool_args_raw = func_info.get("arguments", "{}")
            tool_call_id  = tc.get("id", "")

            logger.info("Executing tool: %s", tool_name)

            # --- Check cancellation before executing each tool ----------
            if cancel_event and cancel_event.is_set():
                raise AgentCancelled("Agent cancelled before tool execution")

            # --- Parse arguments ------------------------------------------
            # The LLM always sends arguments as a JSON-encoded string.
            try:
                if isinstance(tool_args_raw, str):
                    tool_args = json.loads(tool_args_raw)
                elif isinstance(tool_args_raw, dict):
                    tool_args = tool_args_raw
                else:
                    tool_args = {}
            except json.JSONDecodeError:
                logger.warning("Could not parse tool arguments for %s: %r",
                               tool_name, tool_args_raw)
                tool_args = {}

            # --- Fire on_tool_start callback (UI progress) -----------------
            if self.on_tool_start:
                try:
                    self.on_tool_start(tool_name, tool_args)
                except Exception:
                    pass

            # --- Execute tool ---------------------------------------------
            try:
                tool_result = self.tools.execute(
                    name=tool_name,
                    arguments=tool_args,
                    sandbox_path=self.config.tools.sandbox_path,
                    allow_destructive=self.config.tools.allow_destructive,
                )
            except Exception as exc:
                # Wrap unexpected execution errors as a ToolResult so the
                # LLM can reason about the failure.
                logger.exception("Unexpected error executing tool %s", tool_name)
                from phoenix_agent.tools.registry import ToolResult as TR
                tool_result = TR(success=False, content="",
                                 error=f"Internal error: tool '{tool_name}' failed unexpectedly")

            # --- Fire callback (UI progress) ------------------------------
            if self.on_tool_call:
                try:
                    self.on_tool_call(tool_name, tool_args, tool_result)
                except Exception:
                    pass  # Never let callback errors break the agent loop

            # --- Serialise result for the API -----------------------------
            # Tool content must be a plain string for the OpenAI API.
            # Truncate excessively large results to avoid context overflow.
            result_content = tool_result.to_json()
            MAX_TOOL_RESULT_CHARS = 30000  # ~7500 tokens, safe per-message limit
            if len(result_content) > MAX_TOOL_RESULT_CHARS:
                truncated_len = len(result_content) - MAX_TOOL_RESULT_CHARS
                result_content = (
                    result_content[:MAX_TOOL_RESULT_CHARS]
                    + f"\n\n... [TRUNCATED: {truncated_len} more characters omitted]"
                )
                logger.info(
                    "Tool result for '%s' truncated from %d to %d chars",
                    tool_name, len(result_content) + truncated_len, len(result_content),
                )

            # --- Store in history -----------------------------------------
            tool_msg = Message.tool(
                content=result_content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
            self.history.add_message(tool_msg)
            self.session.add_message(
                role="tool",
                content=result_content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )

            results.append(tool_msg.to_dict())

        return results

    # ==================================================================
    # Skill management
    # ==================================================================

    def _get_skill_registry(self):
        """Lazily import and return the SkillRegistry singleton."""
        if self._skill_registry is None:
            from phoenix_agent.skills.registry import SkillRegistry
            self._skill_registry = SkillRegistry.get_instance()
        return self._skill_registry

    def use_skill(self, skill: "Skill") -> None:
        """
        Activate a skill on this agent.

        When active, the skill's system prompt is prepended to the base
        prompt and any skill-specific tools become available.

        Args:
            skill: A ``Skill`` instance (must already be loaded).
        """
        if not skill.is_loaded:
            skill.load()

        self._active_skill = skill
        logger.info("Agent now using skill: %s", skill.name)

        # Fire callback
        if self.on_skill_change:
            try:
                self.on_skill_change(skill.name, "activated")
            except Exception:
                pass

    def clear_skill(self) -> Optional[str]:
        """
        Deactivate the current skill.

        Returns:
            Name of the deactivated skill, or ``None`` if no skill was active.
        """
        if not self._active_skill:
            return None

        name = self._active_skill.name
        self._active_skill = None
        logger.info("Skill deactivated: %s", name)

        if self.on_skill_change:
            try:
                self.on_skill_change(name, "deactivated")
            except Exception:
                pass

        return name

    @property
    def active_skill(self) -> Optional["Skill"]:
        """Return the currently active skill, if any."""
        return self._active_skill

    def _build_effective_system_prompt(self) -> str:
        """Build the system prompt, incorporating memory and active skill if set."""
        base = self.system_prompt

        # Inject current date/time block (critical for temporal questions)
        from datetime import datetime
        now = datetime.now()
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")
        time_block = (
            f"\n\n# Current Time (auto-injected)\n"
            f"- Date: {date_str} ({weekday_names[now.weekday()]} / {weekday_cn[now.weekday()]})\n"
            f"- Time: {time_str}\n"
            f"- IMPORTANT: For any question about today's date, day of week, time, or temporal context, "
            f"use the above values directly. Do NOT guess or rely on training data."
        )
        base = base + time_block

        # Inject memory context block
        if self.memory and self.memory.count() > 0:
            mem_block = self.memory.build_context_block()
            if mem_block:
                base = f"{base}\n\n{mem_block}"

        # Inject active skill prompt
        if self._active_skill and self._active_skill.system_prompt:
            skill_prompt = self._active_skill.system_prompt
            return (
                f"{base}\n\n"
                f"# Active Skill: {self._active_skill.name}\n\n"
                f"{skill_prompt}"
            )
        return base

    def _build_effective_tool_filter(self) -> List[str]:
        """Build the tool filter list, incorporating skill tool requirements."""
        enabled = list(self.config.tools.enabled) if self.config.tools.enabled else []

        if self._active_skill:
            # Ensure skill-required tools are enabled
            for tool_name in self._active_skill.manifest.tools:
                if tool_name not in enabled:
                    enabled.append(tool_name)
            # Also enable skill-specific tools by name prefix
            for tool_extra in self._active_skill.manifest.tools_extra:
                skill_tool_name = f"{self._active_skill.name}."
                # The registered tool names are like "skillname.funcname"
                # We need to pass them explicitly so get_definitions includes them
                if ":" in tool_extra:
                    _, func_name = tool_extra.rsplit(":", 1)
                    full_name = f"{self._active_skill.name}.{func_name}"
                    if full_name not in enabled:
                        enabled.append(full_name)

        return enabled

    def reset(self) -> None:
        """Reset the agent state for a new conversation."""
        self.history.clear()
        self.session.clear()
        self.iteration_count = 0
        logger.info("Agent state reset")

    def new_session(self) -> str:
        """
        Start a fresh session while preserving memory.

        Unlike reset() which clears the current session's messages,
        this creates a brand new session.  Cross-session memories
        are always retained.

        Returns:
            The new session ID.
        """
        old_id = self.session.session_id
        self.history.clear()
        self.iteration_count = 0
        self.session = SessionState(db=self.session.db)
        logger.info("New session started (old=%s, new=%s)", old_id[:8], self.session.session_id[:8])
        return self.session.session_id

    def end(self) -> None:
        """End the current session."""
        self.session.end()
        logger.info("Session ended: %s", self.session.session_id)

    def get_history(self) -> List[Message]:
        """Get the conversation history."""
        return self.history.messages.copy()

    def __repr__(self) -> str:
        return (f"PhoenixAgent(model={self._current_model_name}/{self.config.provider.model}, "
                f"plan={self.plan_mode}, session={self.session.session_id[:8]}...)")
