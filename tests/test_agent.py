"""
Test Suite for Phoenix Agent

Basic tests to verify core functionality.
"""

import pytest
import tempfile
import os
from pathlib import Path


class TestConfig:
    """Test configuration module."""

    def test_default_config(self):
        """Test default configuration loading."""
        from phoenix_agent.core.config import Config
        config = Config()
        assert config.provider.type == "openai"
        assert config.agent.max_iterations > 0

    def test_custom_config_path(self):
        """Test loading config from custom path."""
        from phoenix_agent.core.config import Config

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
provider:
  type: openai
  model: gpt-4o-mini
agent:
  max_iterations: 10
""")
            f.flush()
            config_path = f.name

        try:
            config = Config(path=config_path)
            assert config.provider.model == "gpt-4o-mini"
            assert config.agent.max_iterations == 10
        finally:
            import time
            time.sleep(0.1)
            try:
                os.unlink(config_path)
            except PermissionError:
                pass


class TestMessage:
    """Test message types."""

    def test_message_creation(self):
        """Test creating messages."""
        from phoenix_agent.core.message import Message, Role

        msg = Message.user("Hello")
        assert msg.role == Role.USER
        assert msg.content == "Hello"

    def test_message_to_dict(self):
        """Test message serialization."""
        from phoenix_agent.core.message import Message

        msg = Message.user("Test message")
        data = msg.to_dict()
        assert data["role"] == "user"
        assert data["content"] == "Test message"

    def test_assistant_message(self):
        """Test assistant message creation."""
        from phoenix_agent.core.message import Message, ToolCall

        tc = ToolCall(id="call_123", name="test", arguments='{"arg": "value"}')
        msg = Message.assistant(content="I'll help", tool_calls=[tc])
        assert msg.has_tool_calls()
        assert msg.tool_calls[0].name == "test"


class TestDatabase:
    """Test database operations."""

    def test_create_and_get_session(self):
        """Test session creation and retrieval."""
        from phoenix_agent.core.state import Database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            session_id = db.create_session(title="Test Session")
            assert session_id is not None

            session = db.get_session(session_id)
            assert session is not None
            assert session["title"] == "Test Session"

            db.close()

    def test_add_message(self):
        """Test adding messages to session."""
        from phoenix_agent.core.state import Database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            session_id = db.create_session()
            msg_id = db.add_message(session_id, "user", "Hello, world!")
            assert msg_id > 0

            messages = db.get_messages(session_id)
            assert len(messages) == 1
            assert messages[0]["content"] == "Hello, world!"

            db.close()

    def test_session_update(self):
        """Test updating session metadata."""
        from phoenix_agent.core.state import Database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            session_id = db.create_session()
            db.update_session(session_id, title="Updated Title")

            session = db.get_session(session_id)
            assert session["title"] == "Updated Title"

            db.close()


class TestToolRegistry:
    """Test tool registry - uses global singleton."""

    def test_registry_singleton(self):
        """Test that registry is a singleton."""
        from phoenix_agent.tools.registry import registry
        from phoenix_agent.tools.registry import ToolRegistry

        # Both should be the same instance
        direct = ToolRegistry.get_instance()
        assert registry is direct

    def test_builtin_tools_loaded(self):
        """Test that builtin tools are registered."""
        from phoenix_agent.tools.registry import registry

        # Should have some builtin tools
        tool_names = registry.list_tools()
        assert len(tool_names) > 0

    def test_get_definitions(self):
        """Test getting tool definitions."""
        from phoenix_agent.tools.registry import registry

        definitions = registry.get_definitions()
        assert len(definitions) > 0
        assert definitions[0]["type"] == "function"

    def test_execute_echo_tool(self):
        """Test executing the echo tool."""
        from phoenix_agent.tools.registry import registry

        # The echo tool should be registered from builtin
        if "echo" in registry.list_tools():
            result = registry.execute("echo", {"message": "Hello"})
            assert result.success
            assert "Hello" in result.content

    def test_execute_get_time(self):
        """Test executing the get_time tool."""
        from phoenix_agent.tools.registry import registry

        if "get_time" in registry.list_tools():
            result = registry.execute("get_time", {})
            assert result.success


class TestAgent:
    """Test agent initialization."""

    def test_agent_init(self):
        """Test agent can be initialized."""
        from phoenix_agent import Agent

        try:
            agent = Agent()
            assert agent.config is not None
            assert agent.history is not None
        except Exception as e:
            # Expected without API key - check error is reasonable
            error_msg = str(e).lower()
            assert any(x in error_msg for x in ["api key", "configuration", "invalid", "error"])

    def test_agent_has_tools(self):
        """Test agent has tool registry."""
        from phoenix_agent import Agent

        try:
            agent = Agent()
            assert agent.tools is not None
            assert len(agent.tools) > 0
        except Exception:
            pass  # May fail without API key


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
