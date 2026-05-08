"""Tests for model discovery feature in Agent."""

import json
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from urllib.error import URLError

from phoenix_agent.core.config import Config, AgentConfig, ModelConfig, ProviderConfig
from phoenix_agent.core.agent import Agent


def _make_agent(config=None):
    """Create an Agent with a mock provider so no real LLM calls happen."""
    if config is None:
        config = Config()
    with patch("phoenix_agent.core.agent.create_provider") as mock_prov:
        mock_inst = MagicMock()
        mock_prov.return_value = mock_inst
        return Agent(config=config)


# ---------------------------------------------------------------------------
# discover_models tests
# ---------------------------------------------------------------------------

class TestDiscoverModels:

    def test_discover_success(self):
        agent = _make_agent()
        fake_response = {
            "data": [
                {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
                {"id": "qwen2.5-72b", "object": "model", "owned_by": "qwen"},
            ]
        }
        fake_body = json.dumps(fake_response).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_body

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            results = agent.discover_models("http://localhost:8080", "test-key", "openai")

        assert len(results) == 2
        assert results[0]["id"] == "deepseek-chat"
        assert results[0]["base_url"] == "http://localhost:8080"
        assert results[0]["api_key"] == "test-key"
        assert results[0]["provider_type"] == "openai"
        assert results[1]["id"] == "qwen2.5-72b"

    def test_discover_empty_list(self):
        agent = _make_agent()
        fake_body = json.dumps({"data": []}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_body

        with patch("urllib.request.urlopen", return_value=mock_resp):
            results = agent.discover_models("http://localhost:8080")

        assert results == []

    def test_discover_connection_failure(self):
        agent = _make_agent()

        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            results = agent.discover_models("http://localhost:9999")

        assert results == []

    def test_discover_invalid_json(self):
        agent = _make_agent()
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"not json"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            results = agent.discover_models("http://localhost:8080")

        assert results == []

    def test_discover_strips_trailing_slash(self):
        agent = _make_agent()
        fake_body = json.dumps({"data": [{"id": "m1", "object": "model", "owned_by": "x"}]}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_body

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            results = agent.discover_models("http://localhost:8080/")

        # Verify the URL called is normalized (no double slash)
        call_args = mock_urlopen.call_args[0][0]
        assert "8080/v1/models" in str(call_args.full_url)
        assert len(results) == 1

    def test_discover_no_api_key(self):
        agent = _make_agent()
        fake_body = json.dumps({"data": [{"id": "m1", "object": "model", "owned_by": "x"}]}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_body

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            results = agent.discover_models("http://localhost:8080")

        # Should not send Authorization header when no api_key
        req = mock_urlopen.call_args[0][0]
        assert "Authorization" not in req.headers

    def test_discover_with_api_key_sends_auth(self):
        agent = _make_agent()
        fake_body = json.dumps({"data": []}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_body

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            agent.discover_models("http://localhost:8080", "my-key")

        req = mock_urlopen.call_args[0][0]
        assert req.headers["Authorization"] == "Bearer my-key"


# ---------------------------------------------------------------------------
# register_model tests
# ---------------------------------------------------------------------------

class TestRegisterModel:

    def test_register_new_model(self):
        agent = _make_agent()
        result = agent.register_model(
            name="local-deepseek",
            model="deepseek-chat",
            base_url="http://localhost:8080",
            provider_type="openai-compatible",
            description="Local DeepSeek",
        )
        assert result is True
        assert "local-deepseek" in agent._model_configs
        cfg = agent._model_configs["local-deepseek"]
        assert cfg["model"] == "deepseek-chat"
        assert cfg["base_url"] == "http://localhost:8080"
        assert cfg["type"] == "openai-compatible"

    def test_register_overwrite_existing(self):
        agent = _make_agent()
        agent.register_model(name="m1", model="old-model")
        agent.register_model(name="m1", model="new-model")
        assert agent._model_configs["m1"]["model"] == "new-model"

    def test_register_inherits_provider_defaults(self):
        agent = _make_agent()
        agent.register_model(name="inherited", model="test-model")
        cfg = agent._model_configs["inherited"]
        # Should inherit from the agent's default provider config
        assert cfg["type"] == agent.config.provider.type
        assert cfg["api_key"] == (agent.config.provider.api_key or "")

    def test_register_then_list(self):
        agent = _make_agent()
        agent.register_model(name="discovered-1", model="model-a", description="Model A")
        models = agent.list_models()
        names = [m["name"] for m in models]
        assert "discovered-1" in names


# ---------------------------------------------------------------------------
# switch_model with discovered model ID tests
# ---------------------------------------------------------------------------

class TestSwitchDiscovered:

    def test_switch_by_model_id(self):
        """switch_model should match by model field if name not found."""
        agent = _make_agent()
        agent.register_model(name="ds-chat", model="deepseek-chat", base_url="http://x")

        # Switch by raw model ID (not the registered name)
        with patch("phoenix_agent.core.agent.create_provider") as mock_prov:
            mock_prov.return_value = MagicMock()
            result = agent.switch_model("deepseek-chat")

        assert result is True
        assert agent.current_model == "ds-chat"

    def test_switch_by_name_takes_priority(self):
        """If both name and model field match, name wins."""
        agent = _make_agent()
        agent.register_model(name="model-a", model="model-x")
        agent.register_model(name="model-x", model="model-x")

        with patch("phoenix_agent.core.agent.create_provider") as mock_prov:
            mock_prov.return_value = MagicMock()
            result = agent.switch_model("model-x")

        # Should match by name "model-x", not by model field
        assert result is True
        assert agent.current_model == "model-x"

    def test_switch_unknown_returns_false(self):
        agent = _make_agent()
        result = agent.switch_model("nonexistent-model-id")
        assert result is False

    def test_switch_discovered_then_register_and_switch(self):
        """Full workflow: discover -> register -> list -> switch."""
        agent = _make_agent()

        # Simulate discovery
        fake_body = json.dumps({
            "data": [{"id": "qwen2.5-32b", "object": "model", "owned_by": "qwen"}]
        }).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_body

        with patch("urllib.request.urlopen", return_value=mock_resp):
            discovered = agent.discover_models("http://localhost:8080")

        assert len(discovered) == 1
        assert discovered[0]["id"] == "qwen2.5-32b"

        # Register it
        agent.register_model(
            name="qwen-local",
            model=discovered[0]["id"],
            base_url=discovered[0]["base_url"],
            provider_type="openai-compatible",
            description="Local Qwen",
        )

        # Verify it appears in list
        models = agent.list_models()
        qwen = [m for m in models if m["name"] == "qwen-local"]
        assert len(qwen) == 1

        # Switch to it
        with patch("phoenix_agent.core.agent.create_provider") as mock_prov:
            mock_prov.return_value = MagicMock()
            result = agent.switch_model("qwen-local")

        assert result is True
        assert agent.current_model == "qwen-local"
