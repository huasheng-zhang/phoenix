"""
Tests for the Skill extension system.

Covers:
- SkillManifest: YAML parsing, path resolution
- Skill: lifecycle (load/unload), prompt building, env management
- SkillRegistry: discovery, matching, lookup
- Agent integration: use_skill, clear_skill, auto-matching
"""

import os
import sys
import tempfile
from pathlib import Path
from textwrap import dedent
from unittest import mock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def skill_dir(tmp_path):
    """Create a minimal skill directory with SKILL.yaml and prompt.md."""
    d = tmp_path / "test-skill"
    d.mkdir()

    manifest = {
        "name": "test-skill",
        "version": "1.0.0",
        "description": "A test skill for unit testing.",
        "triggers": ["analyze.*data", "数据分析", "process spreadsheet"],
        "tools": ["read_file", "run_command"],
        "tools_extra": [],
        "env": {"SKILL_TEST_VAR": "hello"},
        "settings": {"max_rows": 100},
    }
    with open(d / "SKILL.yaml", "w", encoding="utf-8") as f:
        yaml.dump(manifest, f)

    (d / "prompt.md").write_text(
        "# Test Skill\n\nYou are a data analyst.\nFollow these steps:\n1. Read the data\n2. Analyze\n",
        encoding="utf-8",
    )

    refs = d / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide\n\nThis is a reference doc.", encoding="utf-8")

    return d


@pytest.fixture
def skill_dir_no_prompt(tmp_path):
    """Create a skill directory without prompt.md."""
    d = tmp_path / "minimal-skill"
    d.mkdir()

    manifest = {
        "name": "minimal-skill",
        "version": "0.1.0",
        "description": "A minimal skill.",
        "triggers": ["minimal"],
    }
    with open(d / "SKILL.yaml", "w", encoding="utf-8") as f:
        yaml.dump(manifest, f)

    return d


@pytest.fixture(autouse=True)
def reset_tool_registry():
    """Reset ToolRegistry singleton before each test."""
    from phoenix_agent.tools.registry import ToolRegistry
    # Clear existing tools to avoid cross-test pollution
    inst = ToolRegistry.get_instance()
    inst._tools.clear()
    inst._categories = {cat: [] for cat in inst._categories}
    yield
    inst._tools.clear()
    inst._categories = {cat: [] for cat in inst._categories}


@pytest.fixture(autouse=True)
def reset_skill_registry():
    """Reset SkillRegistry singleton before each test."""
    from phoenix_agent.skills.registry import SkillRegistry
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


# ===========================================================================
# SkillManifest tests
# ===========================================================================

