"""
Skill Registry — Discovery, storage, and matching of skills.

The registry scans designated directories for ``SKILL.yaml`` manifests,
parses them, and provides:

- ``discover()`` — scan all default skill search paths.
- ``match(user_input)`` — find the best skill for a user message
  (based on trigger regex patterns).
- ``get(name)`` / ``list()`` — direct lookup / enumeration.

Default search paths (in priority order):

1. ``~/.phoenix/skills/``         — user-level skills (all projects).
2. ``./skills/``                  — project-level skills (cwd).
3. ``{phoenix_home}/skills/``     — same as #1, explicit.

Users can add extra search paths via ``PHOENIX_SKILL_PATHS`` env var
(colon / semicolon separated).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from phoenix_agent.skills.manifest import SkillManifest, MANIFEST_FILENAME
from phoenix_agent.skills.skill import Skill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Central registry for all discovered skills.

    Usage::

        registry = SkillRegistry()
        registry.discover()
        print(registry.list_names())

        skill = registry.match("帮我分析这个 Excel 文件")
        if skill:
            skill.load()
            agent.use_skill(skill)
    """

    _instance: Optional["SkillRegistry"] = None

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}  # name -> Skill
        self._search_paths: List[Path] = []
        self._compiled_triggers: Dict[str, List[re.Pattern]] = {}

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "SkillRegistry":
        """Return the global singleton registry."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (useful for testing)."""
        if cls._instance:
            for skill in cls._instance._skills.values():
                if skill.is_loaded:
                    skill.unload()
        cls._instance = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(
        self,
        extra_paths: Optional[List[str]] = None,
    ) -> int:
        """Scan all search paths and load any new skills found.

        Args:
            extra_paths: Additional directories to scan.

        Returns:
            Number of newly discovered skills.
        """
        self._search_paths = self._default_search_paths()

        if extra_paths:
            for p in extra_paths:
                self._search_paths.append(Path(p).resolve())

        new_count = 0

        for search_dir in self._search_paths:
            if not search_dir.is_dir():
                logger.debug("Skill search path does not exist: %s", search_dir)
                continue

            for child in search_dir.iterdir():
                if not child.is_dir():
                    continue
                if child.name.startswith(".") or child.name.startswith("_"):
                    continue

                if child.name not in self._skills:
                    manifest = SkillManifest.from_directory(child)
                    if manifest:
                        skill = Skill(manifest)
                        self._skills[manifest.name] = skill
                        self._compile_triggers(manifest.name, manifest.triggers)
                        new_count += 1
                        logger.info(
                            "Discovered skill: %s (v%s) from %s",
                            manifest.name,
                            manifest.version,
                            child,
                        )

        logger.info(
            "Skill discovery complete: %d skills (%d new)",
            len(self._skills),
            new_count,
        )
        return new_count

    def load_from_directory(self, directory: str) -> Optional[Skill]:
        """Load a single skill from an explicit directory path.

        Args:
            directory: Path to the skill directory containing ``SKILL.yaml``.

        Returns:
            The loaded ``Skill``, or ``None`` if the manifest is invalid.
        """
        path = Path(directory).resolve()
        manifest = SkillManifest.from_directory(path)
        if not manifest:
            return None

        # Replace if already exists
        if manifest.name in self._skills and self._skills[manifest.name].is_loaded:
            self._skills[manifest.name].unload()

        skill = Skill(manifest)
        self._skills[manifest.name] = skill
        self._compile_triggers(manifest.name, manifest.triggers)
        return skill

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_names(self) -> List[str]:
        """Return sorted list of all registered skill names."""
        return sorted(self._skills.keys())

    def list_skills(self, include_loaded_only: bool = False) -> List[Skill]:
        """Return list of all skills, optionally filtered by loaded state."""
        skills = list(self._skills.values())
        if include_loaded_only:
            skills = [s for s in skills if s.is_loaded]
        return skills

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def match(self, user_input: str) -> Optional[Skill]:
        """Find the best matching skill for a user message.

        Matching strategy:
        1. Each trigger pattern is compiled as a regex.
        2. The first skill whose triggers produce a match wins.
        3. Skills are checked in discovery order.

        Args:
            user_input: The user's message text.

        Returns:
            The matched ``Skill``, or ``None``.
        """
        if not user_input:
            return None

        for skill_name, patterns in self._compiled_triggers.items():
            for pattern in patterns:
                try:
                    if pattern.search(user_input):
                        logger.debug(
                            "Skill %s matched user input via pattern /%s/",
                            skill_name,
                            pattern.pattern,
                        )
                        return self._skills.get(skill_name)
                except re.error:
                    continue

        return None

    # ------------------------------------------------------------------
    # Management
    # ------------------------------------------------------------------

    def unload_all(self) -> int:
        """Unload all currently loaded skills.

        Returns:
            Number of skills that were unloaded.
        """
        count = 0
        for skill in self._skills.values():
            if skill.is_loaded:
                skill.unload()
                count += 1
        return count

    def remove(self, name: str) -> bool:
        """Remove and unload a skill from the registry.

        Args:
            name: Skill name to remove.

        Returns:
            ``True`` if the skill was found and removed.
        """
        skill = self._skills.pop(name, None)
        if skill:
            if skill.is_loaded:
                skill.unload()
            self._compiled_triggers.pop(name, None)
            return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _default_search_paths(self) -> List[Path]:
        """Build the list of default skill search directories."""
        paths: List[Path] = []

        # 1. User-level: ~/.phoenix/skills/
        phoenix_home = Path(os.environ.get("PHOENIX_HOME", Path.home() / ".phoenix"))
        user_skills = phoenix_home / "skills"
        if user_skills not in paths:
            paths.append(user_skills.resolve())

        # 2. Project-level: ./skills/ (current working directory)
        cwd_skills = Path.cwd() / "skills"
        if cwd_skills not in paths:
            paths.append(cwd_skills.resolve())

        # 3. Extra paths from env var
        env_extra = os.environ.get("PHOENIX_SKILL_PATHS", "").strip()
        if env_extra:
            sep = ";" if os.name == "nt" else ":"
            for p in env_extra.split(sep):
                p = p.strip()
                if p:
                    paths.append(Path(p).resolve())

        return paths

    def _compile_triggers(self, skill_name: str, triggers: List[str]) -> None:
        """Compile trigger patterns into regex objects for fast matching."""
        patterns: List[re.Pattern] = []
        for raw in triggers:
            try:
                patterns.append(re.compile(raw, re.IGNORECASE | re.DOTALL))
            except re.error as exc:
                logger.warning(
                    "Skill %s: invalid trigger pattern /%s/ — %s",
                    skill_name,
                    raw,
                    exc,
                )
        self._compiled_triggers[skill_name] = patterns

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._skills)

    def __repr__(self) -> str:
        return f"SkillRegistry(skills={len(self._skills)}, paths={len(self._search_paths)})"
