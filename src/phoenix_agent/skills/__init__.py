"""
Phoenix Agent — Skills Package

A **skill** is a reusable, self-contained capability bundle that extends
the agent's functionality beyond basic tools.  Each skill provides:

- A dedicated **system prompt** that defines the skill's persona & behaviour.
- A curated set of **tools** the skill needs (auto-enabled when active).
- An optional **workflow** (step-by-step SOP) injected into the prompt.
- Optional **reference documents** loaded from a ``references/`` directory.
- **Lifecycle hooks**: ``on_load`` / ``on_unload`` for setup / teardown.

Skill directory layout::

    ~/.phoenix/skills/my-skill/
    ├── SKILL.yaml          # manifest (metadata, tools, trigger patterns)
    ├── prompt.md           # system prompt / instructions
    ├── references/         # optional knowledge docs (auto-injected)
    │   └── guide.md
    └── tools/              # optional skill-specific Python tools
        └── my_tool.py

Quick start::

    from phoenix_agent.skills import SkillRegistry

    registry = SkillRegistry()
    registry.discover()          # scan default skill directories

    skill = registry.match("帮我分析一下这个 Excel")
    if skill:
        agent.use_skill(skill)

CLI::

    phoenix skill list           # list installed skills
    phoenix skill show <name>    # show skill details
    phoenix skill create <name>  # scaffold a new skill
"""

from phoenix_agent.skills.manifest import SkillManifest
from phoenix_agent.skills.skill import Skill
from phoenix_agent.skills.registry import SkillRegistry

__all__ = [
    "SkillManifest",
    "Skill",
    "SkillRegistry",
]
