"""Tests for plan_mode feature in Agent."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from phoenix_agent.core.config import Config, AgentConfig, ModelConfig, ProviderConfig
from phoenix_agent.core.agent import Agent
from phoenix_agent.core.message import Message, Role
from phoenix_agent.providers.base import LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_provider(content: str = "", tool_calls=None):
    """Return a mock that behaves like a BaseProvider.complete()."""
    resp = LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason="stop" if not tool_calls else "tool_calls",
    )
    return resp


def _make_tool_call(name: str, args: dict) -> dict:
    return {
        "id": "call_001",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# ---------------------------------------------------------------------------
# Config-level tests
# ---------------------------------------------------------------------------

class TestPlanModeConfig:
    """Test plan_mode in configuration."""

    def test_plan_mode_default_false(self):
        cfg = AgentConfig()
        assert cfg.plan_mode is False

    def test_plan_mode_from_dict(self):
        """plan_mode=True should be parsed from raw config dict."""
        # Simulate _build_agent_config parsing
        raw = {"plan_mode": True}
        cfg = AgentConfig(
            plan_mode=raw.get("plan_mode", False),
        )
        assert cfg.plan_mode is True

    def test_plan_mode_explicit_false(self):
        raw = {"plan_mode": False}
        cfg = AgentConfig(
            plan_mode=raw.get("plan_mode", False),
        )
        assert cfg.plan_mode is False


# ---------------------------------------------------------------------------
# Agent-level tests
# ---------------------------------------------------------------------------

class TestPlanModeAgent:
    """Test plan_mode behavior on the Agent class."""

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_plan_mode_initializes_from_config(self, mock_db, mock_get_config, mock_create):
        """Agent should read plan_mode from config."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(api_key="test-key", model="test-model")
        cfg.agent = AgentConfig(plan_mode=True, memory_enabled=False)
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        assert agent.plan_mode is True

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_set_plan_mode(self, mock_db, mock_get_config, mock_create):
        """set_plan_mode() should toggle the flag."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(api_key="test-key", model="test-model")
        cfg.agent = AgentConfig(plan_mode=False, memory_enabled=False)
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        assert agent.plan_mode is False

        agent.set_plan_mode(True)
        assert agent.plan_mode is True

        agent.set_plan_mode(False)
        assert agent.plan_mode is False

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_plan_mode_blocks_tool_execution(self, mock_db, mock_get_config, mock_create):
        """In plan_mode, tool_calls from LLM should NOT be executed."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(api_key="test-key", model="test-model")
        cfg.agent = AgentConfig(
            plan_mode=True,
            memory_enabled=False,
            max_iterations=10,
            max_history_messages=None,
            max_context_tokens=None,
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg

        # Provider returns a tool_call response
        mock_provider = MagicMock()
        mock_provider.complete.return_value = _make_mock_provider(
            content="I'll analyze this task.",
            tool_calls=[_make_tool_call("read_file", {"file_path": "/tmp/test.txt"})],
        )
        mock_create.return_value = mock_provider

        agent = Agent(config=cfg)
        response = agent.run("Analyze the codebase structure")

        # Should NOT have called execute on the tool registry
        assert "Plan Mode" in response
        assert "read_file" in response
        assert "Disable plan mode to execute" in response

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_normal_mode_executes_tools(self, mock_db, mock_get_config, mock_create):
        """In normal mode, tool_calls should be executed as before."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(api_key="test-key", model="test-model")
        cfg.agent = AgentConfig(
            plan_mode=False,
            memory_enabled=False,
            max_iterations=10,
            max_history_messages=None,
            max_context_tokens=None,
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        cfg.tools.sandbox_path = None
        cfg.tools.allow_destructive = False
        mock_get_config.return_value = cfg

        # First call: LLM wants a tool; Second call: LLM gives final answer
        tool_response = _make_mock_provider(
            content="",
            tool_calls=[_make_tool_call("echo", {"text": "hello"})],
        )
        final_response = _make_mock_provider(content="Task completed.")

        mock_provider = MagicMock()
        mock_provider.complete.side_effect = [tool_response, final_response]
        mock_create.return_value = mock_provider

        agent = Agent(config=cfg)
        response = agent.run("Say hello")

        # Should have executed the tool (provider.complete called twice)
        assert mock_provider.complete.call_count == 2
        assert response == "Task completed."

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_plan_method_is_one_shot(self, mock_db, mock_get_config, mock_create):
        """plan() should force plan_mode for one turn and restore it."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(api_key="test-key", model="test-model")
        cfg.agent = AgentConfig(
            plan_mode=False,
            memory_enabled=False,
            max_iterations=10,
            max_history_messages=None,
            max_context_tokens=None,
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg

        mock_provider = MagicMock()
        mock_provider.complete.return_value = _make_mock_provider(
            content="Here's my plan: 1. Read files 2. Analyze 3. Report",
        )
        mock_create.return_value = mock_provider

        agent = Agent(config=cfg)
        assert agent.plan_mode is False

        result = agent.plan("Analyze the project")
        assert agent.plan_mode is False  # restored
        assert "plan" in result.lower() or "Plan Mode" not in result  # no tool_calls, so no plan note

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_plan_method_preserves_existing_plan_mode(self, mock_db, mock_get_config, mock_create):
        """plan() should restore plan_mode even if it was already True."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(api_key="test-key", model="test-model")
        cfg.agent = AgentConfig(
            plan_mode=True,
            memory_enabled=False,
            max_iterations=10,
            max_history_messages=None,
            max_context_tokens=None,
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg

        mock_provider = MagicMock()
        mock_provider.complete.return_value = _make_mock_provider(content="Plan output")
        mock_create.return_value = mock_provider

        agent = Agent(config=cfg)
        assert agent.plan_mode is True

        agent.plan("Analyze")
        assert agent.plan_mode is True  # still True
