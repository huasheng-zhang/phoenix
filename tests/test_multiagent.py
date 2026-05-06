"""
Tests for Multi-Agent Collaboration (Agent Roles, Orchestrator, Tools)
=====================================================================
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from phoenix_agent.core.agents.roles import (
    AgentRole,
    AgentRoleConfig,
    load_roles_from_config,
    load_roles_from_directory,
    discover_all_roles,
)
from phoenix_agent.core.agents.orchestrator import (
    AgentOrchestrator,
    DelegationResult,
)


# ============================================================================
# AgentRoleConfig Tests
# ============================================================================

class TestAgentRoleConfig:
    def test_from_dict_minimal(self):
        cfg = AgentRoleConfig.from_dict("test", {"system_prompt": "You are a test."})
        assert cfg.name == "test"
        assert cfg.system_prompt == "You are a test."
        assert cfg.tools == []
        assert cfg.model is None
        assert cfg.max_iterations is None

    def test_from_dict_full(self):
        data = {
            "system_prompt": "You are a coder.",
            "description": "Code specialist",
            "tools": ["read_file", "write_file"],
            "model": "gpt-4o",
            "max_iterations": 20,
            "temperature": 0.3,
        }
        cfg = AgentRoleConfig.from_dict("coder", data)
        assert cfg.name == "coder"
        assert cfg.description == "Code specialist"
        assert cfg.tools == ["read_file", "write_file"]
        assert cfg.model == "gpt-4o"
        assert cfg.max_iterations == 20
        assert cfg.temperature == 0.3


# ============================================================================
# AgentRole Tests
# ============================================================================

class TestAgentRole:
    def test_create_role(self):
        cfg = AgentRoleConfig(
            name="test",
            system_prompt="You are a test.",
            tools=["read_file"],
        )
        role = AgentRole(cfg)
        assert role.name == "test"
        assert role.description == "test"

    def test_role_summary(self):
        cfg = AgentRoleConfig(
            name="researcher",
            system_prompt="Research specialist.",
            tools=["web_search", "web_fetch"],
            description="Web researcher",
        )
        role = AgentRole(cfg)
        summary = role.summary()
        assert summary["name"] == "researcher"
        assert summary["tools"] == ["web_search", "web_fetch"]
        assert summary["description"] == "Web researcher"

    def test_role_repr(self):
        cfg = AgentRoleConfig(
            name="coder",
            system_prompt="Code specialist.",
            tools=["read_file"],
        )
        role = AgentRole(cfg)
        assert "coder" in repr(role)
        assert "read_file" in repr(role)


# ============================================================================
# Role Loading Tests
# ============================================================================

class TestRoleLoading:
    def test_load_roles_from_config(self):
        config_data = {
            "agent_roles": {
                "researcher": {
                    "system_prompt": "You are a researcher.",
                    "tools": ["web_search"],
                },
                "coder": {
                    "system_prompt": "You are a coder.",
                    "tools": ["read_file", "write_file"],
                },
            }
        }
        roles = load_roles_from_config(config_data)
        assert len(roles) == 2
        assert roles[0].name == "researcher"
        assert roles[1].name == "coder"
        assert roles[0].config.tools == ["web_search"]

    def test_load_roles_from_config_skips_invalid(self):
        config_data = {
            "agent_roles": {
                "valid": {
                    "system_prompt": "Valid role.",
                },
                "no_prompt": {
                    "tools": ["read_file"],
                },
                "not_a_dict": "oops",
            }
        }
        roles = load_roles_from_config(config_data)
        assert len(roles) == 1
        assert roles[0].name == "valid"

    def test_load_roles_from_config_empty(self):
        roles = load_roles_from_config({})
        assert roles == []
        roles = load_roles_from_config({"agent_roles": []})
        assert roles == []

    def test_load_roles_from_directory(self, tmp_path):
        # Create valid role file
        role_file = tmp_path / "researcher.yaml"
        role_file.write_text(
            "name: researcher\n"
            "system_prompt: 'You are a researcher.'\n"
            "tools: [web_search]\n",
            encoding="utf-8",
        )
        # Create invalid role file (no system_prompt)
        bad_file = tmp_path / "invalid.yaml"
        bad_file.write_text("name: bad\ntools: [read_file]\n", encoding="utf-8")
        # Create non-YAML file
        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("not a yaml file")

        roles = load_roles_from_directory(tmp_path)
        assert len(roles) == 1
        assert roles[0].name == "researcher"

    def test_load_roles_from_nonexistent_dir(self):
        roles = load_roles_from_directory(Path("/nonexistent/path"))
        assert roles == []

    def test_discover_all_roles_with_config(self, tmp_path):
        """Test that config roles override directory roles with same name."""
        config_data = {
            "agent_roles": {
                "researcher": {
                    "system_prompt": "Config researcher.",
                    "tools": ["web_search"],
                },
            }
        }
        # Patch CWD to tmp_path and phoenix_home
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "coder.yaml").write_text(
            "name: coder\nsystem_prompt: 'Dir coder.'\ntools: [read_file]\n",
            encoding="utf-8",
        )
        (roles_dir / "researcher.yaml").write_text(
            "name: researcher\nsystem_prompt: 'Dir researcher.'\ntools: [web_fetch]\n",
            encoding="utf-8",
        )

        with patch("phoenix_agent.core.agents.roles.Path.cwd", return_value=tmp_path):
            with patch("phoenix_agent.core.agents.roles.Path.home", return_value=tmp_path):
                with patch("os.environ.get", side_effect=lambda k, d=None: d):
                    roles = discover_all_roles(config_data)

        role_names = {r.name for r in roles}
        assert "researcher" in role_names
        assert "coder" in role_names
        # Config researcher should override directory researcher
        researcher = next(r for r in roles if r.name == "researcher")
        assert "Config researcher" in researcher.config.system_prompt


# ============================================================================
# DelegationResult Tests
# ============================================================================

class TestDelegationResult:
    def test_success_result(self):
        result = DelegationResult(
            success=True,
            content="Research complete: AI is evolving.",
            role="researcher",
            duration_ms=1500,
            iterations=3,
        )
        data = json.loads(result.to_json())
        assert data["success"] is True
        assert data["content"] == "Research complete: AI is evolving."
        assert data["role"] == "researcher"
        assert data["duration_ms"] == 1500
        assert data["iterations"] == 3

    def test_error_result(self):
        result = DelegationResult(
            success=False,
            content="",
            role="unknown",
            error="Role not found",
        )
        data = json.loads(result.to_json())
        assert data["success"] is False
        assert data["error"] == "Role not found"


# ============================================================================
# AgentOrchestrator Tests
# ============================================================================

class TestAgentOrchestrator:
    def test_create_orchestrator(self):
        orch = AgentOrchestrator()
        assert orch.role_count == 0
        assert orch.worker_count == 0

    def test_load_roles_from_list(self):
        roles = [
            AgentRole(AgentRoleConfig(name="a", system_prompt="A.")),
            AgentRole(AgentRoleConfig(name="b", system_prompt="B.")),
        ]
        orch = AgentOrchestrator()
        count = orch.load_roles_from_list(roles)
        assert count == 2
        assert orch.role_count == 2

    def test_list_roles(self):
        roles = [
            AgentRole(AgentRoleConfig(
                name="researcher",
                system_prompt="Research.",
                description="Web researcher",
                tools=["web_search"],
            )),
        ]
        orch = AgentOrchestrator()
        orch.load_roles_from_list(roles)
        summaries = orch.list_roles()
        assert len(summaries) == 1
        assert summaries[0]["name"] == "researcher"

    def test_get_role(self):
        roles = [
            AgentRole(AgentRoleConfig(name="coder", system_prompt="Code.")),
        ]
        orch = AgentOrchestrator()
        orch.load_roles_from_list(roles)
        assert orch.get_role("coder") is not None
        assert orch.get_role("nonexistent") is None

    def test_delegate_unknown_role(self):
        orch = AgentOrchestrator()
        orch.load_roles_from_list([])
        result = orch.delegate("nonexistent", "Do something")
        assert result.success is False
        assert "Unknown role" in result.error

    def test_ask_unknown_role(self):
        orch = AgentOrchestrator()
        orch.load_roles_from_list([])
        result = orch.ask("nonexistent", "What is AI?")
        assert result.success is False
        assert "Unknown role" in result.error

    def test_delegate_with_mock_agent(self):
        """Test delegation with a mocked worker agent."""
        roles = [
            AgentRole(AgentRoleConfig(
                name="researcher",
                system_prompt="Research.",
                tools=["web_search"],
            )),
        ]
        mock_config = MagicMock()
        mock_config.agent.max_iterations = 10
        mock_config.agent.temperature = 0.7
        mock_config._file_config = {}
        mock_config.provider.model = "test-model"

        orch = AgentOrchestrator(config=mock_config)
        orch.load_roles_from_list(roles)

        # Mock the Agent creation
        mock_worker = MagicMock()
        mock_worker.run.return_value = "Research result: AI is great."
        mock_worker.iteration_count = 2
        mock_worker.max_iterations = 10
        mock_worker.config.agent.temperature = 0.7
        mock_worker.history = MagicMock()

        with patch.object(orch, "_create_worker", return_value=mock_worker):
            result = orch.delegate("researcher", "Research AI")

        assert result.success is True
        assert "Research result: AI is great" in result.content
        assert result.role == "researcher"
        assert result.iterations == 2
        assert result.duration_ms >= 0

    def test_ask_with_mock_agent(self):
        """Test ask (lightweight delegation) with max_iterations=1."""
        roles = [
            AgentRole(AgentRoleConfig(
                name="coder",
                system_prompt="Code.",
                tools=["read_file"],
            )),
        ]
        mock_config = MagicMock()
        mock_config.agent.max_iterations = 10
        mock_config.agent.temperature = 0.7
        mock_config._file_config = {}
        mock_config.provider.model = "test-model"

        orch = AgentOrchestrator(config=mock_config)
        orch.load_roles_from_list(roles)

        mock_worker = MagicMock()
        mock_worker.run.return_value = "Use a dict comprehension."
        mock_worker.iteration_count = 1
        mock_worker.max_iterations = 10
        mock_worker.history = MagicMock()

        with patch.object(orch, "_create_worker", return_value=mock_worker):
            result = orch.ask("coder", "How to filter a list?")

        assert result.success is True
        assert "dict comprehension" in result.content

        # Verify ask uses max_iterations=1
        _, kwargs = mock_worker.run.call_args
        # History should have been cleared
        mock_worker.history.clear.assert_called()

    def test_delegate_with_context(self):
        """Test that context is prepended to the task."""
        roles = [
            AgentRole(AgentRoleConfig(
                name="researcher",
                system_prompt="Research.",
            )),
        ]
        mock_config = MagicMock()
        mock_config.agent.max_iterations = 10
        mock_config.agent.temperature = 0.7
        mock_config._file_config = {}
        mock_config.provider.model = "test-model"

        orch = AgentOrchestrator(config=mock_config)
        orch.load_roles_from_list(roles)

        mock_worker = MagicMock()
        mock_worker.run.return_value = "Result."
        mock_worker.iteration_count = 1
        mock_worker.max_iterations = 10
        mock_worker.history = MagicMock()

        with patch.object(orch, "_create_worker", return_value=mock_worker):
            orch.delegate("researcher", "Find info", context="User wants daily briefing")

        # Verify the task included context
        call_args = mock_worker.run.call_args
        task_arg = call_args[0][0]
        assert "User wants daily briefing" in task_arg
        assert "Find info" in task_arg

    def test_shutdown(self):
        roles = [
            AgentRole(AgentRoleConfig(name="a", system_prompt="A.")),
        ]
        mock_config = MagicMock()
        mock_config.agent.max_iterations = 10
        mock_config.agent.temperature = 0.7
        mock_config._file_config = {}
        mock_config.provider.model = "test-model"

        orch = AgentOrchestrator(config=mock_config)
        orch.load_roles_from_list(roles)

        mock_worker = MagicMock()
        with patch.object(orch, "_create_worker", return_value=mock_worker):
            orch.delegate("a", "task")

        assert orch.worker_count == 1
        count = orch.shutdown()
        assert count == 1
        assert orch.worker_count == 0
        mock_worker.end.assert_called_once()


# ============================================================================
# Tool Registration Tests
# ============================================================================

class TestToolRegistration:
    def test_agent_tools_registered(self):
        """Verify that the three agent tools are registered in the builtin tools."""
        from phoenix_agent.tools.builtin import load_builtin_tools
        from phoenix_agent.tools.registry import ToolRegistry, ToolCategory

        # Create a fresh registry
        reg = ToolRegistry.__new__(ToolRegistry)
        reg._tools = {}
        reg._categories = {cat: [] for cat in ToolCategory}

        load_builtin_tools(reg)

        assert "list_agent_roles" in reg._tools
        assert "delegate_to_agent" in reg._tools
        assert "ask_agent" in reg._tools
