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
import time
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
)
from starlette.routing import Mount, Route

logger = logging.getLogger(__name__)

# Path to the static HTML file (bundled with the package)
_STATIC_DIR = Path(__file__).parent / "static"


def build_web_app(pool, config=None) -> Starlette:
    """
    Build the Starlette sub-app for the Web UI.

    Args:
        pool:   The AgentPool instance (shared with the main server).
        config: Optional Config — read ``web.token`` for auth.

    Returns:
        A Starlette application.
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

    def _new_session_id() -> str:
        nonlocal _session_counter
        _session_counter += 1
        return f"web-{int(time.time())}-{_session_counter}"

    def _get_pool_key(session_id: str) -> str:
        return _sessions.get(session_id, session_id)

    # --- Routes ---

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

        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        pool_key = _get_pool_key(session_id) or _new_session_id()
        if session_id and session_id not in _sessions:
            _sessions[session_id] = pool_key

        agent = pool.get_agent("web", pool_key)

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

        if not message:
            return PlainTextResponse("message is required", status_code=400)

        pool_key = _get_pool_key(session_id) or _new_session_id()
        if session_id and session_id not in _sessions:
            _sessions[session_id] = pool_key

        agent = pool.get_agent("web", pool_key)

        async def _event_stream():
            """Generate SSE events from the agent response."""
            try:
                loop = asyncio.get_event_loop()

                # We run the agent in a thread and stream chunks via a queue
                import queue
                import threading

                result_queue: queue.Queue = queue.Queue()
                done_event = threading.Event()

                def _run_agent():
                    try:
                        response = agent.run(message)
                        # Simulate streaming by sending the full response as one chunk
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
                        if msg_type == "chunk":
                            # Send as JSON in SSE data field
                            yield f"data: {json.dumps({'content': msg_data})}\n\n"
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

        return Response(
            content=_event_stream(),
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

    routes = [
        Route("/", endpoint=_serve_index, methods=["GET"]),
        Route("/api/chat", endpoint=_api_chat, methods=["POST"]),
        Route("/api/chat/stream", endpoint=_api_chat_stream, methods=["POST"]),
        Route("/api/session/new", endpoint=_api_session_new, methods=["POST"]),
        Route("/api/history", endpoint=_api_history, methods=["GET"]),
        Route("/api/sessions", endpoint=_api_sessions, methods=["GET"]),
    ]

    return Starlette(routes=routes)
