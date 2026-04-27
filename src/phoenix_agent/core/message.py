"""
Message Types Module

Defines the core message types used throughout Phoenix Agent.
Messages are the primary data structure for agent communication.
"""

import re
import time
import uuid
import logging
from typing import Optional, List, Dict, Any, Literal
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class Role(str, Enum):
    """Message role enumeration."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """
    Represents a single tool call requested by the model.

    Attributes:
        id: Unique identifier for this tool call.
        name: Name of the tool to invoke.
        arguments: Arguments to pass to the tool (as JSON string).
    """
    id: str
    name: str
    arguments: str  # JSON string of arguments

    def get_args_dict(self) -> Dict[str, Any]:
        """Parse arguments JSON string to dictionary."""
        import json
        try:
            return json.loads(self.arguments)
        except (json.JSONDecodeError, TypeError):
            return {}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        """Create ToolCall from dictionary (OpenAI format)."""
        func = data.get("function", {})
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            name=func.get("name", ""),
            arguments=func.get("arguments", "{}"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (OpenAI tool_call format)."""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            }
        }


@dataclass
class ContentBlock:
    """
    A single content block within a message.

    Supports text and tool_use types.
    """
    type: str  # "text" or "tool_use"
    text: Optional[str] = None
    tool_use: Optional[ToolCall] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API formatting."""
        if self.type == "text":
            return {"type": "text", "text": self.text or ""}
        elif self.type == "tool_use":
            return {
                "type": "tool_use",
                "id": self.tool_use.id if self.tool_use else "",
                "name": self.tool_use.name if self.tool_use else "",
                "input": self.tool_use.get_args_dict() if self.tool_use else {},
            }
        return {"type": self.type}

    @classmethod
    def text_block(cls, text: str) -> "ContentBlock":
        """Create a text content block."""
        return cls(type="text", text=text)

    @classmethod
    def tool_use_block(cls, tool_call: ToolCall) -> "ContentBlock":
        """Create a tool_use content block."""
        return cls(type="tool_use", tool_use=tool_call)


@dataclass
class Message:
    """
    Core message class for agent communication.

    Messages flow between user, assistant, and tools during a conversation.

    Attributes:
        role: Who is sending this message (system/user/assistant/tool).
        content: The message content (text or structured).
        tool_calls: Optional list of tool calls requested.
        tool_call_id: ID of the tool call this message is responding to.
        tool_name: Name of the tool that produced this message.
        name: Optional name for the sender (for tool messages).
        reasoning: Optional reasoning/thinking content.
        finish_reason: Why the message generation stopped.
        metadata: Additional metadata dictionary.
    """
    role: Role
    content: str = ""
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    name: Optional[str] = None
    reasoning: Optional[str] = None
    finish_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Auto-generated fields
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        """Ensure role is properly converted from string."""
        if isinstance(self.role, str):
            self.role = Role(self.role)

    def is_tool_message(self) -> bool:
        """Check if this is a tool response message."""
        return self.role == Role.TOOL or self.tool_call_id is not None

    def has_tool_calls(self) -> bool:
        """Check if this message contains tool call requests."""
        return bool(self.tool_calls)

    def get_text_content(self) -> str:
        """Extract text content, handling various formats."""
        if self.content:
            return self.content
        return ""

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert message to dictionary format.

        Returns OpenAI-compatible message format.
        """
        result: Dict[str, Any] = {"role": self.role.value}

        # Handle content
        if self.tool_calls:
            result["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
            if self.content:
                result["content"] = self.content
        else:
            result["content"] = self.content or ""

        # Tool-specific fields
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id

        if self.tool_name:
            result["tool_name"] = self.tool_name

        if self.name:
            result["name"] = self.name

        return result

    def to_openai_messages_format(self) -> List[Dict[str, Any]]:
        """
        Convert message to list of OpenAI message format.

        Handles multi-part content by splitting into separate messages.
        """
        if not self.tool_calls:
            return [self.to_dict()]

        # For assistant messages with tool calls, we need to:
        # 1. Send the assistant message with tool_calls
        # 2. The tool responses will be added as separate messages
        return [self.to_dict()]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """
        Create Message from dictionary.

        Handles both OpenAI and custom formats.
        """
        role = data.get("role", "user")
        if isinstance(role, str):
            role = Role(role)

        tool_calls = None
        if "tool_calls" in data and data["tool_calls"]:
            tool_calls = [
                ToolCall.from_dict(tc) if isinstance(tc, dict) else tc
                for tc in data["tool_calls"]
            ]

        content = data.get("content", "") or ""

        return cls(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            tool_name=data.get("tool_name"),
            name=data.get("name"),
            reasoning=data.get("reasoning"),
            finish_reason=data.get("finish_reason"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def system(cls, content: str) -> "Message":
        """Create a system message."""
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        """Create a user message."""
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Optional[List[ToolCall]] = None) -> "Message":
        """Create an assistant message."""
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, content: str, tool_call_id: str, tool_name: str) -> "Message":
        """Create a tool response message."""
        return cls(
            role=Role.TOOL,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )


@dataclass
class ConversationTurn:
    """
    Represents a single turn in a conversation.

    A turn consists of a user message followed by
    the assistant's response (which may include tool calls).
    """
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_message: Optional[Message] = None
    assistant_messages: List[Message] = field(default_factory=list)
    tool_messages: List[Message] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def is_complete(self) -> bool:
        """Check if this turn has a complete response."""
        return bool(self.assistant_messages)

    def add_assistant_message(self, message: Message) -> None:
        """Add an assistant message to this turn."""
        self.assistant_messages.append(message)

    def add_tool_message(self, message: Message) -> None:
        """Add a tool response message to this turn."""
        self.tool_messages.append(message)


class MessageHistory:
    """
    Manages the message history for a conversation.

    Provides utilities for building context and
    managing conversation state.
    """

    # Regex to detect CJK characters for token estimation.
    _CJK_RE = re.compile(
        r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
        r"\u3000-\u303f\uff00-\uffef\uac00-\ud7af]"
    )

    def __init__(self):
        self.messages: List[Message] = []
        self.turns: List[ConversationTurn] = []

    def add_message(self, message: Message) -> None:
        """Add a message to history."""
        self.messages.append(message)

        # Track turns
        if message.role == Role.USER:
            # Start a new turn
            turn = ConversationTurn(user_message=message)
            self.turns.append(turn)
        elif message.role == Role.ASSISTANT and self.turns:
            self.turns[-1].add_assistant_message(message)
        elif message.role == Role.TOOL and self.turns:
            self.turns[-1].add_tool_message(message)

    def get_messages_for_api(self) -> List[Dict[str, Any]]:
        """
        Get all messages in API-compatible format.

        Converts Message objects to dictionaries.
        """
        return [msg.to_dict() for msg in self.messages]

    def get_truncated_messages(
        self,
        max_messages: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ) -> List[Message]:
        """
        Return a truncated view of message history.

        Both limits apply independently — the stricter one wins:

        * **max_messages**: keep at most *max_messages* most-recent messages.
        * **max_tokens**: estimate token count of messages (old → new) and
          drop the oldest ones until the total stays under *max_tokens*.

        Truncation prefers to keep complete turns (user/assistant/tool groups)
        intact rather than cutting in the middle of a tool-call sequence.

        Args:
            max_messages: Maximum number of messages to retain.
            max_tokens: Maximum estimated token budget.

        Returns:
            A (possibly shorter) list of Message objects.
        """
        if not self.messages:
            return []

        result = list(self.messages)

        # --- Message count limit -------------------------------------------
        if max_messages is not None and len(result) > max_messages:
            result = result[-max_messages:]

        # --- Token budget limit --------------------------------------------
        if max_tokens is not None and max_tokens > 0:
            result = self._trim_by_tokens(result, max_tokens)

        return result

    # ------------------------------------------------------------------
    # Token estimation helpers
    # ------------------------------------------------------------------

    @classmethod
    def estimate_tokens(cls, text: str) -> int:
        """
        Estimate the number of tokens in *text*.

        Uses a conservative heuristic:
        * CJK characters: ~1 token per 1.5 characters
        * Non-CJK (Latin, digits, whitespace, punctuation): ~1 token per 4 characters

        This intentionally over-estimates by ~20-30 % to stay safely
        under the real context window.
        """
        if not text:
            return 0

        cjk_chars = len(cls._CJK_RE.findall(text))
        other_chars = len(text) - cjk_chars
        return int(cjk_chars / 1.5) + int(other_chars / 4) + 1

    def estimate_total_tokens(self, messages: Optional[List[Message]] = None) -> int:
        """Estimate total token count for a list of messages."""
        if messages is None:
            messages = self.messages
        total = 0
        for msg in messages:
            total += self.estimate_tokens(msg.content or "")
            # Budget for role/formatting overhead per message
            total += 4
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += self.estimate_tokens(tc.arguments) + 8
        return total

    def _trim_by_tokens(self, messages: List[Message], max_tokens: int) -> List[Message]:
        """
        Drop oldest messages until the estimated token count fits.

        Tries to preserve complete conversation turns when possible.
        """
        total = self.estimate_total_tokens(messages)
        if total <= max_tokens:
            return messages

        # Build a list of per-message token costs.
        costs = [self._msg_token_cost(m) for m in messages]

        # We want to drop the oldest prefix while keeping recent context.
        # Strategy: walk from the front, accumulate tokens to drop, stop
        # once the remaining budget fits.
        keep_from = 0
        for i, cost in enumerate(costs):
            total -= cost
            keep_from = i + 1
            if total <= max_tokens:
                break

        if keep_from >= len(messages):
            # Even a single message may not fit — return just the last one.
            logger.warning(
                "Context window too small: single message exceeds %d tokens",
                max_tokens,
            )
            return [messages[-1]]

        trimmed = messages[keep_from:]

        # Prefer turn boundaries: if the first kept message is a tool or
        # assistant message, look back to include the user message that
        # started the turn, unless that would bust the budget.
        if trimmed and trimmed[0].role not in (Role.USER, Role.SYSTEM):
            if keep_from > 0:
                candidate = messages[keep_from - 1]
                if candidate.role == Role.USER:
                    extra = self._msg_token_cost(candidate)
                    new_total = self.estimate_total_tokens(trimmed) + extra
                    if new_total <= max_tokens:
                        trimmed = [candidate] + trimmed

        dropped = keep_from
        logger.info(
            "Trimmed %d oldest messages to fit %d-token budget "
            "(estimated %d tokens after trim)",
            dropped, max_tokens, self.estimate_total_tokens(trimmed),
        )
        return trimmed

    @classmethod
    def _msg_token_cost(cls, msg: Message) -> int:
        """Return the estimated token cost of a single message."""
        cost = cls.estimate_tokens(msg.content or "") + 4  # +4 role overhead
        if msg.tool_calls:
            for tc in msg.tool_calls:
                cost += cls.estimate_tokens(tc.arguments) + 8
        return cost

    def get_last_n_messages(self, n: int) -> List[Message]:
        """Get the last n messages."""
        return self.messages[-n:] if n > 0 else self.messages

    def clear(self) -> None:
        """Clear all message history."""
        self.messages.clear()
        self.turns.clear()

    def __len__(self) -> int:
        return len(self.messages)

    def __repr__(self) -> str:
        return f"MessageHistory(messages={len(self.messages)}, turns={len(self.turns)})"
