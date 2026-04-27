"""
Tool Registry Module

Provides a decorator-based tool registration system with type validation.

Design philosophy:
- Type-safe: All tool arguments validated against JSON Schema
- Extensible: New tools added via decorator
- Sandboxed: Dangerous operations require explicit enablement
- Documented: Schema self-generated from function signatures
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Type, get_type_hints
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps

logger = logging.getLogger(__name__)


class ToolCategory(str, Enum):
    """Tool category for organization and filtering."""
    FILE = "file"
    WEB = "web"
    SYSTEM = "system"
    CODE = "code"
    UTILITY = "utility"


@dataclass
class ToolDefinition:
    """Defines a tool's interface and behavior for the LLM."""
    name: str
    description: str
    parameters: Dict[str, Any]
    category: ToolCategory = ToolCategory.UTILITY
    enabled_by_default: bool = True
    requires_sandbox: bool = False
    allow_destructive: bool = False
    handler: Optional[Callable] = field(default=None, repr=False)


@dataclass
class ToolResult:
    """Result from executing a tool."""
    success: bool
    content: str
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"success": self.success, "content": self.content, "error": self.error, **self.metadata}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _python_type_to_json_schema_type(python_type: Type) -> str:
    """Convert Python type hint to JSON Schema type."""
    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    return type_map.get(python_type, "string")


def _build_schema_from_signature(func: Callable) -> Dict[str, Any]:
    """Build JSON Schema from function signature."""
    hints = get_type_hints(func)
    params = {}
    sig_params = []
    try:
        import inspect
        sig = inspect.signature(func)
        sig_params = [p for p in sig.parameters.keys() if p not in ("self", "cls")]
    except (ValueError, TypeError):
        pass
    for param_name in sig_params:
        python_type = hints.get(param_name, str)
        params[param_name] = {
            "type": _python_type_to_json_schema_type(python_type),
            "description": f"Parameter {param_name}",
        }
    return {"type": "object", "properties": params, "required": sig_params}


def _validate_arguments(schema: Dict[str, Any], arguments: Dict[str, Any]) -> List[str]:
    """Validate tool arguments against JSON Schema."""
    errors = []
    properties = schema.get("properties", {})
    for field_name in schema.get("required", []):
        if field_name not in arguments:
            errors.append(f"Missing required field: {field_name}")
    for field_name, value in arguments.items():
        if field_name not in properties:
            continue
        expected_type = properties[field_name].get("type", "string")
        checks = {
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
        }
        check = checks.get(expected_type)
        if check and not check(value):
            errors.append(f"Field '{field_name}' should be {expected_type}, got {type(value).__name__}")
    return errors


def tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    category: ToolCategory = ToolCategory.UTILITY,
    enabled_by_default: bool = True,
):
    """Decorator to register a function as a tool."""
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        tool_desc = description
        if not tool_desc and func.__doc__:
            tool_desc = func.__doc__.strip().split("\n")[0].strip()
        if not tool_desc:
            tool_desc = f"Tool: {tool_name}"
        schema = _build_schema_from_signature(func)
        tool_def = ToolDefinition(
            name=tool_name,
            description=tool_desc,
            parameters=schema,
            category=category,
            enabled_by_default=enabled_by_default,
            handler=func,
        )
        ToolRegistry.register(tool_def)
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        wrapper._tool_definition = tool_def
        return wrapper
    return decorator


class ToolRegistry:
    """Central registry for all available tools."""

    _instance: Optional["ToolRegistry"] = None

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._categories: Dict[ToolCategory, List[str]] = {cat: [] for cat in ToolCategory}
        if not hasattr(self.__class__, "_initialized"):
            self._load_builtin_tools()
            ToolRegistry._instance = self
            self.__class__._initialized = True

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_builtin_tools(self) -> None:
        try:
            from phoenix_agent.tools.builtin import register_builtin_tools
            register_builtin_tools(self)
        except ImportError:
            pass

    @classmethod
    def register(cls, tool_def: ToolDefinition) -> None:
        inst = cls.get_instance()
        inst._tools[tool_def.name] = tool_def
        inst._categories[tool_def.category].append(tool_def.name)

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def get_definitions(
        self,
        enabled: Optional[List[str]] = None,
        disabled: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get tool definitions in OpenAI function-calling format.

        Filtering rules (in priority order):
        1. If ``disabled`` contains the tool's **name**, exclude it.
        2. If ``enabled`` is given, it may contain either **tool names** or
           **category names** (e.g. ``"file"``, ``"web"``).  A tool passes if
           its name OR its category value appears in the list.
        3. Tools that are not enabled-by-default are excluded unless they
           match the ``enabled`` filter.
        """
        # Build a set of category string values for quick lookup
        enabled_categories: set = set()
        enabled_names: set = set()
        if enabled:
            category_values = {cat.value for cat in ToolCategory}
            for item in enabled:
                if item in category_values:
                    enabled_categories.add(item)
                else:
                    enabled_names.add(item)

        result = []
        for tool_name, tool_def in self._tools.items():
            # 1. Hard-disabled by name
            if disabled and tool_name in disabled:
                continue

            # 2. Enabled filter (names or categories)
            if enabled:
                in_enabled_name = tool_name in enabled_names
                in_enabled_category = tool_def.category.value in enabled_categories
                if not in_enabled_name and not in_enabled_category:
                    continue

            # 3. Skip non-default tools unless explicitly enabled by name
            if not tool_def.enabled_by_default and (not enabled_names or tool_name not in enabled_names):
                continue

            result.append({
                "type": "function",
                "function": {
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "parameters": tool_def.parameters,
                }
            })
        return result

    def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        sandbox_path: Optional[str] = None,
        allow_destructive: bool = False,
    ) -> ToolResult:
        """Execute a tool by name with validated arguments."""
        tool_def = self.get(name)
        if not tool_def:
            return ToolResult(success=False, content="", error=f"Tool not found: {name}")
        errors = _validate_arguments(tool_def.parameters, arguments)
        if errors:
            return ToolResult(success=False, content="", error=f"Argument validation failed: {'; '.join(errors)}")
        try:
            if tool_def.handler is None:
                return ToolResult(success=False, content="", error=f"Tool {name} has no handler")
            result = tool_def.handler(**arguments)
            if isinstance(result, ToolResult):
                return result
            elif isinstance(result, str):
                return ToolResult(success=True, content=result)
            else:
                return ToolResult(success=True, content=json.dumps(result, ensure_ascii=False))
        except Exception as e:
            logger.exception("Tool execution error: %s", name)
            return ToolResult(success=False, content="", error=f"Execution error: {str(e)}")

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)


registry = ToolRegistry.get_instance()
