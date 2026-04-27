"""
Phoenix Agent - Core Package

A lightweight, extensible AI agent framework with tool-calling capabilities.
"""

__version__ = "1.0.0"
__author__ = "Phoenix Team"

from phoenix_agent.core.agent import Agent
from phoenix_agent.core.config import Config

__all__ = ["Agent", "Config", "__version__"]
