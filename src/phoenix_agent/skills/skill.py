"""
Skill — Runtime representation of an activated skill.

A ``Skill`` wraps a :class:`SkillManifest` and adds runtime behaviour:

- Building a composite system prompt from ``prompt.md`` + reference docs.
- Registering skill-specific tools into the global :class:`ToolRegistry`.
- Setting / restoring environment variables.
- Lifecycle hooks (``on_load`` / ``on_unload``).

Usage::

    skill = Skill(manifest)
    skill.load()               # register tools, set env, call on_load

    # ... agent uses the skill ...

    skill.unload()             # deregister tools, restore env, call on_unload
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from phoenix_agent.skills.manifest import SkillManifest
from phoenix_agent.tools.registry import ToolRegistry, ToolDefinition, ToolCategory

logger = logging.getLogger(__name__)


class Skill:
    """Runtime skill object.

    Parameters:
        manifest:  The parsed ``SKILL.yaml`` manifest.
    """

    def __init__(self, manifest: SkillManifest) -> None:
        self.manifest = manifest
        self._loaded = False
        self._registered_tool_names: List[str] = []
        self._saved_env: Dict[str, Optional[str]] = {}

        # Cached prompt (rebuilt on load)
        self._system_prompt: str = ""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def description(self) -> str:
        return self.manifest.description

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def system_prompt(self) -> str:
        """Return the composite system prompt for this skill.

        The prompt is assembled from:
        1. ``prompt.md`` (main instructions)
        2. All files under ``references/`` (appended as context)

        If ``prompt.md`` does not exist, returns an empty string.
        """
        if self._system_prompt and self._loaded:
            return self._system_prompt
        return self._build_system_prompt()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Activate the skill: register tools, set env, build prompt.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._loaded:
            logger.debug("Skill %s is already loaded", self.name)
            return

        logger.info("Loading skill: %s (v%s)", self.name, self.manifest.version)

        # 1. Register skill-specific tools
        self._register_extra_tools()

        # 2. Set environment variables
        self._set_env()

        # 3. Build system prompt
        self._system_prompt = self._build_system_prompt()

        # 4. Run on_load hook
        self._call_hook("on_load")

        self._loaded = True
        logger.info("Skill %s loaded successfully", self.name)

    def unload(self) -> None:
        """Deactivate the skill: deregister tools, restore env.

        Safe to call even if the skill was never loaded.
        """
        if not self._loaded:
            return

        logger.info("Unloading skill: %s", self.name)

        # 1. Run on_unload hook
        self._call_hook("on_unload")

        # 2. Restore environment variables
        self._restore_env()

        # 3. Deregister skill-specific tools
        self._deregister_extra_tools()

        self._loaded = False
        self._system_prompt = ""
        logger.info("Skill %s unloaded", self.name)

    # ------------------------------------------------------------------
    # Tool management
    # ------------------------------------------------------------------

    def _register_extra_tools(self) -> None:
        """Import and register tools declared in ``tools_extra``."""
        for entry in self.manifest.tools_extra:
            tool_ref = entry.strip()
            if not tool_ref or ":" not in tool_ref:
                logger.warning("Invalid tools_extra entry: %s", tool_ref)
                continue

            module_path, func_name = tool_ref.rsplit(":", 1)

            try:
                # Support both:
                #   - "excel_tools:analyze"  → import from installed package
                #   - "tools/my_tool.py:analyze"  → import from skill's tools/ dir
                if "/" in module_path or module_path.endswith(".py"):
                    mod = self._import_file_module(module_path)
                else:
                    mod = importlib.import_module(module_path)

                func = getattr(mod, func_name, None)
                if func is None:
                    logger.warning("Function %s not found in %s", func_name, module_path)
                    continue

                # Check if the function has a _tool_definition (decorated via @tool)
                tool_def: Optional[ToolDefinition] = getattr(func, "_tool_definition", None)

                if tool_def is None:
                    # Auto-wrap: create a ToolDefinition from the function
                    from phoenix_agent.tools.registry import _build_schema_from_signature
                    schema = _build_schema_from_signature(func)
                    desc = (func.__doc__ or f"Skill tool: {func_name}").strip().split("\n")[0]
                    tool_def = ToolDefinition(
                        name=f"{self.name}.{func_name}",
                        description=desc,
                        parameters=schema,
                        category=ToolCategory.UTILITY,
                        enabled_by_default=False,
                        handler=func,
                    )

                ToolRegistry.register(tool_def)
                self._registered_tool_names.append(tool_def.name)
                logger.debug("Registered skill tool: %s", tool_def.name)

            except Exception:
                logger.exception("Failed to register tool from %s", tool_ref)

    def _deregister_extra_tools(self) -> None:
        """Remove skill-specific tools from the registry."""
        registry = ToolRegistry.get_instance()
        for name in self._registered_tool_names:
            if name in registry._tools:
                del registry._tools[name]
                logger.debug("Deregistered skill tool: %s", name)
        self._registered_tool_names.clear()

    # ------------------------------------------------------------------
    # Environment management
    # ------------------------------------------------------------------

    def _set_env(self) -> None:
        """Set environment variables from manifest, saving originals."""
        for key, value in self.manifest.env.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = str(value)
            logger.debug("Set env %s=%s", key, value)

    def _restore_env(self) -> None:
        """Restore environment variables to their previous values."""
        for key, old_value in self._saved_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
        self._saved_env.clear()

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Assemble the composite system prompt."""
        parts: List[str] = []

        # 1. Main prompt.md
        prompt_file = self.manifest.prompt_path
        if prompt_file:
            try:
                parts.append(prompt_file.read_text(encoding="utf-8").strip())
            except OSError as exc:
                logger.warning("Failed to read prompt.md: %s", exc)

        # 2. Reference documents
        for ref_file in self.manifest.list_reference_files():
            try:
                content = ref_file.read_text(encoding="utf-8").strip()
                rel_path = ref_file.relative_to(self.manifest.source_path)
                parts.append(f"\n---\n## Reference: {rel_path}\n\n{content}")
            except OSError as exc:
                logger.warning("Failed to read reference %s: %s", ref_file, exc)

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def _call_hook(self, hook_name: str) -> None:
        """Call ``on_load`` or ``on_unload`` if defined in the skill directory."""
        hooks_dir = self.manifest.source_path / "hooks"
        hook_file = hooks_dir / f"{hook_name}.py"
        if not hook_file.is_file():
            return

        try:
            mod = self._import_file_module(str(hook_file))
            hook_func = getattr(mod, hook_name, None)
            if callable(hook_func):
                hook_func(skill=self)
                logger.debug("Executed hook %s for skill %s", hook_name, self.name)
        except Exception:
            logger.exception("Failed to execute hook %s for skill %s", hook_name, self.name)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _import_file_module(self, file_path: str):
        """Import a Python file as a module (without adding to sys.path permanently)."""
        abs_path = Path(file_path).resolve()
        if not abs_path.is_file():
            raise FileNotFoundError(f"Tool file not found: {abs_path}")

        # Resolve relative to skill's tools/ directory if needed
        if not abs_path.is_absolute():
            abs_path = (self.manifest.source_path / file_path).resolve()

        module_name = f"_phoenix_skill_{self.name}_{abs_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, str(abs_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {abs_path}")

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        state = "loaded" if self._loaded else "unloaded"
        return f"Skill(name={self.name!r}, version={self.manifest.version!r}, {state})"

    def summary(self) -> Dict[str, Any]:
        """Return a summary dict for display."""
        return {
            "name": self.name,
            "version": self.manifest.version,
            "description": self.description,
            "loaded": self._loaded,
            "triggers": self.manifest.triggers,
            "tools": self.manifest.tools,
            "tools_extra": self.manifest.tools_extra,
            "has_prompt": self.manifest.prompt_path is not None,
            "references": len(self.manifest.list_reference_files()),
            "source": str(self.manifest.source_path),
        }