class TestSkillManifest:

    def test_from_directory_success(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(skill_dir)
        assert manifest is not None
        assert manifest.name == "test-skill"
        assert manifest.version == "1.0.0"
        assert manifest.description == "A test skill for unit testing."
        assert manifest.triggers == ["analyze.*data", "数据分析", "process spreadsheet"]
        assert manifest.tools == ["read_file", "run_command"]
        assert manifest.env == {"SKILL_TEST_VAR": "hello"}
        assert manifest.settings == {"max_rows": 100}

    def test_from_directory_missing_manifest(self, tmp_path):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(tmp_path / "nonexistent")
        assert manifest is None

    def test_from_directory_missing_name(self, tmp_path):
        from phoenix_agent.skills.manifest import SkillManifest

        d = tmp_path / "bad-skill"
        d.mkdir()
        (d / "SKILL.yaml").write_text("version: 1.0.0\n", encoding="utf-8")

        manifest = SkillManifest.from_directory(d)
        assert manifest is None

    def test_prompt_path_exists(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(skill_dir)
        assert manifest.prompt_path is not None
        assert manifest.prompt_path.name == "prompt.md"

    def test_prompt_path_missing(self, skill_dir_no_prompt):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(skill_dir_no_prompt)
        assert manifest.prompt_path is None

    def test_references_dir(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(skill_dir)
        assert manifest.references_dir is not None
        files = manifest.list_reference_files()
        assert len(files) == 1
        assert files[0].name == "guide.md"

    def test_references_dir_missing(self, skill_dir_no_prompt):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(skill_dir_no_prompt)
        assert manifest.references_dir is None
        assert manifest.list_reference_files() == []

    def test_tools_dir_missing(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(skill_dir)
        assert manifest.tools_dir is None
        assert manifest.list_tool_files() == []

    def test_to_dict(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest

        manifest = SkillManifest.from_directory(skill_dir)
        d = manifest.to_dict()
        assert d["name"] == "test-skill"
        assert d["version"] == "1.0.0"
        assert "source_path" in d


# ===========================================================================
# Skill tests
# ===========================================================================

class TestSkill:

    def test_load_and_unload(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)

        assert not skill.is_loaded
        skill.load()
        assert skill.is_loaded
        skill.unload()
        assert not skill.is_loaded

    def test_double_load_is_noop(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)
        skill.load()
        skill.load()  # second call should be no-op
        assert skill.is_loaded

    def test_unload_without_load(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)
        skill.unload()  # should not raise
        assert not skill.is_loaded

    def test_system_prompt_with_prompt_md(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)
        skill.load()

        prompt = skill.system_prompt
        assert "Test Skill" in prompt
        assert "data analyst" in prompt
        # Reference doc should be included
        assert "Guide" in prompt

    def test_system_prompt_without_prompt_md(self, skill_dir_no_prompt):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir_no_prompt)
        skill = Skill(manifest)
        skill.load()

        assert skill.system_prompt == ""

    def test_env_set_and_restore(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)

        # Ensure the var is not set
        os.environ.pop("SKILL_TEST_VAR", None)

        skill.load()
        assert os.environ.get("SKILL_TEST_VAR") == "hello"

        skill.unload()
        assert os.environ.get("SKILL_TEST_VAR") is None

    def test_name_and_description(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)

        assert skill.name == "test-skill"
        assert skill.description == "A test skill for unit testing."

    def test_summary(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)

        s = skill.summary()
        assert s["name"] == "test-skill"
        assert s["has_prompt"] is True
        assert s["references"] == 1
        assert s["loaded"] is False

    def test_repr(self, skill_dir):
        from phoenix_agent.skills.manifest import SkillManifest
        from phoenix_agent.skills.skill import Skill

        manifest = SkillManifest.from_directory(skill_dir)
        skill = Skill(manifest)
        assert "test-skill" in repr(skill)
        assert "unloaded" in repr(skill)

        skill.load()
        assert "loaded" in repr(skill)


# ===========================================================================
# SkillRegistry tests
# ===========================================================================

class TestSkillRegistry:

    def test_discover_from_directory(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        count = registry.discover(extra_paths=[str(skill_dir.parent)])
        assert count >= 1
        assert "test-skill" in registry.list_names()

    def test_discover_nonexistent_path(self):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        count = registry.discover(extra_paths=["/nonexistent/path"])
        assert count == 0

    def test_get_skill(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        skill = registry.get("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"

    def test_get_nonexistent_skill(self):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        assert registry.get("nonexistent") is None

    def test_match_triggers(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        # Should match "analyze.*data" pattern
        matched = registry.match("Please analyze this data for me")
        assert matched is not None
        assert matched.name == "test-skill"

        # Should match "数据分析" pattern
        matched = registry.match("帮我做一下数据分析")
        assert matched is not None
        assert matched.name == "test-skill"

    def test_no_match(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        matched = registry.match("hello world")
        assert matched is None

    def test_match_empty_input(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        assert registry.match("") is None
        assert registry.match(None) is None

    def test_list_names(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        names = registry.list_names()
        assert "test-skill" in names
        assert names == sorted(names)

    def test_list_skills(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        all_skills = registry.list_skills()
        assert len(all_skills) >= 1

        loaded_skills = registry.list_skills(include_loaded_only=True)
        assert len(loaded_skills) == 0

    def test_remove_skill(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        assert registry.remove("test-skill") is True
        assert registry.get("test-skill") is None

    def test_remove_nonexistent_skill(self):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        assert registry.remove("nonexistent") is False

    def test_load_from_directory(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        skill = registry.load_from_directory(str(skill_dir))
        assert skill is not None
        assert skill.name == "test-skill"

    def test_load_from_bad_directory(self, tmp_path):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        skill = registry.load_from_directory(str(tmp_path))
        assert skill is None

    def test_unload_all(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.discover(extra_paths=[str(skill_dir.parent)])

        skill = registry.get("test-skill")
        skill.load()
        assert skill.is_loaded

        count = registry.unload_all()
        assert count >= 1
        assert not skill.is_loaded


# ===========================================================================
# Agent integration tests
# ===========================================================================

class TestAgentSkillIntegration:

    def test_use_skill_and_build_prompt(self, skill_dir):
        from phoenix_agent.skills.registry import SkillRegistry
        from phoenix_agent.core.config import ToolConfig, AgentConfig
        from phoenix_agent.core.agent import Agent

        # Create a mock config with needed attributes
        mock_config = mock.MagicMock()
        mock_config.provider = mock.Mock(
            type="openai", model="gpt-4o", api_key="test-key",
            base_url=None, timeout=120, max_tokens=8192,
        )
        mock_config.agent = AgentConfig(
            system_prompt="Base prompt.", max_iterations=10,
        )
        mock_config.tools = ToolConfig(
            enabled=["file", "web", "system", "utility"],
        )

        # Construct agent directly, bypassing __init__
        agent = Agent.__new__(Agent)
        agent.config = mock_config
        agent.session = mock.Mock()
        agent.history = mock.Mock()
        agent.iteration_count = 0
        agent.max_iterations = 10
        agent.on_tool_call = None
        agent.on_response = None
        agent.on_skill_change = None
        agent._active_skill = None
        agent._skill_registry = None
        agent.system_prompt = "Base prompt."
        agent.tools = mock.Mock()
        agent.memory = None

        registry = SkillRegistry()
        skill = registry.load_from_directory(str(skill_dir))
        skill.load()

        agent.use_skill(skill)
        assert agent.active_skill is skill

        # Check effective prompt includes skill content
        prompt = agent._build_effective_system_prompt()
        assert "Base prompt." in prompt
        assert "test-skill" in prompt
        assert "data analyst" in prompt

        # Check tool filter includes skill tools
        tools = agent._build_effective_tool_filter()
        assert "read_file" in tools
        assert "run_command" in tools

        # Clear skill
        name = agent.clear_skill()
        assert name == "test-skill"
        assert agent.active_skill is None

    def test_clear_skill_none(self):
        from phoenix_agent.core.agent import Agent

        agent = Agent.__new__(Agent)
        agent._active_skill = None
        assert agent.clear_skill() is None
