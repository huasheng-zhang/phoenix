"""
OpenAI Provider Module

Implements the OpenAI API provider for Phoenix Agent.
Supports OpenAI's function calling format.
"""

import logging
from typing import Any, Dict, List, Optional, Iterator
from openai import OpenAI, APIError, RateLimitError, Timeout

from phoenix_agent.providers.base import BaseProvider, LLMResponse

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """
    OpenAI API provider implementation.

    Supports all OpenAI models with function calling capabilities.

    Example:
        provider = OpenAIProvider(
            api_key="sk-...",
            model="gpt-4o"
        )
        response = provider.complete(messages)
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: Optional[str] = None,
        timeout: int = 120,
        max_retries: int = 3,
        **kwargs
    ):
        """
        Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key.
            model: Model name (e.g., gpt-4o, gpt-4-turbo).
            base_url: Optional custom base URL for proxies.
            timeout: Request timeout in seconds.
            max_retries: Maximum number of retries on failure.
            **kwargs: Additional configuration.
        """
        super().__init__(api_key, model, **kwargs)

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """
        Generate a completion from OpenAI API.

        Args:
            messages: List of message dicts.
            tools: Optional list of tool definitions.
            temperature: Sampling temperature (0-2).
            max_tokens: Maximum tokens to generate.
            **kwargs: Additional parameters.

        Returns:
            LLMResponse with content and tool calls.
        """
        try:
            # Build request arguments
            request_kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }

            if tools:
                request_kwargs["tools"] = tools
                request_kwargs["tool_choice"] = "auto"

            if max_tokens:
                request_kwargs["max_tokens"] = max_tokens

            # Add any additional kwargs
            request_kwargs.update(kwargs)

            # Make the API call
            response = self.client.chat.completions.create(**request_kwargs)

            # Extract response content
            choice = response.choices[0]
            message = choice.message

            # Parse tool calls
            tool_calls = None
            if message.tool_calls:
                tool_calls = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in message.tool_calls
                ]

            # Extract usage information
            usage = None
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return LLMResponse(
                content=message.content or "",
                tool_calls=tool_calls,
                finish_reason=choice.finish_reason,
                usage=usage,
                raw_response=response.model_dump(),
            )

        except RateLimitError as e:
            logger.warning("OpenAI rate limit hit: %s", e)
            raise
        except APIError as e:
            logger.error("OpenAI API error: %s", e)
            raise
        except Exception as e:
            logger.exception("Unexpected error in OpenAI completion")
            raise

    def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Iterator[str]:
        """
        Generate a streaming completion from OpenAI API.

        Args:
            messages: List of message dicts.
            tools: Optional list of tool definitions.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            **kwargs: Additional parameters.

        Yields:
            Text chunks as they are generated.
        """
        try:
            request_kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }

            if tools:
                request_kwargs["tools"] = tools
                request_kwargs["tool_choice"] = "auto"

            if max_tokens:
                request_kwargs["max_tokens"] = max_tokens

            request_kwargs.update(kwargs)

            stream = self.client.chat.completions.create(**request_kwargs)

            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            logger.exception("Error in streaming completion")
            raise


class AnthropicProvider(BaseProvider):
    """
    Anthropic API provider implementation.

    Supports Claude models with tool use capabilities.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        **kwargs
    ):
        """
        Initialize Anthropic provider.

        Args:
            api_key: Anthropic API key.
            model: Model name (e.g., claude-sonnet-4-20250514).
            **kwargs: Additional configuration.
        """
        super().__init__(api_key, model, **kwargs)

        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=api_key)
        except ImportError:
            raise ImportError("Please install anthropic: pip install anthropic")

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """
        Generate a completion from Anthropic API.

        Args:
            messages: List of message dicts.
            tools: Optional list of tool definitions.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            **kwargs: Additional parameters.

        Returns:
            LLMResponse with content and tool calls.
        """
        try:
            # Convert messages to Anthropic format
            anthropic_messages = self._convert_messages(messages)

            # Build request arguments
            request_kwargs = {
                "model": self.model,
                "messages": anthropic_messages,
                "temperature": temperature,
            }

            if tools:
                request_kwargs["tools"] = self._convert_tools(tools)

            # Anthropic requires max_tokens
            request_kwargs["max_tokens"] = max_tokens or 4096

            # Make the API call
            response = self.client.messages.create(**request_kwargs)

            # Parse tool calls
            tool_calls = None
            if response.content and any(block.type == "tool_use" for block in response.content):
                tool_calls = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_calls.append({
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": block.input.__str__() if hasattr(block.input, "__str__") else str(block.input),
                            }
                        })

            # Extract text content
            content = ""
            for block in response.content:
                if block.type == "text":
                    content += block.text

            # Extract usage
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }

            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=response.stop_reason,
                usage=usage,
                raw_response=response.model_dump(),
            )

        except Exception as e:
            logger.exception("Error in Anthropic completion")
            raise

    def stream(self, messages: List[Dict[str, Any]], **kwargs) -> Iterator[str]:
        """Streaming not yet implemented for Anthropic."""
        raise NotImplementedError("Streaming not yet implemented for Anthropic provider")

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert OpenAI format messages to Anthropic format."""
        result = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                result.append({"role": "user", "content": f"[System] {msg.get('content', '')}"})
            elif role == "tool":
                result.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }]
                })
            else:
                result.append(msg)
        return result

    def _convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert OpenAI tool format to Anthropic tool format."""
        result = []
        for tool in tools:
            func = tool.get("function", {})
            result.append({
                "name": func.get("name"),
                "description": func.get("description"),
                "input_schema": func.get("parameters", {}),
            })
        return result


