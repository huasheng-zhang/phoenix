"""Tests for AgentPool — multi-conversation isolation."""

import time
from unittest import mock

import pytest

from phoenix_agent.core.pool import AgentPool, _AgentEntry


class _FakeConfig:
    """Minimal mock Config."""

    def __init__(self):
        self.agent = mock.MagicMock()
        self.agent.memory_enabled = False
        self.storage = mock.MagicMock()


class TestAgentEntry:
    """Test the _AgentEntry wrapper."""

    def test_touch_updates_last_access(self):
        entry = _AgentEntry(agent=mock.Mock(), last_access=1000.0)
        entry.touch()
        assert entry.last_access > 1000.0


class TestAgentPool:
    """Test AgentPool CRUD, reuse, and eviction."""

    @pytest.fixture()
    def config(self):
        return _FakeConfig()

    @pytest.fixture()
    def pool(self, config):
        return AgentPool(config=config, idle_timeout=0, max_agents=10)

    def _make_fake_agent(self):
        """Create a fake Agent with session mock."""
        agent = mock.MagicMock()
        agent.session.session_id = "test-session-abc123"
        agent.memory = None
        return agent

    def test_get_agent_creates_new(self, pool):
        """First call to get_agent creates a new Agent."""
        with mock.patch.object(pool, "_create_agent", wraps=lambda ch, cid: self._make_fake_agent()):
            agent = pool.get_agent("dingtalk", "chat-001")
        assert agent is not None
        assert pool.size == 1

    def test_get_agent_reuses_existing(self, pool):
        """Same (channel, chat_id) returns the same Agent instance."""
        fake = self._make_fake_agent()
        with mock.patch.object(pool, "_create_agent", return_value=fake):
            a1 = pool.get_agent("dingtalk", "chat-001")
            a2 = pool.get_agent("dingtalk", "chat-001")
        assert a1 is a2
        assert pool.size == 1

    def test_different_conversations_get_different_agents(self, pool):
        """Different (channel, chat_id) pairs get different Agents."""
        count = 0
        agents = []

        def fake_create(ch, cid):
            nonlocal count
            count += 1
            a = self._make_fake_agent()
            a.session.session_id = f"session-{count}"
            agents.append(a)
            return a

        with mock.patch.object(pool, "_create_agent", side_effect=fake_create):
            a1 = pool.get_agent("dingtalk", "chat-001")
            a2 = pool.get_agent("dingtalk", "chat-002")
            a3 = pool.get_agent("telegram", "chat-001")

        assert a1 is not a2
        assert a1 is not a3
        assert a2 is not a3
        assert pool.size == 3

    def test_remove_agent(self, pool):
        """remove_agent removes and ends the agent."""
        with mock.patch.object(pool, "_create_agent", return_value=self._make_fake_agent()):
            pool.get_agent("dingtalk", "chat-001")
        assert pool.size == 1

        result = pool.remove_agent("dingtalk", "chat-001")
        assert result is True
        assert pool.size == 0

        # Remove non-existent returns False
        result = pool.remove_agent("dingtalk", "chat-999")
        assert result is False

    def test_max_agents_lru_eviction(self, config):
        """When max_agents is exceeded, LRU agent is evicted."""
        pool = AgentPool(config=config, idle_timeout=0, max_agents=2)

        agents_list = []
        def fake_create(ch, cid):
            a = self._make_fake_agent()
            a.session.session_id = f"session-{cid}"
            agents_list.append(a)
            return a

        with mock.patch.object(pool, "_create_agent", side_effect=fake_create):
            a1 = pool.get_agent("dingtalk", "chat-001")
            a2 = pool.get_agent("dingtalk", "chat-002")
            assert pool.size == 2

            # Access a1 to make it more recently used
            pool.get_agent("dingtalk", "chat-001")

            # Adding a third should evict a2 (LRU)
            a3 = pool.get_agent("dingtalk", "chat-003")
            assert pool.size == 2

            # a1 should still be there (most recently used)
            a1_again = pool.get_agent("dingtalk", "chat-001")
            assert a1 is a1_again

    def test_idle_timeout_cleanup(self, config):
        """cleanup() evicts agents that haven't been accessed recently."""
        pool = AgentPool(config=config, idle_timeout=1, max_agents=100)

        with mock.patch.object(pool, "_create_agent", return_value=self._make_fake_agent()):
            pool.get_agent("dingtalk", "chat-old")
            pool.get_agent("dingtalk", "chat-new")

        assert pool.size == 2

        # Manually age the "old" entry
        old_key = ("dingtalk", "chat-old")
        pool._pool[old_key].last_access = time.time() - 10  # 10 seconds ago

        evicted = pool.cleanup()
        assert evicted == 1
        assert pool.size == 1
        assert old_key not in pool._pool

    def test_cleanup_disabled_when_zero(self, pool):
        """cleanup() is a no-op when idle_timeout is 0."""
        with mock.patch.object(pool, "_create_agent", return_value=self._make_fake_agent()):
            pool.get_agent("dingtalk", "chat-001")
        evicted = pool.cleanup()
        assert evicted == 0
        assert pool.size == 1

    def test_shutdown_ends_all_agents(self, pool):
        """shutdown() ends all agents and clears the pool."""
        with mock.patch.object(pool, "_create_agent", return_value=self._make_fake_agent()):
            pool.get_agent("dingtalk", "chat-001")
            pool.get_agent("telegram", "chat-002")

        count = pool.shutdown()
        assert count == 2
        assert pool.size == 0

    def test_shutdown_can_be_called_safely_on_empty_pool(self, pool):
        """shutdown() on empty pool doesn't crash."""
        count = pool.shutdown()
        assert count == 0

    def test_stats(self, pool):
        """stats() returns correct pool information."""
        with mock.patch.object(pool, "_create_agent", return_value=self._make_fake_agent()):
            pool.get_agent("dingtalk", "chat-001")
            pool.get_agent("telegram", "chat-002")

        stats = pool.stats()
        assert stats["pool_size"] == 2
        assert stats["max_agents"] == 10
        assert stats["idle_timeout"] == 0

    def test_agent_session_has_title(self, pool):
        """Created agents get a session title with channel/chat info."""
        fake = self._make_fake_agent()
        create_spy = mock.MagicMock(return_value=fake)
        with mock.patch.object(pool, "_create_agent", create_spy):
            agent = pool.get_agent("dingtalk", "chat-abc123")
        # _create_agent is called with the right args
        create_spy.assert_called_once_with("dingtalk", "chat-abc123")

    def test_shared_memory_store(self, config):
        """When memory is enabled, all agents share the same MemoryStore."""
        config.agent.memory_enabled = True

        mock_db = mock.MagicMock()
        mock_mem = mock.MagicMock()
        mock_mem.count.return_value = 3

        # Patch at the source module where the lazy import resolves
        with mock.patch("phoenix_agent.core.state.Database", return_value=mock_db), \
             mock.patch("phoenix_agent.core.state.MemoryStore", return_value=mock_mem):
            pool = AgentPool(config=config)

        assert pool.memory is mock_mem

        fake = self._make_fake_agent()
        with mock.patch.object(pool, "_create_agent", return_value=fake):
            a1 = pool.get_agent("dingtalk", "chat-001")
            a2 = pool.get_agent("telegram", "chat-002")
        # All agents should share the same memory store
        assert pool.memory is mock_mem

