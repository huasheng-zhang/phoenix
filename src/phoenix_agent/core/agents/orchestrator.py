"""
Agent Orchestrator — Supervisor-Mode Multi-Agent Collaboration
=============================================================

The ``AgentOrchestrator`` manages a pool of worker ``Agent`` instances
according to registered ``AgentRole`` definitions.  A supervisor agent
(usually the main conversation agent) can **delegate** tasks to these
workers via the ``delegate_to_agent`` and ``ask_agent`` built-in tools.

Delegation flow::

    User message
      -> Supervisor Agent (main Agent)
        -> LLM decides to call delegate_to_agent(role="researcher", task="...")
          -> AgentOrchestrator.delegate()
            -> get or create worker Agent for "researcher" role
            -> worker.run(task)
            -> return DelegationResult
          -> tool result injected back into supervisor context
        -> Supervisor synthesises final answer

Key design choices:
- Workers are **lazy-created** and cached per role name.
- Workers share the same ``ToolRegistry`` singleton (no extra registrations).
- Workers get their own ``MessageHistory`` and ``SessionState`` (isolated context).
- Workers can optionally share the supervisor's ``MemoryStore``.
- Delegation results include structured metadata (role, duration, iterations).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from phoenix_agent.core.agents.roles import AgentRole, AgentRoleConfig
from phoenix_agent.core.config import Config

logger = logging.getLogger(__name__)


@dataclass
class DelegationResult:
    """Result from a delegated agent task."""

    success: bool
    content: str
    role: str = ""
    duration_ms: int = 0
    iterations: int = 0
    error: Optional[str] = None

    def to_json(self) -> str:
        """Serialize for tool result injection."""
        return json.dumps({
            "success": self.success,
            "content": self.content,
            "role": self.role,
            "duration_ms": self.duration_ms,
            "iterations": self.iterations,
            "error": self.error,
        }, ensure_ascii=False)


class AgentOrchestrator:
    """
    Manages worker agents and handles task delegation.

    Usage::

        orchestrator = AgentOrchestrator(config)
        orchestrator.load_roles()

        # Check available roles
        print(orchestrator.list_roles())

        # Delegate a task
        result = orchestrator.delegate(
            role_name="researcher",
            task="Find the latest AI news",
            context="User wants a daily briefing",
        )
        print(result.content)

    Thread safety:
        Delegation is protected by a per-role lock so that concurrent
        delegations to the same role are serialized (same worker agent
        instance), but delegations to *different* roles can run in parallel.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        memory_store=None,
    ):
        """
        Initialize the orchestrator.

        Args:
            config: Phoenix Config object (used to create worker agents).
            memory_store: Optional shared MemoryStore for worker agents.
        """
        self._config = config
        self._memory = memory_store

        # role_name -> AgentRole definition
        self._roles: Dict[str, AgentRole] = {}

        # role_name -> worker Agent instance (lazy-created)
        self._workers: Dict[str, Any] = {}

        # Per-role lock for serialised delegation
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def load_roles(self, config_data: Optional[Dict[str, Any]] = None) -> int:
        """
        Discover and load all agent roles.

        Args:
            config_data: Optional raw config dict for inline roles.

        Returns:
            Number of roles loaded.
        """
        from phoenix_agent.core.agents.roles import discover_all_roles

        if config_data is None and self._config:
            config_data = {}
            if hasattr(self._config, "_file_config"):
                config_data = self._config._file_config

        roles = discover_all_roles(config_data)

        with self._global_lock:
            self._roles = {r.name: r for r in roles}
            # Clear cached workers for removed roles
            removed = [name for name in self._workers if name not in self._roles]
            for name in removed:
                try:
                    self._workers[name].end()
                except Exception:
                    pass
                del self._workers[name]
                self._locks.pop(name, None)

        logger.info(
            "AgentOrchestrator: loaded %d roles (%s)",
            len(self._roles),
            ", ".join(self._roles.keys()) if self._roles else "(none)",
        )
        return len(self._roles)

    def load_roles_from_list(self, roles: List[AgentRole]) -> int:
        """
        Load roles from a pre-built list (useful for testing or dynamic registration).

        Args:
            roles: List of AgentRole instances.

        Returns:
            Number of roles loaded.
        """
        with self._global_lock:
            self._roles = {r.name: r for r in roles}
        return len(self._roles)

    def list_roles(self) -> List[Dict[str, Any]]:
        """Return summaries of all loaded roles."""
        return [r.summary() for r in self._roles.values()]

    def get_role(self, name: str) -> Optional[AgentRole]:
        """Get a role by name, or None if not found."""
        return self._roles.get(name)

    def delegate(
        self,
        role_name: str,
        task: str,
        context: str = "",
        timeout: Optional[int] = None,
    ) -> DelegationResult:
        """
        Delegate a task to a worker agent.

        Creates a worker agent for the given role (if not already cached),
        sends the task, and returns the result.

        Args:
            role_name: Name of the target role.
            task: The task description / prompt for the worker.
            context: Optional context to prepend to the task.
            timeout: Optional timeout in seconds for the worker.

        Returns:
            DelegationResult with the worker's response.
        """
        role = self._roles.get(role_name)
        if not role:
            available = ", ".join(self._roles.keys()) if self._roles else "(none)"
            return DelegationResult(
                success=False,
                content="",
                role=role_name,
                error=f"Unknown role '{role_name}'. Available roles: {available}",
            )

        # Get per-role lock
        with self._global_lock:
            if role_name not in self._locks:
                self._locks[role_name] = threading.Lock()
            lock = self._locks[role_name]

        with lock:
            worker = self._get_or_create_worker(role)
            if worker is None:
                return DelegationResult(
                    success=False,
                    content="",
                    role=role_name,
                    error=f"Failed to create worker for role '{role_name}'",
                )

            return self._run_worker(worker, role, task, context, timeout)

    def ask(
        self,
        role_name: str,
        question: str,
        context: str = "",
    ) -> DelegationResult:
        """
        Ask a worker agent a question (lightweight, no tool iteration).

        Similar to delegate but sets max_iterations=1 to get a quick
        answer without tool use.

        Args:
            role_name: Name of the target role.
            question: The question to ask.
            context: Optional context.

        Returns:
            DelegationResult with the worker's response.
        """
        role = self._roles.get(role_name)
        if not role:
            available = ", ".join(self._roles.keys()) if self._roles else "(none)"
            return DelegationResult(
                success=False,
                content="",
                role=role_name,
                error=f"Unknown role '{role_name}'. Available roles: {available}",
            )

        with self._global_lock:
            if role_name not in self._locks:
                self._locks[role_name] = threading.Lock()
            lock = self._locks[role_name]

        with lock:
            worker = self._get_or_create_worker(role)
            if worker is None:
                return DelegationResult(
                    success=False,
                    content="",
                    role=role_name,
                    error=f"Failed to create worker for role '{role_name}'",
                )

            return self._run_worker(
                worker, role, question, context,
                max_iterations=1,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_worker(self, role: AgentRole):
        """Get a cached worker or create a new one for the given role."""
        if role.name in self._workers:
            return self._workers[role.name]

        try:
            worker = self._create_worker(role)
            self._workers[role.name] = worker
            logger.info("Created worker agent for role: %s", role.name)
            return worker
        except Exception as exc:
            logger.exception("Failed to create worker for role: %s", role.name)
            return None

    def _create_worker(self, role: AgentRole):
        """Create a new Agent instance configured for the given role."""
        from phoenix_agent.core.agent import Agent

        if not self._config:
            raise RuntimeError("No config available for worker creation")

        # Build a role-specific system prompt
        role_prompt = role.config.system_prompt

        # Inject delegation context
        delegation_header = (
            f"# Role: {role.name}\n\n"
            f"You are a specialist agent with the role of '{role.name}'. "
            f"You have been delegated a task by a supervisor agent. "
            f"Focus on completing the specific task assigned to you. "
            f"Provide a clear, concise result.\n\n"
        )
        full_prompt = delegation_header + role_prompt

        # Override max_iterations if specified
        max_iter = role.config.max_iterations or self._config.agent.max_iterations

        # Create the worker agent
        worker = Agent(
            config=self._config,
            system_prompt=full_prompt,
        )
        worker.max_iterations = max_iter

        # Override temperature if specified
        if role.config.temperature is not None:
            worker.config.agent.temperature = role.config.temperature

        # Share memory if available
        if self._memory is not None:
            worker.memory = self._memory

        return worker

    def _run_worker(
        self,
        worker,
        role: AgentRole,
        task: str,
        context: str,
        timeout: Optional[int] = None,
        max_iterations: Optional[int] = None,
    ) -> DelegationResult:
        """Run a task on a worker agent and return the result."""
        start = time.time()

        # Build the full task message
        full_task = task
        if context:
            full_task = f"[Context from supervisor]: {context}\n\n[Task]: {task}"

        # Reset worker history for a clean delegation
        # (workers are stateless between delegations)
        worker.history.clear()

        # Override max_iterations if requested
        original_max = worker.max_iterations
        if max_iterations is not None:
            worker.max_iterations = max_iterations

        try:
            response = worker.run(full_task, stream=False)
            duration = int((time.time() - start) * 1000)

            return DelegationResult(
                success=True,
                content=response,
                role=role.name,
                duration_ms=duration,
                iterations=worker.iteration_count,
            )
        except Exception as exc:
            duration = int((time.time() - start) * 1000)
            logger.exception("Worker %s failed", role.name)
            return DelegationResult(
                success=False,
                content="",
                role=role.name,
                duration_ms=duration,
                error=str(exc),
            )
        finally:
            worker.max_iterations = original_max

    def shutdown(self) -> int:
        """
        End all worker agents and clear the pool.

        Returns:
            Number of workers shut down.
        """
        with self._global_lock:
            count = len(self._workers)
            for name, worker in self._workers.items():
                try:
                    worker.end()
                except Exception:
                    pass
            self._workers.clear()
            self._locks.clear()

        if count:
            logger.info("AgentOrchestrator: shut down %d workers", count)
        return count

    @property
    def role_count(self) -> int:
        """Return number of loaded roles."""
        return len(self._roles)

    @property
    def worker_count(self) -> int:
        """Return number of cached worker agents."""
        return len(self._workers)
