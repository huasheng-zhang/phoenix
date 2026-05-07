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

from phoenix_agent.core.config import Config, get_config
from phoenix_agent.core.message import Message, Role, MessageHistory
from phoenix_agent.core.state import SessionState, MemoryStore
from phoenix_agent.providers.base import LLMResponse
from phoenix_agent.providers.openai import create_provider
from phoenix_agent.tools.registry import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from phoenix_agent.skills.skill import Skill

logger = logging.getLogger(__name__)


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

    def run(
        self,
        user_input: str,
        stream: Optional[bool] = None,
        max_iterations: Optional[int] = None,
    ) -> str:
        """
        Run a conversation turn with the agent.

        Args:
            user_input: User message.
            stream: Whether to stream output (default from config).
            max_iterations: Override max tool call iterations.

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
        user_msg = Message.user(user_input)
        self.history.add_message(user_msg)
        self.session.add_message(role="user", content=user_input)

        # Build system message (skill-aware)
        effective_prompt = self._build_effective_system_prompt()
        system_msg = Message.system(effective_prompt)

        # Build truncated history (respect context limits)
        history_messages = self.history.get_truncated_messages(
            max_messages=self.config.agent.max_history_messages,
            max_tokens=self.config.agent.max_context_tokens,
        )
        messages_for_api = (
            [system_msg.to_dict()]
            + [msg.to_dict() for msg in history_messages]
        )

        # Get tool definitions (skill-aware)
        enabled_tools = self._build_effective_tool_filter()
        tool_definitions = self.tools.get_definitions(
            enabled=enabled_tools if enabled_tools else None,
            disabled=self.config.tools.disabled,
        )

        # Main agent loop
        final_response = ""

        while self.iteration_count < self.max_iterations:
            try:
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
                    tool_result_msgs = self._execute_tool_calls(response.tool_calls)
                    # Append tool results AFTER the assistant message (already done above)
                    messages_for_api.extend(tool_result_msgs)
                    self.iteration_count += 1
                else:
                    # No more tool calls — agent is done
                    break

            except Exception as e:
                logger.exception("Error in agent loop")
                final_response = "I encountered an internal error while processing your request. Please try again."
                break

        return final_response

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

    def _execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
            result_content = tool_result.to_json()

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
        return f"PhoenixAgent(model={self.config.provider.model}, session={self.session.session_id[:8]}...)"
