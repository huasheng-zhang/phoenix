"""
Web UI Routes
=============

Provides a chat web interface for Phoenix Agent, served alongside the
main channel server.  All routes are mounted under ``/`` when the web UI
is enabled.

Routes
------
- ``GET  /``                → serves the SPA HTML page
- ``POST /api/chat``        → send a message, returns full response
- ``POST /api/chat/stream`` → send a message, returns SSE stream
- ``POST /api/session/new`` → reset the current session (new conversation)
- ``GET  /api/history``     → return current conversation history
- ``GET  /health``          → health check (same as the main server one)

Authentication
--------------
An optional ``web.token`` field in ``config.yaml`` gates access:
if set, every request must include ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route

logger = logging.getLogger(__name__)

# Path to the static HTML file (bundled with the package)
_STATIC_DIR = Path(__file__).parent / "static"


def build_web_routes(pool, config=None) -> list:
    """
    Build the route list for the Web UI.

    Args:
        pool:   The AgentPool instance (shared with the main server).
        config: Optional Config — read ``web.token`` for auth.

    Returns:
        A list of Starlette Route objects (not a Starlette app).
    """
    from phoenix_agent.core.config import get_config
    cfg = config or get_config()

    # --- Optional Bearer-token auth ---
    web_cfg: Dict[str, Any] = {}
    raw_channels = {}
    if hasattr(cfg, "_file_config"):
        raw_channels = cfg._file_config.get("channels", {})
    web_cfg = raw_channels.get("web", {})
    auth_token = web_cfg.get("token", "") or ""

    import re

    # --- @mention utility ---
    _MENTION_RE = re.compile(r'@([\w-]+)\s+([\s\S]*)')

    # --- Tool display summary helper ---
    def _tool_display_summary(tool_name: str, tool_args: dict) -> str:
        """Generate a human-readable summary for a tool invocation."""
        _SUMMARY_MAP = {
            "read_file": lambda a: a.get("file_path", a.get("path", "file")),
            "write_file": lambda a: a.get("file_path", a.get("path", "file")),
            "edit_file": lambda a: a.get("file_path", a.get("path", "file")),
            "list_directory": lambda a: a.get("directory_path", a.get("path", "directory")),
            "grep": lambda a: f'"{a.get("pattern", "")}" in {a.get("file_path", "...")}',
            "glob_files": lambda a: f'{a.get("pattern", "")} in {a.get("base_directory", "...")}',
            "run_command": lambda a: a.get("command", "")[:80],
            "web_search": lambda a: a.get("query", a.get("keyword", "")),
            "web_fetch": lambda a: a.get("url", "")[:60],
            "send_message": lambda a: f'to {a.get("recipient", "")}',
            "save_memory": lambda a: a.get("key", "")[:40],
            "recall_memory": lambda a: a.get("query", "")[:40],
            "execute_python": lambda a: "(running code)",
            "execute_bash": lambda a: a.get("command", "")[:80],
            "list_scheduled_tasks": lambda a: "(listing tasks)",
            "add_scheduled_task": lambda a: a.get("task_name", a.get("name", "task")),
            "remove_scheduled_task": lambda a: a.get("task_name", a.get("name", "task")),
            "delegate_to_agent": lambda a: f"@{a.get('role', '')} — {a.get('task', '')[:50]}",
            "ask_agent": lambda a: f"@{a.get('role', '')} — {a.get('question', '')[:50]}",
        }
        gen = _SUMMARY_MAP.get(tool_name)
        if gen:
            try:
                s = gen(tool_args or {})
                return s if isinstance(s, str) else str(s)
            except Exception:
                pass
        # Fallback: tool_name + first arg value
        if tool_args:
            first_val = next(iter(tool_args.values()), "")
            if isinstance(first_val, str) and len(first_val) > 60:
                first_val = first_val[:60] + "..."
            return f"{tool_name}({first_val})" if first_val else tool_name
        return tool_name

    def _parse_mentions(message: str) -> list:
        """
        Parse @mention patterns from user message.

        Returns a list of (role_name, task_text) tuples.
        Supports: "@role task description" anywhere in the message.
        """
        mentions = []
        for m in _MENTION_RE.finditer(message):
            mentions.append((m.group(1), m.group(2).strip()))
        return mentions

    async def _check_auth(request: Request) -> bool:
        """Return True if the request is authenticated (or auth is disabled)."""
        if not auth_token:
            return True
        auth_header = request.headers.get("authorization", "")
        return auth_header == f"Bearer {auth_token}"

    async def _auth_guard(request: Request, call_next):
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

    # --- Session store (browser tab → agent key) ---
    # Each browser tab gets a unique session ID; we map it to a pool key.
    _sessions: Dict[str, str] = {}  # session_id -> pool key
    _session_counter = 0

    # --- Custom agents store ---
    # name -> {name, system_prompt, temperature, max_iterations, ...}
    _custom_agents: Dict[str, Dict[str, Any]] = {}
    _custom_agents_loaded = False

    def _load_custom_agents():
        """Load custom agents from ~/.phoenix/agents/*.yaml and config.yaml custom_agents section."""
        nonlocal _custom_agents_loaded, _custom_agents
        if _custom_agents_loaded:
            return
        _custom_agents_loaded = True

        import yaml as _yaml
        from pathlib import Path as _Path
        import os as _os

        agents_map: Dict[str, Dict[str, Any]] = {}

        # 1. Load from ~/.phoenix/agents/*.yaml
        phoenix_home = _Path(_os.environ.get("PHOENIX_HOME", _Path.home() / ".phoenix"))
        agents_dir = phoenix_home / "agents"
        if agents_dir.is_dir():
            for child in sorted(agents_dir.iterdir()):
                if not child.is_file() or child.suffix not in (".yaml", ".yml"):
                    continue
                if child.name.startswith(".") or child.name.startswith("_"):
                    continue
                try:
                    data = _yaml.safe_load(child.read_text(encoding="utf-8")) or {}
                except Exception as exc:
                    logger.warning("Failed to parse agent file %s: %s", child, exc)
                    continue
                name = data.get("name", child.stem)
                if not data.get("system_prompt"):
                    continue
                agents_map[name] = {
                    "name": name,
                    "system_prompt": data.get("system_prompt", ""),
                    "temperature": data.get("temperature"),
                    "max_iterations": data.get("max_iterations"),
                    "model": data.get("model"),
                    "description": data.get("description", ""),
                    "source": str(child),
                }

        # 2. Load from config.yaml custom_agents section
        if hasattr(cfg, "_file_config"):
            ca_section = cfg._file_config.get("custom_agents", {})
            if isinstance(ca_section, dict):
                for name, data in ca_section.items():
                    if not isinstance(data, dict):
                        continue
                    if not data.get("system_prompt"):
                        continue
                    agents_map[name] = {
                        "name": name,
                        "system_prompt": data.get("system_prompt", ""),
                        "temperature": data.get("temperature"),
                        "max_iterations": data.get("max_iterations"),
                        "model": data.get("model"),
                        "description": data.get("description", ""),
                        "source": "config.yaml",
                    }

        _custom_agents = agents_map

    def _save_custom_agent_to_file(name: str, agent_data: Dict[str, Any]) -> bool:
        """Persist a custom agent to ~/.phoenix/agents/{name}.yaml."""
        import yaml as _yaml
        from pathlib import Path as _Path
        import os as _os

        try:
            phoenix_home = _Path(_os.environ.get("PHOENIX_HOME", _Path.home() / ".phoenix"))
            agents_dir = phoenix_home / "agents"
            agents_dir.mkdir(parents=True, exist_ok=True)

            file_data = {
                "name": agent_data["name"],
                "system_prompt": agent_data["system_prompt"],
                "description": agent_data.get("description", ""),
            }
            if agent_data.get("temperature") is not None:
                file_data["temperature"] = agent_data["temperature"]
            if agent_data.get("max_iterations") is not None:
                file_data["max_iterations"] = agent_data["max_iterations"]
            if agent_data.get("model"):
                file_data["model"] = agent_data["model"]

            file_path = agents_dir / f"{name}.yaml"
            file_path.write_text(
                _yaml.safe_dump(file_data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            logger.exception("Failed to save agent %s: %s", name, exc)
            return False

    def _delete_custom_agent_file(name: str) -> bool:
        """Delete a custom agent YAML file."""
        from pathlib import Path as _Path
        import os as _os

        try:
            phoenix_home = _Path(_os.environ.get("PHOENIX_HOME", _Path.home() / ".phoenix"))
            file_path = phoenix_home / "agents" / f"{name}.yaml"
            if file_path.exists():
                file_path.unlink()
            return True
        except Exception as exc:
            logger.exception("Failed to delete agent %s: %s", name, exc)
            return False

    def _new_session_id() -> str:
        nonlocal _session_counter
        _session_counter += 1
        return f"web-{int(time.time())}-{_session_counter}"

    def _get_pool_key(session_id: str) -> str:
        return _sessions.get(session_id, session_id)

    # --- Routes ---

    def _apply_custom_agent(agent, agent_name: str):
        """Apply a custom agent's config overrides to the given agent instance.
        Returns the custom agent data dict, or None if not found."""
        _load_custom_agents()
        ca = _custom_agents.get(agent_name)
        if not ca:
            return None
        if ca.get("system_prompt"):
            agent._system_prompt = ca["system_prompt"]
        if ca.get("temperature") is not None:
            agent.config.agent.temperature = ca["temperature"]
        if ca.get("max_iterations") is not None:
            agent.max_iterations = ca["max_iterations"]
        return ca

    def _delegate_to_custom_agent(role_name: str, task: str, context: str = ""):
        """Create a temporary agent from custom agent config and run a task.
        Returns a dict with {success, content, error, duration_ms, iterations}
        or None if the custom agent is not found."""
        _load_custom_agents()
        ca = _custom_agents.get(role_name)
        if not ca:
            return None

        from phoenix_agent.core.agents.orchestrator import DelegationResult
        from phoenix_agent.core.agent import Agent

        start = time.time()
        try:
            # Build a role-specific system prompt
            delegation_header = (
                f"# Role: {role_name}\n\n"
                f"You are a specialist agent with the role of '{role_name}'. "
                f"You have been delegated a task by a supervisor agent. "
                f"Focus on completing the specific task assigned to you. "
                f"Provide a clear, concise result.\n\n"
            )
            full_prompt = delegation_header + ca.get("system_prompt", "")

            worker = Agent(config=cfg, system_prompt=full_prompt)
            if ca.get("max_iterations") is not None:
                worker.max_iterations = ca["max_iterations"]
            if ca.get("temperature") is not None:
                worker.config.agent.temperature = ca["temperature"]
            if pool.memory:
                worker.memory = pool.memory

            full_task = task
            if context:
                full_task = f"[Context from supervisor]: {context}\n\n[Task]: {task}"

            response = worker.run(full_task, stream=False)
            duration = int((time.time() - start) * 1000)

            return DelegationResult(
                success=True,
                content=response,
                role=role_name,
                duration_ms=duration,
                iterations=worker.iteration_count,
            )
        except Exception as exc:
            duration = int((time.time() - start) * 1000)
            logger.exception("Custom agent delegation failed for %s", role_name)
            return DelegationResult(
                success=False,
                content="",
                role=role_name,
                duration_ms=duration,
                error=str(exc),
            )

    async def _serve_index(request: Request):
        """Serve the SPA HTML page."""
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            return HTMLResponse(
                "<h1>Phoenix Agent Web UI</h1>"
                "<p>Static files not found. "
                "Run <code>phoenix build-web</code> or install from source.</p>",
                status_code=500,
            )
        return FileResponse(index)

    async def _api_chat(request: Request):
        """POST /api/chat — send a message, get a full JSON response."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        message = body.get("message", "").strip()
        session_id = body.get("session_id", "")
        attachments = body.get("attachments", [])
        agent_name = body.get("agent_name", "")

        if not message and not attachments:
            return JSONResponse({"error": "message or attachments required"}, status_code=400)

        pool_key = _get_pool_key(session_id) or _new_session_id()
        if session_id and session_id not in _sessions:
            _sessions[session_id] = pool_key

        agent = pool.get_agent("web", pool_key)

        # Apply custom agent config if specified
        if agent_name:
            _apply_custom_agent(agent, agent_name)

        if attachments:
            file_lines = []
            for att in attachments:
                file_lines.append(f"[File: {att.get('filename', 'unknown')} "
                                  f"({att.get('size', 0)} bytes) "
                                  f"at {att.get('file_path', '')}]")
            attachment_context = "\n".join(file_lines)
            message = (message + "\n\n" + attachment_context) if message else attachment_context

        try:
            loop = asyncio.get_event_loop()
            response_text = await loop.run_in_executor(
                None, lambda: agent.run(message),
            )
            return JSONResponse({
                "response": response_text or "",
                "session_id": session_id or pool_key,
            })
        except Exception as exc:
            logger.exception("[web] Error in /api/chat")
            return JSONResponse(
                {"error": "Internal error. Please try again."},
                status_code=500,
            )

    async def _api_chat_stream(request: Request):
        """POST /api/chat/stream — SSE streaming endpoint."""
        if not await _check_auth(request):
            return PlainTextResponse("Unauthorized", status_code=401)

        try:
            body = await request.json()
        except Exception:
            return PlainTextResponse("Invalid JSON", status_code=400)

        message = body.get("message", "").strip()
        session_id = body.get("session_id", "")
        attachments = body.get("attachments", [])  # [{filename, file_path, file_type, size}]
        agent_name = body.get("agent_name", "")

        if not message and not attachments:
            return PlainTextResponse("message or attachments required", status_code=400)

        pool_key = _get_pool_key(session_id) or _new_session_id()
        if session_id and session_id not in _sessions:
            _sessions[session_id] = pool_key

        agent = pool.get_agent("web", pool_key)

        # Apply custom agent config if specified
        active_agent_name = ""
        if agent_name:
            ca = _apply_custom_agent(agent, agent_name)
            if ca:
                active_agent_name = agent_name

        # Inject file info into message so LLM knows about attachments
        if attachments:
            file_lines = []
            for att in attachments:
                file_lines.append(f"[File: {att.get('filename', 'unknown')} "
                                  f"({att.get('size', 0)} bytes) "
                                  f"at {att.get('file_path', '')}]")
            attachment_context = "\n".join(file_lines)
            message = (message + "\n\n" + attachment_context) if message else attachment_context

        # --- Pre-parse @mentions before entering SSE ---
        mentions = _parse_mentions(message)
        # Strip @mention prefixes from the message sent to the main agent
        # but keep the task description
        clean_message = _MENTION_RE.sub(r'\2', message).strip()
        if not clean_message:
            clean_message = message  # fallback: keep original if all was @mentions

        async def _event_stream():
            """Generate SSE events from the agent response."""
            try:
                from phoenix_agent.core.agents.orchestrator import DelegationResult
                loop = asyncio.get_event_loop()

                # We run the agent in a thread and stream chunks via a queue
                import queue
                import threading

                result_queue: queue.Queue = queue.Queue()
                done_event = threading.Event()

                # Send agent info as first event
                if active_agent_name:
                    result_queue.put(("agent_info", {"agent_name": active_agent_name}))

                # --- Handle @mention delegations BEFORE the main agent ---
                mention_results = []
                if mentions:
                    for role_name, task_text in mentions:
                        # Notify frontend about mention delegation starting
                        result_queue.put(("mention_start", {
                            "role": role_name,
                            "task": task_text[:200],
                        }))

                        # Try orchestrator first (built-in roles)
                        deleg_result = None
                        if pool.orchestrator:
                            deleg_result = pool.orchestrator.delegate(
                                role_name=role_name,
                                task=task_text,
                                context=f"User mentioned @{role_name} in main chat",
                            )
                            # If orchestrator doesn't know the role, try custom agents
                            if not deleg_result.success and "Unknown role" in (deleg_result.error or ""):
                                deleg_result = None

                        # Fallback: try custom agents
                        if deleg_result is None or not deleg_result.success:
                            custom_result = _delegate_to_custom_agent(
                                role_name=role_name,
                                task=task_text,
                                context=f"User mentioned @{role_name} in main chat",
                            )
                            if custom_result is not None:
                                deleg_result = custom_result

                        if deleg_result is None:
                            deleg_result = DelegationResult(
                                success=False, content="", role=role_name,
                                error=f"Unknown agent @{role_name}. Not found in built-in roles or custom agents.",
                                duration_ms=0, iterations=0,
                            )

                        mention_results.append(deleg_result)
                        # Push result as SSE event
                        result_queue.put(("mention_result", {
                            "role": role_name,
                            "success": deleg_result.success,
                            "content": deleg_result.content,
                            "error": deleg_result.error,
                            "duration_ms": deleg_result.duration_ms,
                            "iterations": deleg_result.iterations,
                        }))

                def _on_tool_result(tool_name, tool_args, tool_result):
                    """Callback: push tool results to the SSE queue so the
                    frontend can react (e.g. show confirmation buttons)."""
                    result_queue.put(("tool_result", {
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "success": tool_result.success,
                        "content": tool_result.content,
                        "error": tool_result.error,
                        "metadata": getattr(tool_result, "metadata", None) or {},
                    }))

                def _on_tool_start(tool_name, tool_args):
                    """Callback: notify frontend that a tool is about to run."""
                    # Produce a short summary for the UI
                    summary = _tool_display_summary(tool_name, tool_args)
                    result_queue.put(("tool_start", {
                        "tool_name": tool_name,
                        "summary": summary,
                    }))

                def _on_iteration(iteration, max_iterations):
                    """Callback: notify frontend of iteration progress."""
                    result_queue.put(("iteration", {
                        "iteration": iteration,
                        "max_iterations": max_iterations,
                    }))

                def _run_agent():
                    try:
                        agent.on_tool_call = _on_tool_result
                        agent.on_tool_start = _on_tool_start
                        agent.on_iteration = _on_iteration
                        # Build the message: inject mention results as context
                        effective_msg = clean_message
                        if mention_results:
                            mention_ctx = []
                            for mr in mention_results:
                                if mr.success:
                                    mention_ctx.append(
                                        f"[Result from @{mr.role} ({mr.duration_ms}ms, "
                                        f"{mr.iterations} iterations)]:\n{mr.content}"
                                    )
                                else:
                                    mention_ctx.append(
                                        f"[Error from @{mr.role}]: {mr.error}"
                                    )
                            effective_msg = (
                                "[Below are results from agents you mentioned via @mention. "
                                "Incorporate them into your response as appropriate.]\n\n"
                                + "\n\n---\n\n".join(mention_ctx)
                                + "\n\n---\n\n"
                                + (clean_message or "[User only used @mentions without additional instructions. "
                                  "Summarize the results above.]")
                            )
                        response = agent.run(effective_msg)
                        # Send the final LLM text response
                        result_queue.put(("chunk", response or ""))
                    except Exception as exc:
                        result_queue.put(("error", str(exc)))
                    finally:
                        done_event.set()
                        result_queue.put(("done", ""))

                thread = threading.Thread(target=_run_agent, daemon=True)
                thread.start()

                while not done_event.is_set() or not result_queue.empty():
                    try:
                        msg_type, msg_data = result_queue.get(timeout=0.1)
                        if msg_type == "agent_info":
                            yield f"data: {json.dumps({'agent_info': msg_data})}\n\n"
                        elif msg_type == "mention_start":
                            yield f"data: {json.dumps({'mention_start': msg_data})}\n\n"
                        elif msg_type == "mention_result":
                            yield f"data: {json.dumps({'mention_result': msg_data})}\n\n"
                        elif msg_type == "chunk":
                            # Send as JSON in SSE data field
                            yield f"data: {json.dumps({'content': msg_data})}\n\n"
                        elif msg_type == "tool_start":
                            yield f"data: {json.dumps({'tool_start': msg_data})}\n\n"
                        elif msg_type == "tool_result":
                            # Forward tool result to frontend (e.g. confirmation request)
                            yield f"data: {json.dumps({'tool_result': msg_data})}\n\n"
                        elif msg_type == "iteration":
                            yield f"data: {json.dumps({'iteration': msg_data})}\n\n"
                        elif msg_type == "error":
                            yield f"data: {json.dumps({'error': msg_data})}\n\n"
                        elif msg_type == "done":
                            yield "data: [DONE]\n\n"
                    except queue.Empty:
                        continue

                thread.join(timeout=5)

            except Exception as exc:
                logger.exception("[web] Error in SSE stream")
                yield f"data: {json.dumps({'error': 'Internal error'})}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _api_collaborate(request: Request):
        """POST /api/chat/collaborate — SSE endpoint for two agents discussing a topic.

        Request body:
            agent_a: str  — name of first custom agent (or empty for default)
            agent_b: str  — name of second custom agent (or empty for default)
            topic: str    — discussion topic / initial prompt
            max_rounds: int — max exchange rounds (default 5)
        """
        if not await _check_auth(request):
            return PlainTextResponse("Unauthorized", status_code=401)

        try:
            body = await request.json()
        except Exception:
            return PlainTextResponse("Invalid JSON", status_code=400)

        agent_a_name = body.get("agent_a", "").strip()
        agent_b_name = body.get("agent_b", "").strip()
        topic = body.get("topic", "").strip()
        max_rounds = min(int(body.get("max_rounds", 5)), 20)

        if not topic:
            return PlainTextResponse("topic is required", status_code=400)

        async def _event_stream():
            try:
                import queue as _queue
                import threading

                result_queue = _queue.Queue()
                done_event = threading.Event()

                # Use stop event to allow client cancellation
                stop_event = threading.Event()

                def _run_collaboration():
                    try:
                        _load_custom_agents()

                        # Create two fresh agent instances for the collaboration
                        from phoenix_agent.core.agent import Agent

                        agent_a = Agent(config=cfg)
                        agent_b = Agent(config=cfg)

                        # Apply custom agent configs if specified
                        if agent_a_name:
                            _apply_custom_agent(agent_a, agent_a_name)
                        if agent_b_name:
                            _apply_custom_agent(agent_b, agent_b_name)

                        # Share memory if available
                        if pool.memory:
                            agent_a.memory = pool.memory
                            agent_b.memory = pool.memory

                        # Build per-agent system prompts with collaboration context
                        label_a = agent_a_name or "Agent A"
                        label_b = agent_b_name or "Agent B"

                        collab_context_a = (
                            f"\n\n# Collaboration Mode\n"
                            f"You are '{label_a}' in a discussion with '{label_b}'.\n"
                            f"You will receive messages from {label_b}. Respond naturally as your character.\n"
                            f"Be concise and focused. Do not repeat what {label_b} already said.\n"
                            f"When you have nothing new to add, say 'I agree' or similar.\n"
                        )
                        collab_context_b = (
                            f"\n\n# Collaboration Mode\n"
                            f"You are '{label_b}' in a discussion with '{label_a}'.\n"
                            f"You will receive messages from {label_a}. Respond naturally as your character.\n"
                            f"Be concise and focused. Do not repeat what {label_a} already said.\n"
                            f"When you have nothing new to add, say 'I agree' or similar.\n"
                        )
                        agent_a.system_prompt = agent_a.system_prompt + collab_context_a
                        agent_b.system_prompt = agent_b.system_prompt + collab_context_b

                        # Send initial info
                        result_queue.put(("collab_start", {
                            "agent_a": label_a,
                            "agent_b": label_b,
                            "topic": topic,
                            "max_rounds": max_rounds,
                        }))

                        # Agent A starts with the topic
                        current_speaker = "a"
                        current_prompt = topic
                        exchange_count = 0

                        while exchange_count < max_rounds and not stop_event.is_set():
                            if current_speaker == "a":
                                speaker_agent = agent_a
                                speaker_name = label_a
                                next_speaker = "b"
                            else:
                                speaker_agent = agent_b
                                speaker_name = label_b
                                next_speaker = "a"

                            result_queue.put(("collab_thinking", {
                                "speaker": speaker_name,
                                "round": exchange_count + 1,
                            }))

                            try:
                                response = speaker_agent.run(current_prompt)
                            except Exception as exc:
                                result_queue.put(("collab_error", {
                                    "speaker": speaker_name,
                                    "error": str(exc),
                                }))
                                break

                            # Send the response
                            result_queue.put(("collab_message", {
                                "speaker": speaker_name,
                                "round": exchange_count + 1,
                                "content": response or "(no response)",
                            }))

                            # Check for early termination
                            if not response or response.strip().lower() in (
                                "i agree", "agreed", "同意", "没问题", " concur",
                            ):
                                break

                            # Pass response to the other agent
                            current_prompt = (
                                f"[From {speaker_name}]: {response}\n\n"
                                f"Respond to the above."
                            )
                            current_speaker = next_speaker
                            exchange_count += 1

                        result_queue.put(("collab_end", {"total_rounds": exchange_count}))

                    except Exception as exc:
                        logger.exception("[web] Error in collaboration")
                        result_queue.put(("collab_error", {"speaker": "system", "error": str(exc)}))
                    finally:
                        done_event.set()
                        result_queue.put(("done", ""))

                thread = threading.Thread(target=_run_collaboration, daemon=True)
                thread.start()

                while not done_event.is_set() or not result_queue.empty():
                    try:
                        msg_type, msg_data = result_queue.get(timeout=0.1)
                        if msg_type == "collab_start":
                            yield f"data: {json.dumps({'collab_start': msg_data})}\n\n"
                        elif msg_type == "collab_thinking":
                            yield f"data: {json.dumps({'collab_thinking': msg_data})}\n\n"
                        elif msg_type == "collab_message":
                            yield f"data: {json.dumps({'collab_message': msg_data})}\n\n"
                        elif msg_type == "collab_error":
                            yield f"data: {json.dumps({'collab_error': msg_data})}\n\n"
                        elif msg_type == "collab_end":
                            yield f"data: {json.dumps({'collab_end': msg_data})}\n\n"
                        elif msg_type == "done":
                            yield "data: [DONE]\n\n"
                    except _queue.Empty:
                        continue

                thread.join(timeout=5)

            except Exception as exc:
                logger.exception("[web] Error in collaborate SSE stream")
                yield f"data: {json.dumps({'error': 'Internal error'})}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _api_session_new(request: Request):
        """POST /api/session/new — create a new session (reset context)."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            body = {}

        old_session_id = body.get("session_id", "")
        new_session_id = _new_session_id()

        if old_session_id and old_session_id in _sessions:
            old_pool_key = _sessions[old_session_id]
            # Reset the old agent's state
            agent = pool.get_agent("web", old_pool_key)
            agent.reset()
            del _sessions[old_session_id]

        _sessions[new_session_id] = new_session_id

        return JSONResponse({
            "session_id": new_session_id,
            "message": "New session created",
        })

    async def _api_history(request: Request):
        """GET /api/history — return current session history."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session_id = request.query_params.get("session_id", "")
        pool_key = _get_pool_key(session_id)

        if not pool_key:
            return JSONResponse({"history": []})

        agent = pool.get_agent("web", pool_key)
        messages = agent.get_history()

        history = []
        for msg in messages:
            history.append({
                "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                "content": msg.content,
            })

        return JSONResponse({"history": history})

    async def _api_sessions(request: Request):
        """GET /api/sessions — list active web sessions."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return JSONResponse({
            "sessions": list(_sessions.keys()),
        })

    # --- File uploads directory ---
    _UPLOAD_DIR = Path(os.getcwd()) / "uploads"
    _UPLOAD_DIR.mkdir(exist_ok=True)

    # Allowed file extensions for upload
    _ALLOWED_EXTENSIONS = {
        ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml",
        ".py", ".js", ".ts", ".html", ".css", ".sh", ".bat", ".ps1",
        ".log", ".sql", ".conf", ".cfg", ".ini", ".env",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".zip", ".tar", ".gz", ".7z", ".rar",
    }
    _MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB

    async def _api_upload(request: Request):
        """POST /api/upload — upload a file. Accepts multipart/form-data.
        Returns JSON with {file_id, filename, file_type, size, file_path}."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            form = await request.form()
        except Exception:
            return JSONResponse({"error": "Invalid form data"}, status_code=400)

        file = form.get("file")
        if not file or not file.filename:
            return JSONResponse({"error": "No file provided"}, status_code=400)

        # Check file size
        content = await file.read()
        if len(content) > _MAX_UPLOAD_SIZE:
            return JSONResponse({"error": f"File too large (max {_MAX_UPLOAD_SIZE // (1024*1024)}MB)"}, status_code=400)

        # Check extension
        ext = Path(file.filename).suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            return JSONResponse({"error": f"File type '{ext}' not allowed"}, status_code=400)

        # Generate unique filename to avoid collisions
        file_id = uuid.uuid4().hex[:12]
        safe_name = Path(file.filename).stem[:80]  # truncate long names
        stored_name = f"{file_id}_{safe_name}{ext}"
        file_path = _UPLOAD_DIR / stored_name

        # Write file
        try:
            with open(file_path, "wb") as f:
                f.write(content)
        except Exception as exc:
            logger.exception("[web] Failed to save upload")
            return JSONResponse({"error": "Failed to save file"}, status_code=500)

        # Determine type
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
        file_type = "image" if ext in image_exts else "file"

        return JSONResponse({
            "file_id": file_id,
            "filename": file.filename,
            "file_type": file_type,
            "size": len(content),
            "file_path": str(file_path),
            "download_url": f"/api/download?file={stored_name}",
        })

    async def _api_download(request: Request):
        """GET /api/download?file=xxx — download a file from uploads dir."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        filename = request.query_params.get("file", "")
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return JSONResponse({"error": "Invalid filename"}, status_code=400)

        file_path = _UPLOAD_DIR / filename
        if not file_path.exists() or not file_path.is_file():
            return JSONResponse({"error": "File not found"}, status_code=404)

        return FileResponse(file_path, filename=filename)

    async def _api_list_files(request: Request):
        """GET /api/files — list uploaded files."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        files = []
        for p in sorted(_UPLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file() and not p.name.startswith("."):
                ext = p.suffix.lower()
                image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
                files.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "type": "image" if ext in image_exts else "file",
                    "download_url": f"/api/download?file={p.name}",
                    "modified": p.stat().st_mtime,
                })
        return JSONResponse({"files": files[:50]})

    # --- Config management API ---

    async def _api_get_config(request: Request):
        """GET /api/config — return full configuration (secrets masked)."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            cfg_data = cfg.to_dict()

            # Mask sensitive fields
            _mask_keys = ["api_key", "client_secret", "app_secret", "corp_secret",
                          "bot_token", "access_token", "secret", "token",
                          "encoding_aes_key", "webhook_secret", "webhook_url"]
            def _mask(obj, parent_key=""):
                if isinstance(obj, dict):
                    out = {}
                    for k, v in obj.items():
                        if k in _mask_keys and v and isinstance(v, str) and len(v) > 6:
                            out[k] = v[:3] + "***" + v[-3:]
                        else:
                            out[k] = _mask(v, k)
                    return out
                elif isinstance(obj, list):
                    return [_mask(item) for item in obj]
                return obj

            masked = _mask(cfg_data)
            # Also add raw _file_config (masked) for full YAML visibility
            raw_masked = _mask(getattr(cfg, "_file_config", {}))
            masked["raw_config"] = raw_masked

            return JSONResponse(masked)
        except Exception as exc:
            logger.exception("[web] Error reading config")
            return JSONResponse({"error": "Failed to read config"}, status_code=500)

    async def _api_update_config(request: Request):
        """PUT /api/config — update a section of config.yaml.

        Body: {"section": "provider|agent|tools|web_search|channels|scheduler|skills",
               "data": {...}}
        Only the specified section is overwritten. Other sections remain unchanged.
        """
        nonlocal cfg
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
            section = body.get("section", "").strip()
            data = body.get("data", {})

            if not section or not isinstance(data, dict):
                return JSONResponse({"error": "section (string) and data (object) required"},
                                    status_code=400)

            allowed_sections = {"provider", "agent", "tools", "web_search",
                                "channels", "scheduler", "skills", "storage"}
            if section not in allowed_sections:
                return JSONResponse(
                    {"error": f"Invalid section. Allowed: {', '.join(sorted(allowed_sections))}"},
                    status_code=400)

            config_path = getattr(cfg, "config_file", None)
            if not config_path:
                return JSONResponse({"error": "Config file path not configured"}, status_code=400)

            import yaml

            # Read existing file or start with empty dict
            file_data = {}
            if config_path.exists():
                try:
                    file_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError as e:
                    return JSONResponse({"error": f"Config file is not valid YAML: {e}"}, status_code=400)

            # Ensure parent directory exists
            config_path.parent.mkdir(parents=True, exist_ok=True)

            # Merge the new section
            file_data[section] = data
            config_path.write_text(
                yaml.safe_dump(file_data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            # Invalidate global config cache so next read picks up changes
            from phoenix_agent.core.config import reset_config, get_config
            reset_config()

            # Hot-reload: re-read the config file and replace the local cfg reference
            new_cfg = get_config(str(config_path))
            cfg = new_cfg

            return JSONResponse({"message": f"Section '{section}' updated and hot-reloaded.",
                                 "section": section})
        except Exception as exc:
            logger.exception("[web] Error updating config")
            return JSONResponse({"error": f"Failed to update config: {exc}"}, status_code=500)

    # --- Skills management API ---

    async def _api_list_skills(request: Request):
        """GET /api/skills — list all discovered skills."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            from phoenix_agent.skills.registry import SkillRegistry
            reg = SkillRegistry()
            reg.discover()
            skills = []
            for name, skill in reg._skills.items():
                m = skill.manifest
                skills.append({
                    "name": m.name,
                    "version": m.version,
                    "description": m.description,
                    "triggers": m.triggers,
                    "tools": m.tools,
                    "source_path": str(m.source_path),
                })
            skills.sort(key=lambda s: s["name"])
            return JSONResponse({"skills": skills, "count": len(skills)})
        except Exception as exc:
            logger.exception("[web] Error listing skills")
            return JSONResponse({"error": str(exc)}, status_code=500)

    # --- Agent roles API ---

    async def _api_list_roles(request: Request):
        """GET /api/roles — list all discovered agent roles for multi-agent collaboration."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            from phoenix_agent.core.agents.roles import discover_all_roles

            # Discover roles from YAML files + inline config
            config_data = {}
            if hasattr(cfg, "_file_config"):
                config_data = cfg._file_config
            all_roles = discover_all_roles(config_data)

            roles = []
            for role in all_roles:
                roles.append({
                    "name": role.name,
                    "description": role.description or "",
                    "tools": role.config.tools or [],
                    "model": role.config.model or "",
                    "max_iterations": role.config.max_iterations,
                    "temperature": role.config.temperature,
                })
            roles.sort(key=lambda r: r["name"])
            return JSONResponse({"roles": roles, "count": len(roles)})
        except Exception as exc:
            logger.exception("[web] Error listing roles")
            return JSONResponse({"error": str(exc)}, status_code=500)

    # --- Custom agents API ---

    async def _api_list_agents(request: Request):
        """GET /api/agents — list all custom agents."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        _load_custom_agents()
        agents = list(_custom_agents.values())
        agents.sort(key=lambda a: a["name"])
        return JSONResponse({"agents": agents, "count": len(agents)})

    async def _api_create_agent(request: Request):
        """POST /api/agents — create or update a custom agent."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        name = body.get("name", "").strip()
        system_prompt = body.get("system_prompt", "").strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if not system_prompt:
            return JSONResponse({"error": "system_prompt is required"}, status_code=400)

        # Validate name: alphanumeric, dash, underscore
        import re
        if not re.match(r'^[a-zA-Z0-9_-]+$', name):
            return JSONResponse({"error": "name must be alphanumeric (letters, digits, -, _)"}, status_code=400)

        agent_data = {
            "name": name,
            "system_prompt": system_prompt,
            "description": body.get("description", ""),
            "temperature": body.get("temperature"),
            "max_iterations": body.get("max_iterations"),
            "model": body.get("model"),
        }

        # Persist to file
        if not _save_custom_agent_to_file(name, agent_data):
            return JSONResponse({"error": "Failed to save agent file"}, status_code=500)

        # Update in-memory cache
        agent_data["source"] = "~/.phoenix/agents/"
        _custom_agents[name] = agent_data

        return JSONResponse({"message": f"Agent '{name}' saved", "agent": agent_data})

    async def _api_delete_agent(request: Request):
        """DELETE /api/agents/{name} — delete a custom agent."""
        if not await _check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        name = request.path_params.get("name", "").strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)

        _load_custom_agents()
        if name not in _custom_agents:
            return JSONResponse({"error": f"Agent '{name}' not found"}, status_code=404)

        if not _delete_custom_agent_file(name):
            return JSONResponse({"error": "Failed to delete agent file"}, status_code=500)

        del _custom_agents[name]
        return JSONResponse({"message": f"Agent '{name}' deleted"})

    async def _api_delegate(request: Request):
        """POST /api/chat/delegate — SSE endpoint for delegating a task from the main chat.

        Request body:
            role: str       — agent role name (required)
            task: str       — task description (required)
            context: str    — optional background context
            session_id: str — optional session ID for tracking
        """
        if not await _check_auth(request):
            return PlainTextResponse("Unauthorized", status_code=401)

        try:
            body = await request.json()
        except Exception:
            return PlainTextResponse("Invalid JSON", status_code=400)

        role_name = body.get("role", "").strip()
        task = body.get("task", "").strip()
        context = body.get("context", "").strip()

        if not role_name:
            return PlainTextResponse("role is required", status_code=400)
        if not task:
            return PlainTextResponse("task is required", status_code=400)

        async def _event_stream():
            try:
                import queue as _queue
                import threading

                result_queue = _queue.Queue()
                done_event = threading.Event()

                def _run_delegation():
                    try:
                        from phoenix_agent.tools.builtin import get_orchestrator

                        # Try orchestrator first (built-in roles)
                        deleg_result = None
                        orchestrator = get_orchestrator()
                        if orchestrator:
                            role = orchestrator.get_role(role_name)
                            if role:
                                result_queue.put(("delegate_start", {
                                    "role": role_name,
                                    "task": task,
                                    "description": role.description or "",
                                }))
                                deleg_result = orchestrator.delegate(
                                    role_name=role_name,
                                    task=task,
                                    context=context,
                                )

                        # Fallback: try custom agents
                        if deleg_result is None:
                            _load_custom_agents()
                            ca = _custom_agents.get(role_name)
                            if ca:
                                result_queue.put(("delegate_start", {
                                    "role": role_name,
                                    "task": task,
                                    "description": ca.get("description", ""),
                                }))
                                deleg_result = _delegate_to_custom_agent(
                                    role_name=role_name,
                                    task=task,
                                    context=context,
                                )

                        if deleg_result is None:
                            result_queue.put(("error", f"Unknown agent '{role_name}'. Not found in built-in roles or custom agents."))
                            return

                        if deleg_result.success:
                            result_queue.put(("delegate_result", {
                                "role": deleg_result.role,
                                "content": deleg_result.content,
                                "duration_ms": deleg_result.duration_ms,
                                "iterations": deleg_result.iterations,
                            }))
                        else:
                            result_queue.put(("delegate_error", {
                                "role": deleg_result.role,
                                "error": deleg_result.error,
                                "duration_ms": deleg_result.duration_ms,
                            }))

                    except Exception as exc:
                        logger.exception("[web] Error in delegation")
                        result_queue.put(("error", str(exc)))
                    finally:
                        done_event.set()
                        result_queue.put(("done", ""))

                thread = threading.Thread(target=_run_delegation, daemon=True)
                thread.start()

                while not done_event.is_set() or not result_queue.empty():
                    try:
                        msg_type, msg_data = result_queue.get(timeout=0.1)
                        if msg_type == "delegate_start":
                            yield f"data: {json.dumps({'delegate_start': msg_data})}\n\n"
                        elif msg_type == "delegate_result":
                            yield f"data: {json.dumps({'delegate_result': msg_data})}\n\n"
                        elif msg_type == "delegate_error":
                            yield f"data: {json.dumps({'delegate_error': msg_data})}\n\n"
                        elif msg_type == "error":
                            yield f"data: {json.dumps({'error': msg_data})}\n\n"
                        elif msg_type == "done":
                            yield "data: [DONE]\n\n"
                    except _queue.Empty:
                        continue

                thread.join(timeout=5)

            except Exception as exc:
                logger.exception("[web] Error in delegate SSE stream")
                yield f"data: {json.dumps({'error': 'Internal error'})}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # --- Routes ---
    routes = [
        Route("/", endpoint=_serve_index, methods=["GET"]),
        Route("/api/chat", endpoint=_api_chat, methods=["POST"]),
        Route("/api/chat/stream", endpoint=_api_chat_stream, methods=["POST"]),
        Route("/api/chat/delegate", endpoint=_api_delegate, methods=["POST"]),
        Route("/api/chat/collaborate", endpoint=_api_collaborate, methods=["POST"]),
        Route("/api/session/new", endpoint=_api_session_new, methods=["POST"]),
        Route("/api/history", endpoint=_api_history, methods=["GET"]),
        Route("/api/sessions", endpoint=_api_sessions, methods=["GET"]),
        Route("/api/upload", endpoint=_api_upload, methods=["POST"]),
        Route("/api/download", endpoint=_api_download, methods=["GET"]),
        Route("/api/files", endpoint=_api_list_files, methods=["GET"]),
        Route("/api/config", endpoint=_api_get_config, methods=["GET"]),
        Route("/api/config", endpoint=_api_update_config, methods=["PUT"]),
        Route("/api/skills", endpoint=_api_list_skills, methods=["GET"]),
        Route("/api/roles", endpoint=_api_list_roles, methods=["GET"]),
        Route("/api/agents", endpoint=_api_list_agents, methods=["GET"]),
        Route("/api/agents", endpoint=_api_create_agent, methods=["POST"]),
        Route("/api/agents/{name}", endpoint=_api_delete_agent, methods=["DELETE"]),
    ]

    return routes
