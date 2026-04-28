"""Tests for web_search tool — Tavily, DuckDuckGo, custom providers."""

import json
from unittest import mock

import pytest

from phoenix_agent.tools.registry import ToolRegistry


def _get_web_search_handler():
    """Get the web_search tool handler from the registry."""
    registry = ToolRegistry()
    from phoenix_agent.tools.builtin import load_builtin_tools
    load_builtin_tools(registry)
    definition = registry.get("web_search")
    assert definition is not None, "web_search tool not registered"
    return definition.handler, definition.parameters


def _mock_ws_config(provider="tavily", api_key=None, max_results=5,
                    search_depth="basic", endpoint=None, api_key_name="api_key"):
    """Create a mock web_search config attributes dict."""
    return {
        "provider": provider,
        "api_key": api_key,
        "max_results": max_results,
        "search_depth": search_depth,
        "custom_endpoint": endpoint,
        "custom_api_key_name": api_key_name,
    }


def _mock_config(ws_cfg):
    """Create a full mock Config with web_search."""
    cfg = mock.MagicMock()
    cfg.web_search = mock.MagicMock(**ws_cfg)
    return cfg


def _patch_get_config(ws_cfg):
    """Patch get_config at the source module level."""
    return mock.patch(
        "phoenix_agent.core.config.get_config",
        return_value=_mock_config(ws_cfg),
    )


class TestWebSearchRegistration:
    """Test that web_search is properly registered."""

    def test_web_search_is_registered(self):
        _, params = _get_web_search_handler()
        assert params["type"] == "object"
        assert "query" in params["required"]
        assert "max_results" not in params["required"]


class TestWebSearchTavily:
    """Test Tavily search provider."""

    def test_tavily_missing_api_key(self):
        """Returns error when Tavily API key is not set."""
        handler, _ = _get_web_search_handler()

        with _patch_get_config(_mock_ws_config(api_key=None)):
            result = json.loads(handler("test query"))

        assert result["success"] is False
        assert "TAVILY_API_KEY" in result["error"]

    def test_tavily_success(self):
        """Returns formatted results on successful Tavily search."""
        handler, _ = _get_web_search_handler()
        mock_tavily_response = {
            "answer": "Test AI summary",
            "results": [
                {"title": "Result 1", "url": "https://example.com/1", "content": "Content 1", "score": 0.95},
                {"title": "Result 2", "url": "https://example.com/2", "content": "Content 2", "score": 0.85},
            ],
        }

        # Patch at the source module where the lazy import resolves
        with _patch_get_config(_mock_ws_config(api_key="tvly-test-key")), \
             mock.patch("tavily.TavilyClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.search.return_value = mock_tavily_response

            result = json.loads(handler("test query"))

        assert result["success"] is True
        assert "Test AI summary" in result["content"]
        assert "Result 1" in result["content"]
        assert "https://example.com/1" in result["content"]
        assert result["provider"] == "tavily"
        assert result["result_count"] == 3  # 1 summary + 2 results

    def test_tavily_no_results(self):
        """Returns 'no results' message when Tavily returns empty."""
        handler, _ = _get_web_search_handler()

        with _patch_get_config(_mock_ws_config(api_key="tvly-test-key")), \
             mock.patch("tavily.TavilyClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.search.return_value = {"answer": "", "results": []}

            result = json.loads(handler("obscure query xyz123"))

        assert result["success"] is True
        assert "No results found" in result["content"]


class TestWebSearchDuckDuckGo:
    """Test DuckDuckGo search provider."""

    def test_ddg_success(self):
        """Returns results parsed from DuckDuckGo HTML."""
        handler, _ = _get_web_search_handler()
        mock_html = """
        <html>
        <a class="result-link" href="https://example.com/1">Title One</a>
        <td class="result-snippet">Snippet for result one</td>
        <a class="result-link" href="https://example.com/2">Title Two</a>
        <td class="result-snippet">Snippet for result two</td>
        </html>
        """

        with _patch_get_config(_mock_ws_config(provider="duckduckgo")), \
             mock.patch("httpx.Client") as MockClient:
            mock_resp = mock.Mock()
            mock_resp.text = mock_html
            mock_resp.raise_for_status = mock.Mock()
            mock_ctx = mock.Mock()
            mock_ctx.get.return_value = mock_resp
            MockClient.return_value.__enter__ = mock.Mock(return_value=mock_ctx)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)

            result = json.loads(handler("python web framework"))

        assert result["success"] is True
        assert "Title One" in result["content"]
        assert "https://example.com/1" in result["content"]
        assert result["provider"] == "duckduckgo"

    def test_ddg_no_api_key_needed(self):
        """DuckDuckGo works without any API key."""
        handler, _ = _get_web_search_handler()
        mock_html = "<html><body>No results page</body></html>"

        with _patch_get_config(_mock_ws_config(provider="duckduckgo")), \
             mock.patch("httpx.Client") as MockClient:
            mock_resp = mock.Mock()
            mock_resp.text = mock_html
            mock_resp.raise_for_status = mock.Mock()
            mock_ctx = mock.Mock()
            mock_ctx.get.return_value = mock_resp
            MockClient.return_value.__enter__ = mock.Mock(return_value=mock_ctx)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)

            result = json.loads(handler("test"))

        assert result["success"] is True


class TestWebSearchCustom:
    """Test custom search provider."""

    def test_custom_missing_endpoint(self):
        """Returns error when custom endpoint is not configured."""
        handler, _ = _get_web_search_handler()

        with _patch_get_config(_mock_ws_config(provider="custom")):
            result = json.loads(handler("test"))

        assert result["success"] is False
        assert "custom_endpoint" in result["error"]

    def test_custom_success(self):
        """Returns results from custom endpoint."""
        handler, _ = _get_web_search_handler()
        mock_response = {
            "results": [
                {"title": "Custom Result", "url": "https://example.com", "content": "Custom content"},
            ]
        }

        with _patch_get_config(_mock_ws_config(
            provider="custom", api_key="my-key",
            endpoint="https://search.example.com/api", api_key_name="key",
        )), mock.patch("httpx.Client") as MockClient:
            mock_resp = mock.Mock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status = mock.Mock()
            mock_ctx = mock.Mock()
            mock_ctx.post.return_value = mock_resp
            MockClient.return_value.__enter__ = mock.Mock(return_value=mock_ctx)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)

            result = json.loads(handler("test query"))

        assert result["success"] is True
        assert "Custom Result" in result["content"]


class TestWebSearchUnknownProvider:
    """Test error handling for unknown provider."""

    def test_unknown_provider(self):
        """Returns error for unknown provider."""
        handler, _ = _get_web_search_handler()

        with _patch_get_config(_mock_ws_config(provider="google")):
            result = json.loads(handler("test"))

        assert result["success"] is False
        assert "Unknown search provider" in result["error"]
