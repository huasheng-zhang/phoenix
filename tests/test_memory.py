"""Tests for the MemoryStore and Agent memory integration."""

import pytest
import tempfile
import os
import shutil

from phoenix_agent.core.state import Database, MemoryStore


class TestMemoryStore:
    """Test MemoryStore CRUD and query operations."""

    @pytest.fixture()
    def db(self, tmp_path):
        """Create a temporary database for each test (auto-cleaned by tmp_path)."""
        db_path = str(tmp_path / "test_memory.db")
        db = Database(db_path)
        yield db
        db.close()

    @pytest.fixture()
    def store(self, db):
        """Create a MemoryStore backed by the test database."""
        return MemoryStore(db)

    # -- CRUD --

    def test_save_and_get(self, store):
        """Save a memory and retrieve it by key."""
        store.save("user_name", "Alice")
        m = store.get("user_name")
        assert m is not None
        assert m["content"] == "Alice"
        assert m["category"] == "general"

    def test_save_with_category(self, store):
        """Save a memory with a custom category."""
        store.save("lang", "Python", category="preference")
        m = store.get("lang")
        assert m["category"] == "preference"

    def test_save_upsert(self, store):
        """Updating an existing memory should overwrite content."""
        store.save("key1", "old value")
        store.save("key1", "new value", category="updated")
        m = store.get("key1")
        assert m["content"] == "new value"
        assert m["category"] == "updated"

    def test_get_nonexistent(self, store):
        """Getting a nonexistent key returns None."""
        assert store.get("no_such_key") is None

    def test_delete(self, store):
        """Delete a memory by key."""
        store.save("tmp", "temporary")
        assert store.delete("tmp") is True
        assert store.get("tmp") is None

    def test_delete_nonexistent(self, store):
        """Deleting a nonexistent key returns False."""
        assert store.delete("nope") is False

    def test_delete_category(self, store):
        """Delete all memories in a category."""
        store.save("a", "1", category="cat1")
        store.save("b", "2", category="cat1")
        store.save("c", "3", category="cat2")
        deleted = store.delete_category("cat1")
        assert deleted == 2
        assert store.get("a") is None
        assert store.get("b") is None
        assert store.get("c") is not None

    def test_clear(self, store):
        """Clear all memories."""
        store.save("a", "1")
        store.save("b", "2")
        store.save("c", "3")
        deleted = store.clear()
        assert deleted == 3
        assert store.count() == 0

    # -- Query --

    def test_load_all(self, store):
        """Load all memories as a dict."""
        store.save("x", "10")
        store.save("y", "20")
        all_m = store.load_all()
        assert all_m == {"x": "10", "y": "20"}

    def test_load_all_detail(self, store):
        """Load all memories with full metadata."""
        store.save("k1", "v1", category="cat")
        details = store.load_all_detail()
        assert len(details) == 1
        assert details[0]["key"] == "k1"
        assert details[0]["category"] == "cat"
        assert "created_at" in details[0]
        assert "updated_at" in details[0]

    def test_recall_by_key(self, store):
        """Recall memories by key match."""
        store.save("project_name", "Phoenix")
        store.save("user_name", "Alice")
        results = store.recall("project")
        assert len(results) == 1
        assert results[0]["key"] == "project_name"

    def test_recall_by_content(self, store):
        """Recall memories by content match."""
        store.save("fact1", "Python is a programming language")
        store.save("fact2", "Java is also a language")
        results = store.recall("programming")
        assert len(results) == 1
        assert results[0]["key"] == "fact1"

    def test_recall_no_match(self, store):
        """Recall with no matches returns empty list."""
        store.save("a", "b")
        results = store.recall("zzz")
        assert results == []

    def test_count(self, store):
        """Count returns the correct number of memories."""
        assert store.count() == 0
        store.save("a", "1")
        store.save("b", "2")
        assert store.count() == 2

    def test_source_session(self, store):
        """Source session is stored on save."""
        store.save("k", "v", source_session="sess-123")
        m = store.get("k")
        assert m["source_session"] == "sess-123"

    # -- Prompt generation --

    def test_build_context_block_empty(self, store):
        """Empty store produces empty block."""
        assert store.build_context_block() == ""

    def test_build_context_block_nonempty(self, store):
        """Non-empty store produces a formatted block."""
        store.save("user", "Alice", category="identity")
        store.save("lang", "Python")
        block = store.build_context_block()
        assert "# Persistent Memory" in block
        assert "user" in block
        assert "Alice" in block
        assert "[identity]" in block
        assert "lang" in block
        assert "Python" in block

    def test_build_context_block_max(self, store):
        """Respects max_memories limit."""
        for i in range(10):
            store.save(f"key{i}", f"value{i}")
        block = store.build_context_block(max_memories=3)
        # Should only include the 3 most recent (by updated_at DESC)
        lines = [l for l in block.splitlines() if l.startswith("- **")]
        assert len(lines) == 3
