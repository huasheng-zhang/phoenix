"""
Multi-Agent Collaboration Module
=================================

Provides Supervisor-mode multi-agent collaboration for Phoenix Agent.

Architecture:
- ``AgentRole``     — Defines an agent persona (name, prompt, tools, model).
- ``AgentOrchestrator`` — Manages a pool of role-based agents and routes
  delegation requests from a supervisor agent to worker agents.

Usage::

    orchestrator = AgentOrchestrator(config)
    orchestrator.load_roles()

    # A supervisor agent delegates work to a worker
    result = orchestrator.delegate(
        role_name="researcher",
        task="Find the latest AI news",
        context="User wants a daily briefing",
    )
"""

from phoenix_agent.core.agents.roles import AgentRole, AgentRoleConfig
from phoenix_agent.core.agents.orchestrator import AgentOrchestrator, DelegationResult

__all__ = [
    "AgentRole",
    "AgentRoleConfig",
    "AgentOrchestrator",
    "DelegationResult",
]
