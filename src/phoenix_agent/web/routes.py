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
        attachments = body.get("attachments", [])

        if not message and not attachments:
            return JSONResponse({"error": "message or attachments required"}, status_code=400)

        pool_key = _get_pool_key(session_id) or _new_session_id()
        if session_id and session_id not in _sessions:
            _sessions[session_id] = pool_key

        agent = pool.get_agent("web", pool_key)

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

        if not message and not attachments:
            return PlainTextResponse("message or attachments required", status_code=400)

        pool_key = _get_pool_key(session_id) or _new_session_id()
        if session_id and session_id not in _sessions:
            _sessions[session_id] = pool_key

        agent = pool.get_agent("web", pool_key)

        # Inject file info into message so LLM knows about attachments
        if attachments:
            file_lines = []
            for att in attachments:
                file_lines.append(f"[File: {att.get('filename', 'unknown')} "
                                  f"({att.get('size', 0)} bytes) "
                                  f"at {att.get('file_path', '')}]")
            attachment_context = "\n".join(file_lines)
            message = (message + "\n\n" + attachment_context) if message else attachment_context

        async def _event_stream():
            """Generate SSE events from the agent response."""
            try:
                loop = asyncio.get_event_loop()

                # We run the agent in a thread and stream chunks via a queue
                import queue
                import threading

                result_queue: queue.Queue = queue.Queue()
                done_event = threading.Event()

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

                def _run_agent():
                    try:
                        agent.on_tool_call = _on_tool_result
                        response = agent.run(message)
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
                        if msg_type == "chunk":
                            # Send as JSON in SSE data field
                            yield f"data: {json.dumps({'content': msg_data})}\n\n"
                        elif msg_type == "tool_result":
                            # Forward tool result to frontend (e.g. confirmation request)
                            yield f"data: {json.dumps({'tool_result': msg_data})}\n\n"
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
            if not config_path or not config_path.exists():
                return JSONResponse({"error": "Config file not found"}, status_code=500)

            import yaml
            file_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            file_data[section] = data
            config_path.write_text(
                yaml.safe_dump(file_data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            # Invalidate global config cache so next read picks up changes
            from phoenix_agent.core.config import reset_config
            reset_config()

            return JSONResponse({"message": f"Section '{section}' updated. Restart or reload for full effect.",
                                 "section": section})
        except Exception as exc:
            logger.exception("[web] Error updating config")
            return JSONResponse({"error": "Failed to update config"}, status_code=500)

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

    # --- Routes ---
    routes = [
        Route("/", endpoint=_serve_index, methods=["GET"]),
        Route("/api/chat", endpoint=_api_chat, methods=["POST"]),
        Route("/api/chat/stream", endpoint=_api_chat_stream, methods=["POST"]),
        Route("/api/session/new", endpoint=_api_session_new, methods=["POST"]),
        Route("/api/history", endpoint=_api_history, methods=["GET"]),
        Route("/api/sessions", endpoint=_api_sessions, methods=["GET"]),
        Route("/api/upload", endpoint=_api_upload, methods=["POST"]),
        Route("/api/download", endpoint=_api_download, methods=["GET"]),
        Route("/api/files", endpoint=_api_list_files, methods=["GET"]),
        Route("/api/config", endpoint=_api_get_config, methods=["GET"]),
        Route("/api/config", endpoint=_api_update_config, methods=["PUT"]),
        Route("/api/skills", endpoint=_api_list_skills, methods=["GET"]),
    ]

    return routes
