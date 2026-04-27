"""
Test Suite for Context Window Management

Tests for MessageHistory truncation, token estimation,
and context budget enforcement.
"""

import pytest

from phoenix_agent.core.message import (
    Message, MessageHistory, Role, ConversationTurn,
)


class TestTokenEstimation:
    """Test the token estimation heuristic."""

    def test_empty_string(self):
        assert MessageHistory.estimate_tokens("") == 0

    def test_english_text(self):
        # ~4 chars per token for English
        text = "Hello world! This is a test."
        tokens = MessageHistory.estimate_tokens(text)
        # "Hello world! This is a test." = 29 chars → ~7-8 tokens + 1 = 8
        assert 5 <= tokens <= 12

    def test_cjk_text(self):
        # ~1.5 chars per token for CJK
        text = "你好世界这是一段中文测试"
        tokens = MessageHistory.estimate_tokens(text)
        # 12 CJK chars → 12/1.5 + 1 = 9
        assert 6 <= tokens <= 12

    def test_mixed_text(self):
        text = "Hello 你好 world 世界"
        tokens = MessageHistory.estimate_tokens(text)
        # 12 English + 4 CJK → 12/4 + 4/1.5 + 1 = 3 + 2 + 1 = 6
        assert 4 <= tokens <= 10

    def test_long_text_overhead(self):
        """Longer text should have proportionally more tokens."""
        short = "hello"
        long_text = "hello " * 100
        assert MessageHistory.estimate_tokens(long_text) > MessageHistory.estimate_tokens(short) * 5


class TestTruncateByMessages:
    """Test max_messages truncation."""

    def _make_messages(self, n: int) -> list:
        return [Message.user(f"msg {i}") for i in range(n)]

    def test_no_truncation_when_under_limit(self):
        hist = MessageHistory()
        for msg in self._make_messages(5):
            hist.add_message(msg)
        result = hist.get_truncated_messages(max_messages=10)
        assert len(result) == 5

    def test_truncation_keeps_most_recent(self):
        hist = MessageHistory()
        for msg in self._make_messages(10):
            hist.add_message(msg)
        result = hist.get_truncated_messages(max_messages=5)
        assert len(result) == 5
        # Should be the last 5 messages (indices 5-9)
        contents = [m.content for m in result]
        assert contents == [f"msg {i}" for i in range(5, 10)]

    def test_truncation_with_exact_limit(self):
        hist = MessageHistory()
        for msg in self._make_messages(5):
            hist.add_message(msg)
        result = hist.get_truncated_messages(max_messages=5)
        assert len(result) == 5

    def test_truncation_to_single_message(self):
        hist = MessageHistory()
        for msg in self._make_messages(20):
            hist.add_message(msg)
        result = hist.get_truncated_messages(max_messages=1)
        assert len(result) == 1
        assert result[0].content == "msg 19"

    def test_none_limits_return_all(self):
        hist = MessageHistory()
        for msg in self._make_messages(10):
            hist.add_message(msg)
        result = hist.get_truncated_messages()
        assert len(result) == 10


class TestTruncateByTokens:
    """Test max_tokens truncation."""

    def test_no_truncation_when_under_budget(self):
        hist = MessageHistory()
        for i in range(5):
            hist.add_message(Message.user(f"short msg {i}"))
        result = hist.get_truncated_messages(max_tokens=10000)
        assert len(result) == 5

    def test_truncation_drops_oldest(self):
        hist = MessageHistory()
        # 10 long messages, each ~500 chars = ~125+ tokens
        for i in range(10):
            hist.add_message(Message.user(f"Long message number {i}: " + "word " * 100))
        # Budget should force dropping oldest
        result = hist.get_truncated_messages(max_tokens=400)
        assert len(result) < 10
        # Most recent message should still be there
        assert "9" in result[-1].content

    def test_extreme_budget_returns_last_message(self):
        hist = MessageHistory()
        for i in range(5):
            hist.add_message(Message.user("x" * 200))
        # Very tiny budget — should still return at least 1 message
        result = hist.get_truncated_messages(max_tokens=10)
        assert len(result) >= 1
        assert result[-1].content == "x" * 200

    def test_empty_history(self):
        hist = MessageHistory()
        result = hist.get_truncated_messages(max_tokens=1000)
        assert result == []

    def test_combined_limits_both_apply(self):
        hist = MessageHistory()
        for i in range(50):
            hist.add_message(Message.user(f"message {i}"))
        # max_messages=10 should be stricter
        result1 = hist.get_truncated_messages(max_messages=10, max_tokens=100000)
        assert len(result1) == 10

        # max_tokens=20 should be stricter
        result2 = hist.get_truncated_messages(max_messages=50, max_tokens=20)
        assert len(result2) < 50


class TestTurnBoundaryPreservation:
    """Test that truncation prefers complete turn boundaries."""

    def test_tool_messages_not_orphaned(self):
        """When trimming, if the first kept message is a tool message,
        try to include the preceding user message."""
        hist = MessageHistory()

        # Turn 1: user + assistant + tool — long content to consume budget
        hist.add_message(Message.user("first question that is quite long"))
        hist.add_message(Message.assistant("let me check something for you"))
        hist.add_message(Message.tool(
            content="result from tool with lots of detail " + "data " * 50,
            tool_call_id="tc1", tool_name="read_file"
        ))

        # Turn 2: user + assistant + tool — short, should fit
        hist.add_message(Message.user("second question"))
        hist.add_message(Message.assistant("ok"))
        hist.add_message(Message.tool(
            content="result", tool_call_id="tc2", tool_name="run_command"
        ))

        # Tight budget: should force dropping turn 1 entirely
        result = hist.get_truncated_messages(max_tokens=40)

        # Should include the user message that starts turn 2
        assert any(m.role == Role.USER and "second" in m.content for m in result)
        # Should NOT include turn 1 user message
        assert not any(m.role == Role.USER and "first" in m.content for m in result)


class TestEstimateTotalTokens:
    """Test total token estimation across messages."""

    def test_empty_history(self):
        hist = MessageHistory()
        assert hist.estimate_total_tokens() == 0

    def test_single_message(self):
        hist = MessageHistory()
        hist.add_message(Message.user("hello"))
        tokens = hist.estimate_total_tokens()
        assert tokens > 0

    def test_multiple_messages_sum(self):
        hist = MessageHistory()
        hist.add_message(Message.user("hello"))
        t1 = hist.estimate_total_tokens()
        hist.add_message(Message.assistant("world"))
        t2 = hist.estimate_total_tokens()
        assert t2 > t1  # More messages = more tokens
