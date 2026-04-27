"""
Tools Package

Provides the tool system for Phoenix Agent.
"""

from phoenix_agent.tools.registry import ToolRegistry, tool, ToolResult, ToolDefinition, ToolCategory

# Initialize the registry and load builtin tools
def _init_tools():
    """Initialize tools registry and load builtins."""
    from phoenix_agent.tools.builtin import load_builtin_tools
    registry = ToolRegistry.get_instance()
    # Directly load builtin tools by calling their registration
    load_builtin_tools(registry)

# This triggers the initialization when the package is first imported
_init_tools()

__all__ = ["ToolRegistry", "tool", "ToolResult", "ToolDefinition", "ToolCategory"]
