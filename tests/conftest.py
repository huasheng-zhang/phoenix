"""pytest configuration and fixtures."""

import pytest
import tempfile
import os
from pathlib import Path


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_config(temp_dir):
    """Create a mock configuration file."""
    config_path = temp_dir / "config.yaml"
    config_path.write_text("""
provider:
  type: openai
  model: gpt-4o-mini
  api_key: test-key
agent:
  max_iterations: 10
tools:
  enabled:
    - utility
storage:
  db_path: "{db_path}"
""".format(db_path=str(temp_dir / "test.db")))
    return config_path
