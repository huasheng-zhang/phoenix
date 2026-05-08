"""Tests for multi-model switching feature in Agent."""

import pytest
from unittest.mock import patch, MagicMock

from phoenix_agent.core.config import (
    Config, AgentConfig, ModelConfig, ProviderConfig,
)
from phoenix_agent.core.agent import Agent
from phoenix_agent.providers.base import LLMResponse


# ---------------------------------------------------------------------------
# Config-level tests
# ---------------------------------------------------------------------------

class TestModelConfig:
    """Test ModelConfig dataclass."""

    def test_basic_fields(self):
        mc = ModelConfig(
            name="gpt4",
            type="openai",
            model="gpt-4o",
            api_key="sk-test",
            description="GPT-4o model",
        )
        assert mc.name == "gpt4"
        assert mc.model == "gpt-4o"
        assert mc.api_key == "sk-test"
        assert mc.description == "GPT-4o model"

    def test_env_placeholder_resolution(self):
        import os
        os.environ["_PHOENIX_TEST_KEY"] = "resolved-key-123"
        mc = ModelConfig(
            name="test",
            api_key="${_PHOENIX_TEST_KEY}",
        )
        assert mc.api_key == "resolved-key-123"
        del os.environ["_PHOENIX_TEST_KEY"]

    def test_env_placeholder_missing_resolves_none(self):
        mc = ModelConfig(
            name="test",
            api_key="${_NONEXISTENT_VAR_PHOENIX}",
        )
        assert mc.api_key is None

    def test_no_placeholder_untouched(self):
        mc = ModelConfig(name="test", api_key="plain-key")
        assert mc.api_key == "plain-key"


class TestAgentConfigModels:
    """Test models list in AgentConfig."""

    def test_models_default_empty(self):
        cfg = AgentConfig()
        assert cfg.models == []

    def test_models_from_list(self):
        models = [
            ModelConfig(name="gpt4", type="openai", model="gpt-4o"),
            ModelConfig(name="deepseek", type="openai-compatible",
                        model="deepseek-chat", base_url="http://localhost:8080"),
        ]
        cfg = AgentConfig(models=models)
        assert len(cfg.models) == 2
        assert cfg.models[0].name == "gpt4"
        assert cfg.models[1].base_url == "http://localhost:8080"

    def test_plan_mode_default(self):
        cfg = AgentConfig()
        assert cfg.plan_mode is False


# ---------------------------------------------------------------------------
# Agent-level tests
# ---------------------------------------------------------------------------

class TestMultiModelAgent:
    """Test model switching on the Agent class."""

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_default_model_registered(self, mock_db, mock_get_config, mock_create):
        """Agent should have a 'default' model from provider config."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="test-key", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(memory_enabled=False)
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        models = agent.list_models()
        assert len(models) >= 1
        assert any(m["name"] == "default" and m["is_active"] for m in models)

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_named_models_from_config(self, mock_db, mock_get_config, mock_create):
        """Named models from config should be registered."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="test-key", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(
            memory_enabled=False,
            models=[
                ModelConfig(name="fast", type="openai", model="gpt-4o-mini",
                            description="Fast model for simple tasks"),
                ModelConfig(name="smart", type="openai", model="o1",
                            description="Reasoning model"),
            ],
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        models = agent.list_models()
        names = {m["name"] for m in models}
        assert "default" in names
        assert "fast" in names
        assert "smart" in names

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_switch_to_valid_model(self, mock_db, mock_get_config, mock_create):
        """switch_model should rebuild the provider."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="test-key", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(
            memory_enabled=False,
            models=[
                ModelConfig(name="fast", type="openai", model="gpt-4o-mini"),
            ],
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg

        mock_provider_default = MagicMock()
        mock_provider_fast = MagicMock()
        mock_create.side_effect = [mock_provider_default, mock_provider_fast]

        agent = Agent(config=cfg)
        assert agent.current_model == "default"

        result = agent.switch_model("fast")
        assert result is True
        assert agent.current_model == "fast"
        assert mock_create.call_count == 2

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_switch_to_invalid_model(self, mock_db, mock_get_config, mock_create):
        """switch_model with unknown name should return False."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="test-key", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(memory_enabled=False)
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        result = agent.switch_model("nonexistent_model")
        assert result is False
        assert agent.current_model == "default"

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_switch_back_to_default(self, mock_db, mock_get_config, mock_create):
        """Can switch back to 'default' model."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="test-key", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(
            memory_enabled=False,
            models=[
                ModelConfig(name="fast", type="openai", model="gpt-4o-mini"),
            ],
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        agent.switch_model("fast")
        assert agent.current_model == "fast"

        result = agent.switch_model("default")
        assert result is True
        assert agent.current_model == "default"

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_list_models_marks_active(self, mock_db, mock_get_config, mock_create):
        """list_models should only mark the current model as active."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="test-key", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(
            memory_enabled=False,
            models=[
                ModelConfig(name="fast", type="openai", model="gpt-4o-mini"),
                ModelConfig(name="smart", type="openai", model="o1"),
            ],
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        models = agent.list_models()

        active_count = sum(1 for m in models if m["is_active"])
        assert active_count == 1

        agent.switch_model("smart")
        models = agent.list_models()
        active = [m for m in models if m["is_active"]]
        assert len(active) == 1
        assert active[0]["name"] == "smart"

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_repr_includes_model_info(self, mock_db, mock_get_config, mock_create):
        """Agent repr should show current model name."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="test-key", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(memory_enabled=False, plan_mode=True)
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        r = repr(agent)
        assert "default" in r
        assert "plan=True" in r

    @patch("phoenix_agent.core.agent.create_provider")
    @patch("phoenix_agent.core.agent.get_config")
    @patch("phoenix_agent.core.state.Database")
    def test_named_model_inherits_default_api_key(self, mock_db, mock_get_config, mock_create):
        """Named model without api_key should inherit from default provider."""
        cfg = MagicMock()
        cfg.provider = ProviderConfig(
            api_key="default-key-123", model="gpt-4o", type="openai",
        )
        cfg.agent = AgentConfig(
            memory_enabled=False,
            models=[
                ModelConfig(name="fast", type="openai", model="gpt-4o-mini"),
                # No api_key specified — should inherit
            ],
        )
        cfg.tools = MagicMock(enabled=["file", "web", "system", "utility"], disabled=[])
        cfg.storage = MagicMock(db_path=":memory:")
        mock_get_config.return_value = cfg
        mock_create.return_value = MagicMock()

        agent = Agent(config=cfg)
        # Check the internal config for "fast" model
        fast_cfg = agent._model_configs.get("fast")
        assert fast_cfg is not None
        assert fast_cfg["api_key"] == "default-key-123"
