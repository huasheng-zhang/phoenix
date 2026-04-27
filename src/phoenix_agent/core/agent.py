"""
Agent Core Module

The main Phoenix Agent implementation that orchestrates:
- LLM communication
- Tool execution
- State management
- Conversation flow
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Callable, Iterator

from phoenix_agent.core.config import Config, get_config
from phoenix_agent.core.message import Message, Role, MessageHistory
from phoenix_agent.core.state import SessionState
from phoenix_agent.providers.base import LLMResponse
from phoenix_agent.providers.openai import create_provider
from phoenix_agent.tools.registry import ToolRegistry, ToolResult

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

        # Iteration tracking
        self.iteration_count = 0
        self.max_iterations = self.config.agent.max_iterations

        # Callbacks
        self.on_tool_call: Optional[Callable] = None
        self.on_response: Optional[Callable] = None

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

        # Add user message to history
        user_msg = Message.user(user_input)
        self.history.add_message(user_msg)
        self.session.add_message(role="user", content=user_input)

        # Build system message
        system_msg = Message.system(self.system_prompt)
        messages_for_api = [system_msg.to_dict()] + self.history.get_messages_for_api()

        # Get tool definitions
        enabled_tools = self.config.tools.enabled
        tool_definitions = self.tools.get_definitions(
            enabled=enabled_tools if enabled_tools else None,
            disabled=self.config.tools.disabled,
        )

        # Main agent loop
        final_response = ""

        while self.iteration_count < self.max_iterations:
            try:
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
                final_response = f"I encountered an error: {str(e)}"
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
                                 error=f"Internal error: {exc}")

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

    def reset(self) -> None:
        """Reset the agent state for a new conversation."""
        self.history.clear()
        self.session.clear()
        self.iteration_count = 0
        logger.info("Agent state reset")

    def end(self) -> None:
        """End the current session."""
        self.session.end()
        logger.info("Session ended: %s", self.session.session_id)

    def get_history(self) -> List[Message]:
        """Get the conversation history."""
        return self.history.messages.copy()

    def __repr__(self) -> str:
        return f"PhoenixAgent(model={self.config.provider.model}, session={self.session.session_id[:8]}...)"
