"""
Skill Manifest — SKILL.yaml declaration file.

Every skill lives in a directory that contains a ``SKILL.yaml`` manifest.
The manifest declares metadata, trigger patterns, tool requirements,
and optional hooks.

Example ``SKILL.yaml``::

    name: excel-analyst
    version: "1.0.0"
    description: Analyze Excel spreadsheets and generate reports.
    triggers:
      - analyze.*excel
      - process.*spreadsheet
      - 表格分析
      - Excel.*报表
    tools:
      - read_file
      - write_file
      - run_command
    tools_extra:
      - excel_tools:analyze         # <module>:<function> skill-specific tools
      - excel_tools:generate_chart
    env:
      PANDAS_VERSION: "2.0"
    settings:
      max_rows: 10000
      output_format: markdown

The manifest is deliberately simple (YAML) so that non-developers can
create and share skills.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# The mandatory manifest filename inside a skill directory.
MANIFEST_FILENAME = "SKILL.yaml"

# The optional system-prompt file.
PROMPT_FILENAME = "prompt.md"

# The optional reference-docs directory.
REFERENCES_DIR = "references"

# The optional skill-specific tools directory.
TOOLS_DIR = "tools"


@dataclass
class SkillManifest:
    """Parsed representation of a ``SKILL.yaml`` file.

    Attributes:
        name:            Unique skill identifier (slug).
        version:         Semantic version string.
        description:     Human-readable one-liner.
        triggers:        Regex patterns / keywords for auto-detection.
        tools:           Names of built-in tools this skill requires.
        tools_extra:     ``"<module>:<callable>"`` entries for skill-specific tools.
        env:             Environment variables to set while the skill is active.
        settings:        Arbitrary key-value settings the skill can read at runtime.
        source_path:     Absolute path to the skill directory.
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    triggers: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    tools_extra: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    settings: Dict[str, Any] = field(default_factory=dict)
    source_path: Path = field(default_factory=Path)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Export as plain dict (for debugging / display)."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "triggers": self.triggers,
            "tools": self.tools,
            "tools_extra": self.tools_extra,
            "env": self.env,
            "settings": self.settings,
            "source_path": str(self.source_path),
        }

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source_path: Path) -> "SkillManifest":
        """Create a manifest from a raw dict."""
        return cls(
            name=data.get("name", source_path.name),
            version=str(data.get("version", "0.1.0")),
            description=data.get("description", ""),
            triggers=data.get("triggers", []),
            tools=data.get("tools", []),
            tools_extra=data.get("tools_extra", []),
            env=data.get("env", {}),
            settings=data.get("settings", {}),
            source_path=source_path,
        )

    @classmethod
    def from_directory(cls, directory: Path) -> Optional["SkillManifest"]:
        """Load a manifest from a skill directory.

        Returns:
            A ``SkillManifest`` instance, or ``None`` if the directory
            does not contain a valid manifest file.
        """
        manifest_file = directory / MANIFEST_FILENAME
        if not manifest_file.is_file():
            logger.debug("No %s found in %s", MANIFEST_FILENAME, directory)
            return None

        try:
            with open(manifest_file, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to parse %s: %s", manifest_file, exc)
            return None

        if not raw.get("name"):
            logger.warning("Skill manifest in %s is missing 'name'", directory)
            return None

        return cls.from_dict(raw, source_path=directory.resolve())

    # ------------------------------------------------------------------
    # Derived paths
    # ------------------------------------------------------------------

    @property
    def prompt_path(self) -> Optional[Path]:
        """Path to the optional ``prompt.md`` file."""
        p = self.source_path / PROMPT_FILENAME
        return p if p.is_file() else None

    @property
    def references_dir(self) -> Optional[Path]:
        """Path to the optional ``references/`` directory."""
        d = self.source_path / REFERENCES_DIR
        return d if d.is_dir() else None

    @property
    def tools_dir(self) -> Optional[Path]:
        """Path to the optional ``tools/`` directory."""
        d = self.source_path / TOOLS_DIR
        return d if d.is_dir() else None

    def list_reference_files(self) -> List[Path]:
        """Return sorted list of all reference documents."""
        ref_dir = self.references_dir
        if not ref_dir:
            return []
        return sorted(
            p
            for p in ref_dir.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        )

    def list_tool_files(self) -> List[Path]:
        """Return sorted list of tool Python files."""
        tools_dir = self.tools_dir
        if not tools_dir:
            return []
        return sorted(
            p
            for p in tools_dir.rglob("*.py")
            if p.is_file() and not p.name.startswith("_")
            and p.name != "__init__.py"
        )