class OpenAICompatibleProvider(BaseProvider):
    """
    Provider for OpenAI-compatible APIs.

    Supports any API that follows the OpenAI chat completion format,
    such as local models, proxies, and third-party providers.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 120,
        **kwargs
    ):
        """
        Initialize OpenAI-compatible provider.

        Args:
            api_key: API key (may be dummy for local models).
            base_url: Base URL of the API endpoint.
            model: Model identifier.
            timeout: Request timeout in seconds.
            **kwargs: Additional configuration.
        """
        super().__init__(api_key, model, **kwargs)

        self.client = OpenAI(
            api_key=api_key or "dummy",
            base_url=base_url,
            timeout=timeout,
            max_retries=3,
        )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """Generate completion via OpenAI-compatible API."""
        try:
            request_kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }

            if tools:
                request_kwargs["tools"] = tools
                request_kwargs["tool_choice"] = "auto"

            if max_tokens:
                request_kwargs["max_tokens"] = max_tokens

            request_kwargs.update(kwargs)

            response = self.client.chat.completions.create(**request_kwargs)
            choice = response.choices[0]
            message = choice.message

            tool_calls = None
            if message.tool_calls:
                tool_calls = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in message.tool_calls
                ]

            usage = None
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return LLMResponse(
                content=message.content or "",
                tool_calls=tool_calls,
                finish_reason=choice.finish_reason,
                usage=usage,
                raw_response=response.model_dump(),
            )

        except Exception as e:
            logger.exception("Error in OpenAI-compatible completion")
            raise

    def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Iterator[str]:
        """Generate streaming completion via OpenAI-compatible API."""
        try:
            request_kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }

            if tools:
                request_kwargs["tools"] = tools
                request_kwargs["tool_choice"] = "auto"

            if max_tokens:
                request_kwargs["max_tokens"] = max_tokens

            request_kwargs.update(kwargs)

            stream = self.client.chat.completions.create(**request_kwargs)

            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            logger.exception("Error in streaming completion")
            raise


def create_provider(provider_type: str, config: Dict[str, Any]) -> BaseProvider:
    """
    Factory function to create a provider instance.

    Args:
        provider_type: Type of provider ("openai", "anthropic", "openai-compatible").
        config: Provider configuration dict.

    Returns:
        BaseProvider instance.

    Raises:
        ValueError: If provider type is unknown.
    """
    if provider_type == "openai":
        return OpenAIProvider(
            api_key=config.get("api_key", ""),
            model=config.get("model", "gpt-4o"),
            base_url=config.get("base_url"),
            timeout=config.get("timeout", 120),
        )
    elif provider_type == "anthropic":
        return AnthropicProvider(
            api_key=config.get("api_key", ""),
            model=config.get("model", "claude-sonnet-4-20250514"),
        )
    elif provider_type == "openai-compatible":
        return OpenAICompatibleProvider(
            api_key=config.get("api_key", ""),
            base_url=config.get("base_url", ""),
            model=config.get("model", ""),
            timeout=config.get("timeout", 120),
        )
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")
