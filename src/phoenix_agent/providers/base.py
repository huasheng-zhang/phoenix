"""
LLM Provider Base Module

Defines the abstract interface for LLM providers.
Phoenix Agent supports multiple providers through a common interface.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Iterator, AsyncIterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """
    Standardized response from an LLM provider.

    All provider responses are converted to this format
    for consistent handling downstream.
    """
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    raw_response: Optional[Dict[str, Any]] = None

    def has_tool_calls(self) -> bool:
        """Check if this response contains tool call requests."""
        return bool(self.tool_calls)


class BaseProvider(ABC):
    """
    Abstract base class for LLM providers.

    All provider implementations must inherit from this class
    and implement the required methods.

    Example:
        class MyProvider(BaseProvider):
            def __init__(self, api_key: str, model: str = "my-model"):
                super().__init__(api_key, model)

            def complete(self, messages: List[Dict], tools: Optional[List] = None) -> LLMResponse:
                # Implement LLM call
                ...
    """

    def __init__(self, api_key: str, model: str, **kwargs):
        """
        Initialize the provider.

        Args:
            api_key: API key for authentication.
            model: Model identifier.
            **kwargs: Additional provider-specific configuration.
        """
        self.api_key = api_key
        self.model = model
        self.config = kwargs

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """
        Generate a completion from the LLM.

        Args:
            messages: List of message dicts in OpenAI format.
            tools: Optional list of tool definitions.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            **kwargs: Additional provider-specific parameters.

        Returns:
            LLMResponse with content and optional tool calls.
        """
        pass

    @abstractmethod
    def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Iterator[str]:
        """
        Generate a streaming completion from the LLM.

        Args:
            messages: List of message dicts in OpenAI format.
            tools: Optional list of tool definitions.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            **kwargs: Additional provider-specific parameters.

        Yields:
            Text chunks as they are generated.
        """
        pass

    def validate_config(self) -> List[str]:
        """
        Validate provider configuration.

        Returns:
            List of validation error messages (empty if valid).
        """
        errors = []
        if not self.api_key:
            errors.append("API key is required")
        if not self.model:
            errors.append("Model name is required")
        return errors

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(model={self.model})"
