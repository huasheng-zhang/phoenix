"""
Agent Pool — Multi-Conversation Isolation
==========================================

Manages a pool of :class:`~phoenix_agent.core.agent.Agent` instances,
one per ``(channel, chat_id)`` tuple.  This ensures that different
users / group chats have independent conversation history and context.

Design choices
--------------
- **Lazy creation**: Agent instances are created on first message to a
  conversation and cached for reuse.
- **Idle timeout**: Agents that have not received a message for
  ``idle_timeout`` seconds are automatically evicted to free memory.
- **Config sharing**: All agents share the same ``Config`` object;
  only session state differs.
- **Memory sharing**: All agents share the same ``MemoryStore`` instance
  (backed by the same database) so that persistent memories are available
  globally across conversations.
- **Thread safety**: Uses an asyncio lock so that concurrent messages to
  the same conversation are serialized.

Typical usage (inside ``server.py``)::

    pool = AgentPool(config=cfg)

    async def handle_message(msg: ChannelMessage):
        agent = pool.get_agent(msg.channel, msg.platform_id)
        response = await loop.run_in_executor(None, lambda: agent.run(msg.text))
        ...

    # Periodically clean up idle agents
    pool.cleanup()
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class AgentPool:
    """
    Manages per-conversation Agent instances.

    Each unique ``(channel_name, chat_id)`` pair gets its own Agent,
    ensuring complete context isolation between conversations.

    Args:
        config: Phoenix Config object shared by all agents.
        idle_timeout: Seconds of inactivity before an agent is evicted.
                      Default 3600 (1 hour). Set to 0 to disable.
        max_agents: Maximum number of agents to keep alive. When exceeded,
                    the least-recently-used agent is evicted. Default 100.
    """

    def __init__(
        self,
        config,
        idle_timeout: int = 3600,
        max_agents: int = 100,
    ):
        self._config = config
        self._idle_timeout = idle_timeout
        self._max_agents = max_agents

        # (channel, chat_id) -> Agent
        self._pool: Dict[Tuple[str, str], "_AgentEntry"] = {}
        self._lock = threading.Lock()

        # Per-conversation asyncio lock to serialise concurrent agent.run() calls.
        # Also tracks the current cancel_event for in-flight tasks so new
        # messages can cancel stale ones.
        self._conversation_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._conversation_cancel: Dict[Tuple[str, str], threading.Event] = {}

        # Shared memory store (all agents see the same memories)
        self._memory = None
        if self._config.agent.memory_enabled:
            try:
                from phoenix_agent.core.state import Database, MemoryStore
                db = Database(self._config.storage.db_path)
                self._memory = MemoryStore(db)
                logger.info(
                    "AgentPool: shared memory store initialised (%d memories)",
                    self._memory.count(),
                )
            except Exception as exc:
                logger.warning("AgentPool: failed to init memory store: %s", exc)

        # Initialize multi-agent orchestrator
        self._orchestrator = None
        try:
            from phoenix_agent.core.agents.orchestrator import AgentOrchestrator
            config_data = {}
            if hasattr(self._config, "_file_config"):
                config_data = self._config._file_config
            self._orchestrator = AgentOrchestrator(
                config=self._config,
                memory_store=self._memory,
            )
            role_count = self._orchestrator.load_roles(config_data)
            if role_count > 0:
                logger.info(
                    "AgentPool: multi-agent orchestrator initialised (%d roles)",
                    role_count,
                )
                # Register globally for tool access
                from phoenix_agent.tools.builtin import init_orchestrator
                init_orchestrator(self._orchestrator)
        except Exception as exc:
            logger.debug("AgentPool: multi-agent orchestrator not available: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_agent(
        self,
        channel: str,
        chat_id: str,
    ):
        """
        Get or create an Agent for the given conversation.

        Args:
            channel: Channel name (e.g. "dingtalk", "telegram").
            chat_id: Platform-specific conversation identifier.

        Returns:
            An :class:`~phoenix_agent.core.agent.Agent` instance.
        """
        key = (channel, chat_id)

        with self._lock:
            entry = self._pool.get(key)

            if entry is not None:
                # Touch last-access time
                entry.last_access = time.time()
                logger.debug(
                    "AgentPool: reused agent for %s/%s (session=%s)",
                    channel, chat_id[:16], entry.agent.session.session_id[:8],
                )
                return entry.agent

            # Evict if at capacity
            if len(self._pool) >= self._max_agents:
                self._evict_lru()

            # Create new agent
            agent = self._create_agent(channel, chat_id)
            self._pool[key] = _AgentEntry(agent=agent, last_access=time.time())

            logger.info(
                "AgentPool: created agent for %s/%s (session=%s, pool_size=%d)",
                channel, chat_id[:16], agent.session.session_id[:8],
                len(self._pool),
            )
            return agent

    async def acquire_conversation_lock(
        self, channel: str, chat_id: str
    ) -> threading.Event:
        """
        Acquire a per-conversation lock, ensuring only one agent.run()
        executes at a time for a given conversation.

        If a previous run is still in progress, its cancel_event is set
        before we wait for the lock, signaling it to stop ASAP.

        Returns:
            A new ``threading.Event`` that the caller should pass to
            ``agent.run()`` as its ``cancel_event`` parameter.
        """
        key = (channel, chat_id)

        # Cancel any in-flight task for this conversation
        old_event = self._conversation_cancel.get(key)
        if old_event is not None and not old_event.is_set():
            logger.info(
                "AgentPool: cancelling in-flight task for %s/%s",
                channel, chat_id[:16],
            )
            old_event.set()  # Signal cancellation

        # Get or create asyncio lock for this conversation
        if key not in self._conversation_locks:
            self._conversation_locks[key] = asyncio.Lock()

        await self._conversation_locks[key].acquire()

        # Create a fresh cancel_event for the new task
        cancel_event = threading.Event()
        self._conversation_cancel[key] = cancel_event

        return cancel_event

    def release_conversation_lock(
        self, channel: str, chat_id: str
    ) -> None:
        """Release the per-conversation lock after agent.run() completes."""
        key = (channel, chat_id)
        # Clear cancel_event
        self._conversation_cancel.pop(key, None)
        # Release the asyncio lock
        lock = self._conversation_locks.get(key)
        if lock and lock.locked():
            lock.release()
        logger.debug(
            "AgentPool: released conversation lock for %s/%s",
            channel, chat_id[:16],
        )

    def remove_agent(self, channel: str, chat_id: str) -> bool:
        """
        Manually remove an agent from the pool.

        This ends the agent's session and frees its resources.

        Returns:
            True if an agent was found and removed.
        """
        key = (channel, chat_id)

        with self._lock:
            entry = self._pool.pop(key, None)
            if entry is None:
                return False

            entry.agent.end()
            logger.info(
                "AgentPool: removed agent for %s/%s (pool_size=%d)",
                channel, chat_id[:16], len(self._pool),
            )
            return True

    def cleanup(self) -> int:
        """
        Evict all idle agents (last access > idle_timeout).

        Returns:
            Number of agents evicted.
        """
        if self._idle_timeout <= 0:
            return 0

        now = time.time()
        evicted = 0

        with self._lock:
            keys_to_remove = [
                key for key, entry in self._pool.items()
                if (now - entry.last_access) > self._idle_timeout
            ]

            for key in keys_to_remove:
                entry = self._pool.pop(key)
                try:
                    entry.agent.end()
                except Exception:
                    pass
                evicted += 1

        if evicted:
            logger.info("AgentPool: evicted %d idle agents (pool_size=%d)", evicted, len(self._pool))

        return evicted

    @property
    def size(self) -> int:
        """Return the current number of agents in the pool."""
        return len(self._pool)

    @property
    def memory(self):
        """Return the shared MemoryStore instance (or None)."""
        return self._memory

    @property
    def orchestrator(self):
        """Return the AgentOrchestrator instance (or None)."""
        return self._orchestrator

    def stats(self) -> Dict[str, int]:
        """Return pool statistics."""
        return {
            "pool_size": len(self._pool),
            "max_agents": self._max_agents,
            "idle_timeout": self._idle_timeout,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_agent(self, channel: str, chat_id: str):
        """Create a new Agent instance for a conversation."""
        from phoenix_agent.core.agent import Agent

        # Build a session title from channel + chat_id for easier debugging
        title = f"{channel}/{chat_id[:32]}"

        agent = Agent(config=self._config)
        agent.session.update_title(title)

        # Share the memory store (avoid each agent opening its own DB connection)
        if self._memory is not None:
            agent.memory = self._memory

        return agent

    def _evict_lru(self) -> None:
        """Evict the least-recently-used agent from the pool."""
        if not self._pool:
            return

        lru_key = min(self._pool, key=lambda k: self._pool[k].last_access)
        entry = self._pool.pop(lru_key)

        try:
            entry.agent.end()
        except Exception:
            pass

        logger.info(
            "AgentPool: LRU evicted agent for %s/%s (pool_size=%d)",
            lru_key[0], lru_key[1][:16], len(self._pool),
        )

    def shutdown(self) -> int:
        """
        End all agents and clear the pool.

        Returns:
            Number of agents ended.
        """
        with self._lock:
            count = len(self._pool)
            for entry in self._pool.values():
                try:
                    entry.agent.end()
                except Exception:
                    pass
            self._pool.clear()

        if count:
            logger.info("AgentPool: shut down %d agents", count)

        # Close shared memory DB
        if self._memory:
            try:
                self._memory.db.close()
            except Exception:
                pass

        # Shut down orchestrator workers
        if self._orchestrator:
            try:
                self._orchestrator.shutdown()
            except Exception:
                pass

        return count


# ---------------------------------------------------------------------------
# Internal entry wrapper
# ---------------------------------------------------------------------------

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phoenix_agent.core.agent import Agent


class _AgentEntry:
    """Lightweight wrapper tracking an agent and its last access time."""

    __slots__ = ("agent", "last_access")

    def __init__(self, agent: "Agent", last_access: float):
        self.agent = agent
        self.last_access = last_access

    def touch(self) -> None:
        self.last_access = time.time()


# Import for type hints at module level
from typing import Dict  # noqa: E402 (needed for stats return type)
