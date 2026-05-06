"""
Agent Role Definitions
======================

An ``AgentRole`` defines a named agent persona with its own:
- system_prompt  — what the agent "is"
- tools         — which tools it can use
- model         — which LLM model it uses (optional, defaults to parent config)
- max_iterations — agent-specific iteration limit

Roles are defined in ``config.yaml`` under the ``agent_roles:`` section
or in standalone YAML files under ``~/.phoenix/roles/`` and ``./roles/``.

Example config.yaml::

    agent_roles:
      researcher:
        system_prompt: "You are a research specialist. Find and summarize information."
        tools: ["web_search", "web_fetch", "read_file"]
        model: null  # use default

      coder:
        system_prompt: "You are an expert programmer. Write clean, tested code."
        tools: ["read_file", "write_file", "edit_file", "run_command", "grep"]
        max_iterations: 30

      reviewer:
        system_prompt: "You are a code reviewer. Analyze code for bugs, style, and best practices."
        tools: ["read_file", "grep", "glob_files"]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class AgentRoleConfig:
    """Configuration for a single agent role."""

    name: str = ""
    system_prompt: str = ""
    tools: List[str] = field(default_factory=list)
    model: Optional[str] = None
    max_iterations: Optional[int] = None
    temperature: Optional[float] = None
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "AgentRoleConfig":
        """Create an AgentRoleConfig from a raw dict."""
        return cls(
            name=name,
            system_prompt=data.get("system_prompt", ""),
            tools=data.get("tools", []),
            model=data.get("model"),
            max_iterations=data.get("max_iterations"),
            temperature=data.get("temperature"),
            description=data.get("description", ""),
        )


class AgentRole:
    """
    Runtime representation of an agent role.

    Parameters:
        config: The parsed role configuration.
    """

    def __init__(self, config: AgentRoleConfig):
        self.config = config
        self._worker_agent = None  # lazy-created worker Agent instance

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def description(self) -> str:
        return self.config.description or self.name

    def __repr__(self) -> str:
        return (
            f"AgentRole(name={self.config.name!r}, "
            f"tools={self.config.tools}, "
            f"model={self.config.model or 'default'})"
        )

    def summary(self) -> Dict[str, Any]:
        """Return a summary dict for display."""
        return {
            "name": self.config.name,
            "description": self.config.description,
            "tools": self.config.tools,
            "model": self.config.model or "(default)",
            "max_iterations": self.config.max_iterations,
            "temperature": self.config.temperature,
        }


# ---------------------------------------------------------------------------
# Role Discovery & Loading
# ---------------------------------------------------------------------------

def load_roles_from_config(config_data: Dict[str, Any]) -> List[AgentRole]:
    """
    Load agent roles from the ``agent_roles`` section of config.yaml.

    Args:
        config_data: The raw YAML config dict.

    Returns:
        List of AgentRole instances.
    """
    roles_raw = config_data.get("agent_roles", {})
    if not isinstance(roles_raw, dict):
        return []

    roles = []
    for name, data in roles_raw.items():
        if not isinstance(data, dict):
            logger.warning("Skipping invalid agent role definition: %s", name)
            continue
        if not data.get("system_prompt"):
            logger.warning("Agent role '%s' has no system_prompt, skipping", name)
            continue

        cfg = AgentRoleConfig.from_dict(name, data)
        roles.append(AgentRole(cfg))
        logger.info("Loaded agent role: %s", cfg.name)

    return roles


def load_roles_from_directory(directory: Path) -> List[AgentRole]:
    """
    Load agent roles from standalone YAML files in a directory.

    Each YAML file defines one role with the same schema as the inline
    config::

        # ~/.phoenix/roles/researcher.yaml
        name: researcher
        system_prompt: "You are a research specialist."
        tools: [web_search, web_fetch]

    Args:
        directory: Path to scan for role YAML files.

    Returns:
        List of AgentRole instances.
    """
    roles: List[AgentRole] = []
    if not directory.is_dir():
        return roles

    for child in sorted(directory.iterdir()):
        if not child.is_file():
            continue
        if child.suffix not in (".yaml", ".yml"):
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue

        try:
            data = yaml.safe_load(child.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("Failed to parse role file %s: %s", child, exc)
            continue

        name = data.get("name", child.stem)
        if not data.get("system_prompt"):
            logger.warning("Role file %s has no system_prompt, skipping", child)
            continue

        cfg = AgentRoleConfig.from_dict(name, data)
        roles.append(AgentRole(cfg))
        logger.info("Loaded agent role from file: %s (%s)", cfg.name, child)

    return roles


def discover_all_roles(config_data: Optional[Dict[str, Any]] = None) -> List[AgentRole]:
    """
    Discover and load all agent roles from all sources.

    Priority order (later overrides earlier for same name):
    1. ``~/.phoenix/roles/`` directory
    2. ``./roles/`` directory
    3. ``agent_roles`` section in config.yaml

    Args:
        config_data: Optional raw config dict.

    Returns:
        Deduplicated list of AgentRole instances (by name, last wins).
    """
    import os

    role_map: Dict[str, AgentRole] = {}

    # 1. User-level roles directory
    phoenix_home = Path(os.environ.get("PHOENIX_HOME", Path.home() / ".phoenix"))
    user_roles_dir = phoenix_home / "roles"
    for role in load_roles_from_directory(user_roles_dir):
        role_map[role.name] = role

    # 2. Project-level roles directory
    project_roles_dir = Path.cwd() / "roles"
    for role in load_roles_from_directory(project_roles_dir):
        role_map[role.name] = role

    # 3. Config file inline roles
    if config_data:
        for role in load_roles_from_config(config_data):
            role_map[role.name] = role

    return list(role_map.values())
