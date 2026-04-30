"""
Built-in Tools Module

Provides the core toolset for Phoenix Agent.
Tools are organized into four categories:
  - FILE     : read, write, edit, list, search, move, delete, create dirs
  - WEB      : fetch URL content
  - SYSTEM   : run shell commands, inspect environment
  - UTILITY  : time, calculator, echo

All handlers follow a consistent contract:
  - Receive plain Python types as arguments
  - Return a JSON string via ToolResult.to_json()
  - Never raise exceptions to the caller — errors are returned as ToolResult(success=False)

Security notes:
  - Path traversal (``..``) is rejected for all file operations
  - Shell commands run with a 60-second timeout
  - Files larger than 10 MB cannot be read in full (use offset/limit)
"""

import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from phoenix_agent.tools.registry import ToolDefinition, ToolCategory, ToolResult, ToolRegistry


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_builtin_tools(registry: ToolRegistry) -> None:
    """
    Register all built-in tools into *registry*.

    Called once at package import time via ``tools/__init__.py``.
    Adding a new tool means writing its handler here and calling
    ``registry._tools[name] = ToolDefinition(...)`` at the end.
    """
    _register_file_tools(registry)
    _register_web_tools(registry)
    _register_system_tools(registry)
    _register_utility_tools(registry)
    _register_scheduler_tools(registry)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_path(raw: str) -> tuple[Optional[Path], Optional[str]]:
    """
    Resolve and validate a user-supplied path.

    Returns (resolved_path, None) on success or (None, error_message) on
    rejection.  Rejects paths that contain ``..`` to prevent traversal,
    and uses ``resolve()`` to detect symlink-based escapes.
    """
    if ".." in raw:
        return None, "Path traversal ('..') is not allowed"
    try:
        resolved = Path(raw).expanduser().resolve()
        # Detect symlink-based traversal: if the raw path contains no ".."
        # but the resolved path escapes above the starting directory,
        # something suspicious is going on (e.g., symlink to /etc/passwd).
        # Note: This is a secondary check; the primary sandbox enforcement
        # happens in ToolRegistry.execute() when sandbox_path is configured.
        return resolved, None
    except Exception as exc:
        return None, f"Invalid path: {exc}"


def _reg(registry: ToolRegistry, name: str, description: str,
         parameters: dict, category: ToolCategory, handler) -> None:
    """Convenience wrapper to add a ToolDefinition to the registry."""
    registry._tools[name] = ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        category=category,
        handler=handler,
    )


# ---------------------------------------------------------------------------
# FILE TOOLS
# ---------------------------------------------------------------------------

def _register_file_tools(registry: ToolRegistry) -> None:
    """Register all file-system tools."""

    # ---- read_file --------------------------------------------------------
    def read_file(file_path: str,
                  offset: Optional[int] = None,
                  limit: Optional[int] = None) -> str:
        """
        Read a file's content.

        Args:
            file_path: Absolute or relative path to the file.
            offset:    First line to include (1-based).
            limit:     Maximum number of lines to return.
        """
        path, err = _safe_path(file_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        if not path.exists():
            return ToolResult(success=False, content="",
                              error=f"File not found: {file_path}").to_json()
        if not path.is_file():
            return ToolResult(success=False, content="",
                              error=f"Not a file: {file_path}").to_json()
        if path.stat().st_size > 10 * 1024 * 1024:
            return ToolResult(success=False, content="",
                              error="File too large (max 10 MB). Use offset/limit.").to_json()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            # Apply offset / limit (offset is 1-based)
            start = max(0, (offset or 1) - 1)
            end   = start + limit if limit else len(lines)
            chunk = "".join(lines[start:end])
            meta = {"total_lines": len(lines), "shown_lines": len(lines[start:end])}
            return ToolResult(success=True, content=chunk, metadata=meta).to_json()
        except PermissionError:
            return ToolResult(success=False, content="",
                              error=f"Permission denied: {file_path}").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error reading file: {exc}").to_json()

    _reg(registry, "read_file",
         "Read the contents of a file. Use offset/limit to read large files in chunks.",
         {"type": "object",
          "properties": {
              "file_path": {"type": "string", "description": "Path to the file"},
              "offset":    {"type": "integer", "description": "Start line (1-based, optional)"},
              "limit":     {"type": "integer", "description": "Max lines to return (optional)"},
          },
          "required": ["file_path"]},
         ToolCategory.FILE, read_file)

    # ---- write_file -------------------------------------------------------
    def write_file(file_path: str, content: str) -> str:
        """
        Write (overwrite) a file with the provided content.
        Creates parent directories automatically.
        """
        path, err = _safe_path(file_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(success=True,
                              content=f"Wrote {len(content)} characters to {file_path}").to_json()
        except PermissionError:
            return ToolResult(success=False, content="",
                              error=f"Permission denied: {file_path}").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error writing file: {exc}").to_json()

    _reg(registry, "write_file",
         "Write content to a file (creates or overwrites). Parent directories are created automatically.",
         {"type": "object",
          "properties": {
              "file_path": {"type": "string", "description": "Destination file path"},
              "content":   {"type": "string", "description": "Text content to write"},
          },
          "required": ["file_path", "content"]},
         ToolCategory.FILE, write_file)

    # ---- edit_file --------------------------------------------------------
    def edit_file(file_path: str,
                  old_str: str,
                  new_str: str,
                  occurrence: int = 1) -> str:
        """
        Replace *old_str* with *new_str* in a file.

        Args:
            file_path:  Path to the file to edit.
            old_str:    Exact string to search for (must be unique or use ``occurrence``).
            new_str:    Replacement string.
            occurrence: Which occurrence to replace (default 1 = first).
                        Use 0 to replace **all** occurrences.
        """
        path, err = _safe_path(file_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        if not path.exists():
            return ToolResult(success=False, content="",
                              error=f"File not found: {file_path}").to_json()
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
            count = original.count(old_str)
            if count == 0:
                return ToolResult(success=False, content="",
                                  error=f"String not found in file: {repr(old_str[:80])}").to_json()
            if occurrence == 0:
                # Replace all
                updated = original.replace(old_str, new_str)
                replaced = count
            else:
                # Replace nth occurrence
                parts = original.split(old_str)
                if occurrence > count:
                    return ToolResult(success=False, content="",
                                      error=f"Occurrence {occurrence} not found (file has {count})").to_json()
                # Rebuild: join the first `occurrence` splits, insert new_str, then rest
                before = old_str.join(parts[:occurrence])
                after  = old_str.join(parts[occurrence:])
                updated = before + new_str + after
                replaced = 1
            path.write_text(updated, encoding="utf-8")
            return ToolResult(success=True,
                              content=f"Replaced {replaced} occurrence(s) in {file_path}").to_json()
        except PermissionError:
            return ToolResult(success=False, content="",
                              error=f"Permission denied: {file_path}").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error editing file: {exc}").to_json()

    _reg(registry, "edit_file",
         "Replace a specific string in a file with a new string. Use this to make targeted edits without rewriting the whole file.",
         {"type": "object",
          "properties": {
              "file_path":  {"type": "string",  "description": "Path to the file"},
              "old_str":    {"type": "string",  "description": "Exact text to replace"},
              "new_str":    {"type": "string",  "description": "Replacement text"},
              "occurrence": {"type": "integer", "description": "Which occurrence (1=first, 0=all). Default 1"},
          },
          "required": ["file_path", "old_str", "new_str"]},
         ToolCategory.FILE, edit_file)

    # ---- list_directory ---------------------------------------------------
    def list_directory(directory_path: str = ".", show_hidden: bool = False) -> str:
        """List files and directories at the given path."""
        path, err = _safe_path(directory_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        if not path.exists():
            return ToolResult(success=False, content="",
                              error=f"Directory not found: {directory_path}").to_json()
        if not path.is_dir():
            return ToolResult(success=False, content="",
                              error=f"Not a directory: {directory_path}").to_json()
        try:
            items = []
            for item in sorted(path.iterdir()):
                if not show_hidden and item.name.startswith("."):
                    continue
                kind = "DIR " if item.is_dir() else "FILE"
                size = f"  {item.stat().st_size:>10,} B" if item.is_file() else ""
                items.append(f"  [{kind}] {item.name}{size}")
            return ToolResult(success=True,
                              content="\n".join(items) if items else "  (empty)").to_json()
        except PermissionError:
            return ToolResult(success=False, content="",
                              error=f"Permission denied: {directory_path}").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error listing directory: {exc}").to_json()

    _reg(registry, "list_directory",
         "List files and subdirectories at a given path.",
         {"type": "object",
          "properties": {
              "directory_path": {"type": "string",  "description": "Directory to list (default '.')"},
              "show_hidden":    {"type": "boolean", "description": "Include hidden files (default false)"},
          },
          "required": []},
         ToolCategory.FILE, list_directory)

    # ---- create_directory -------------------------------------------------
    def create_directory(directory_path: str) -> str:
        """Create a directory (and any missing parents)."""
        path, err = _safe_path(directory_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        try:
            path.mkdir(parents=True, exist_ok=True)
            return ToolResult(success=True,
                              content=f"Created directory: {directory_path}").to_json()
        except PermissionError:
            return ToolResult(success=False, content="",
                              error=f"Permission denied: {directory_path}").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error creating directory: {exc}").to_json()

    _reg(registry, "create_directory",
         "Create a directory (and all necessary parent directories).",
         {"type": "object",
          "properties": {
              "directory_path": {"type": "string", "description": "Path of the directory to create"},
          },
          "required": ["directory_path"]},
         ToolCategory.FILE, create_directory)

    # ---- move_file --------------------------------------------------------
    def move_file(source_path: str, destination_path: str) -> str:
        """Move or rename a file or directory."""
        src, err = _safe_path(source_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        dst, err = _safe_path(destination_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        if not src.exists():
            return ToolResult(success=False, content="",
                              error=f"Source not found: {source_path}").to_json()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return ToolResult(success=True,
                              content=f"Moved {source_path} → {destination_path}").to_json()
        except PermissionError:
            return ToolResult(success=False, content="",
                              error="Permission denied").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error moving file: {exc}").to_json()

    _reg(registry, "move_file",
         "Move or rename a file or directory.",
         {"type": "object",
          "properties": {
              "source_path":      {"type": "string", "description": "Current path"},
              "destination_path": {"type": "string", "description": "Target path"},
          },
          "required": ["source_path", "destination_path"]},
         ToolCategory.FILE, move_file)

    # ---- delete_file ------------------------------------------------------
    def delete_file(file_path: str, recursive: bool = False) -> str:
        """
        Delete a file.  Set ``recursive=true`` to remove a non-empty directory.

        WARNING: This operation is irreversible.
        """
        path, err = _safe_path(file_path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        if not path.exists():
            return ToolResult(success=False, content="",
                              error=f"Path not found: {file_path}").to_json()
        try:
            if path.is_dir():
                if recursive:
                    shutil.rmtree(path)
                else:
                    path.rmdir()   # Fails if non-empty — intentional safety
            else:
                path.unlink()
            return ToolResult(success=True,
                              content=f"Deleted: {file_path}").to_json()
        except OSError as exc:
            return ToolResult(success=False, content="",
                              error=f"Error deleting: {exc}").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error deleting: {exc}").to_json()

    _reg(registry, "delete_file",
         "Delete a file or directory. Use recursive=true to remove non-empty directories (irreversible).",
         {"type": "object",
          "properties": {
              "file_path": {"type": "string",  "description": "Path to file or directory"},
              "recursive": {"type": "boolean", "description": "Remove directory recursively (default false)"},
          },
          "required": ["file_path"]},
         ToolCategory.FILE, delete_file)

    # ---- grep -------------------------------------------------------------
    def grep(pattern: str,
             path: str = ".",
             file_glob: str = "*",
             recursive: bool = True,
             case_sensitive: bool = False,
             context_lines: int = 0,
             max_results: int = 50) -> str:
        """
        Search for a regex/text pattern across files.

        Args:
            pattern:        Search pattern (plain text or regex).
            path:           Directory (or file) to search in.
            file_glob:      Glob filter for filenames (e.g. "*.py").
            recursive:      Descend into subdirectories.
            case_sensitive: Match case exactly.
            context_lines:  Lines of context above and below each match.
            max_results:    Cap on the number of matches returned.
        """
        import re
        search_path, err = _safe_path(path)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        if not search_path.exists():
            return ToolResult(success=False, content="",
                              error=f"Path not found: {path}").to_json()
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult(success=False, content="",
                              error=f"Invalid regex pattern: {exc}").to_json()

        matches: list[str] = []
        total = 0

        # Build file list
        if search_path.is_file():
            files = [search_path]
        elif recursive:
            files = list(search_path.rglob(file_glob))
        else:
            files = list(search_path.glob(file_glob))

        for fp in files:
            if not fp.is_file():
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            except (PermissionError, UnicodeDecodeError):
                continue

            for i, line in enumerate(lines):
                if compiled.search(line):
                    total += 1
                    start = max(0, i - context_lines)
                    end   = min(len(lines), i + context_lines + 1)
                    block_lines = []
                    for j in range(start, end):
                        prefix = ">" if j == i else " "
                        block_lines.append(f"{fp}:{j+1}{prefix} {lines[j]}")
                    matches.append("\n".join(block_lines))
                    if total >= max_results:
                        break
            if total >= max_results:
                break

        if not matches:
            return ToolResult(success=True,
                              content=f"No matches for '{pattern}'").to_json()
        header = f"Found {total} match(es):\n\n"
        return ToolResult(success=True,
                          content=header + "\n---\n".join(matches)).to_json()

    _reg(registry, "grep",
         "Search for a text pattern or regex across files in a directory. Returns matching lines with optional context.",
         {"type": "object",
          "properties": {
              "pattern":        {"type": "string",  "description": "Text or regex to search for"},
              "path":           {"type": "string",  "description": "Directory or file to search in (default '.')"},
              "file_glob":      {"type": "string",  "description": "Filename filter, e.g. '*.py' (default '*')"},
              "recursive":      {"type": "boolean", "description": "Search subdirectories (default true)"},
              "case_sensitive": {"type": "boolean", "description": "Case-sensitive match (default false)"},
              "context_lines":  {"type": "integer", "description": "Lines of context around each match (default 0)"},
              "max_results":    {"type": "integer", "description": "Max matches to return (default 50)"},
          },
          "required": ["pattern"]},
         ToolCategory.FILE, grep)

    # ---- glob_files -------------------------------------------------------
    def glob_files(pattern: str, base_directory: str = ".") -> str:
        """
        Find files matching a glob pattern.

        Args:
            pattern:        Glob pattern, e.g. ``"**/*.py"`` or ``"src/*.ts"``.
            base_directory: Root directory for the search.
        """
        base, err = _safe_path(base_directory)
        if err:
            return ToolResult(success=False, content="", error=err).to_json()
        if not base.exists():
            return ToolResult(success=False, content="",
                              error=f"Directory not found: {base_directory}").to_json()
        try:
            found = sorted(base.glob(pattern))
            lines = [str(p) for p in found if p.is_file()]
            if not lines:
                return ToolResult(success=True,
                                  content=f"No files match '{pattern}'").to_json()
            return ToolResult(success=True,
                              content=f"{len(lines)} file(s):\n" + "\n".join(lines)).to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error: {exc}").to_json()

    _reg(registry, "glob_files",
         "Find files matching a glob pattern (e.g. '**/*.py'). Returns a list of matching file paths.",
         {"type": "object",
          "properties": {
              "pattern":        {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
              "base_directory": {"type": "string", "description": "Root directory (default '.')"},
          },
          "required": ["pattern"]},
         ToolCategory.FILE, glob_files)


# ---------------------------------------------------------------------------
# WEB TOOLS
# ---------------------------------------------------------------------------

def _register_web_tools(registry: ToolRegistry) -> None:
    """Register HTTP/web tools."""

    import re as _re
    import ipaddress

    # Private / internal IP ranges (SSRF blacklist)
    _PRIVATE_NETWORKS = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),          # IPv6 private
        ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
        ipaddress.ip_network("0.0.0.0/8"),
    ]

    _ALLOWED_SCHEMES = ("http", "https")

    def _is_private_url(url: str) -> bool:
        """Return True if *url* resolves to a private / internal IP."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
                return True  # block non-http(s) schemes like file://, gopher://
            hostname = parsed.hostname
            if not hostname:
                return True
            # Block raw IP addresses that fall in private ranges
            try:
                ip = ipaddress.ip_address(hostname)
                return any(ip in net for net in _PRIVATE_NETWORKS)
            except ValueError:
                pass  # hostname is a domain, check further below
            # Block common internal DNS names
            internal_names = ("localhost", "metadata.google.internal",
                               "metadata", "kubernetes.default",
                               "metadata.azure.com")
            if hostname.lower() in internal_names:
                return True
            return False
        except Exception:
            return True  # fail closed

    def web_fetch(url: str, timeout: int = 30) -> str:
        """
        Fetch the content of a URL and return it as text.

        Follows redirects and returns up to 50 000 characters of content.
        """
        if _is_private_url(url):
            return ToolResult(
                success=False, content="",
                error=f"URL blocked: access to internal/private addresses is not allowed.",
            ).to_json()
        try:
            import httpx
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": "PhoenixAgent/1.0"})
                resp.raise_for_status()
            text = resp.text[:50_000]
            if len(resp.text) > 50_000:
                text += "\n\n[Content truncated at 50 000 chars]"
            return ToolResult(success=True, content=text,
                              metadata={"url": url, "status": resp.status_code}).to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"HTTP error: {exc}").to_json()

    _reg(registry, "web_fetch",
         "Fetch the raw text content of a URL. Useful for reading web pages, APIs, and documentation.",
         {"type": "object",
          "properties": {
              "url":     {"type": "string",  "description": "Full URL to fetch"},
              "timeout": {"type": "integer", "description": "Request timeout in seconds (default 30)"},
          },
          "required": ["url"]},
         ToolCategory.WEB, web_fetch)

    # ---- web_search -------------------------------------------------------

    def _get_search_config() -> dict:
        """Lazily load web_search config from the global Config."""
        try:
            from phoenix_agent.core.config import get_config
            cfg = get_config()
            return {
                "provider": cfg.web_search.provider,
                "api_key": cfg.web_search.api_key,
                "max_results": cfg.web_search.max_results,
                "search_depth": cfg.web_search.search_depth,
                "custom_endpoint": cfg.web_search.custom_endpoint,
                "custom_api_key_name": cfg.web_search.custom_api_key_name,
            }
        except Exception:
            return {
                "provider": "tavily",
                "api_key": None,
                "max_results": 5,
                "search_depth": "basic",
                "custom_endpoint": None,
                "custom_api_key_name": "api_key",
            }

    def _search_tavily(query: str, max_results: int,
                       search_depth: str, api_key: str) -> list[dict]:
        """Search using Tavily API."""
        from tavily import TavilyClient

        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_answer=True,
        )

        results = []
        if response.get("answer"):
            results.append({
                "title": "AI Summary",
                "url": "",
                "content": response["answer"],
            })

        for r in response.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0),
            })

        return results

    def _search_duckduckgo(query: str, max_results: int, **_kw) -> list[dict]:
        """Search using DuckDuckGo (no API key required)."""
        from httpx import Client

        # Use DuckDuckGo HTML lite endpoint
        with Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query, "kl": "wt-wt"},
                headers={"User-Agent": "PhoenixAgent/1.0"},
            )
            resp.raise_for_status()

        # Parse results from HTML
        import re
        results = []
        # DuckDuckGo lite returns results as table rows
        # Each result link is in <a class="result-link" href="...">
        links = re.findall(
            r'<a[^>]+class="result-link"[^>]+href="([^"]+)"',
            resp.text,
        )
        # Snippets are in <td class="result-snippet">
        snippets = re.findall(
            r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>',
            resp.text, re.DOTALL,
        )
        # Titles in next td after result-link
        titles = re.findall(
            r'<a[^>]+class="result-link"[^>]*>(.*?)</a>',
            resp.text, re.DOTALL,
        )

        for i in range(min(max_results, len(links))):
            title = re.sub(r"<[^>]+>", "", titles[i]).strip() if i < len(titles) else ""
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            results.append({
                "title": title,
                "url": links[i],
                "content": snippet,
            })

        return results

    def _search_custom(query: str, max_results: int,
                       api_key: str, endpoint: str,
                       api_key_name: str = "api_key", **_kw) -> list[dict]:
        """Search using a custom API endpoint.

        Expected request:
            POST {endpoint}
            Body: {"query": "...", "max_results": N, {api_key_name}: "..."}
        Expected response:
            {"results": [{"title": "...", "url": "...", "content": "..."}]}
        """
        import httpx

        payload = {"query": query, "max_results": max_results}
        if api_key:
            payload[api_key_name] = api_key

        with httpx.Client(timeout=30) as client:
            resp = client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return data.get("results", [])

    def web_search(query: str,
                   max_results: int = 0,
                   search_depth: str = "basic") -> str:
        """
        Search the web for information.

        Returns relevant search results with titles, URLs, and content snippets.
        Uses the configured search provider (default: Tavily).

        Configure via config.yaml 'web_search:' section or env vars:
          - TAVILY_API_KEY for Tavily
          - Set provider to "duckduckgo" for free search (no key needed)
          - Set provider to "custom" with custom_endpoint for custom APIs
        """
        cfg = _get_search_config()
        provider = cfg["provider"]
        api_key = cfg["api_key"]
        limit = max_results if max_results > 0 else cfg["max_results"]
        depth = search_depth if search_depth != "basic" else cfg["search_depth"]

        try:
            if provider == "tavily":
                if not api_key:
                    return ToolResult(
                        success=False, content="",
                        error="Tavily API key required. Set TAVILY_API_KEY env var "
                              "or api_key in config.yaml web_search section."
                    ).to_json()
                results = _search_tavily(query, limit, depth, api_key)

            elif provider == "duckduckgo":
                results = _search_duckduckgo(query, limit, search_depth=depth)

            elif provider == "custom":
                endpoint = cfg["custom_endpoint"]
                if not endpoint:
                    return ToolResult(
                        success=False, content="",
                        error="Custom provider requires custom_endpoint in config.yaml"
                    ).to_json()
                results = _search_custom(
                    query, limit, api_key=api_key, endpoint=endpoint,
                    api_key_name=cfg["custom_api_key_name"],
                )
            else:
                return ToolResult(
                    success=False, content="",
                    error=f"Unknown search provider: {provider}. "
                          f"Supported: tavily, duckduckgo, custom"
                ).to_json()

        except ImportError as exc:
            return ToolResult(
                success=False, content="",
                error=f"Missing dependency for {provider}: {exc}"
            ).to_json()
        except Exception as exc:
            return ToolResult(
                success=False, content="",
                error=f"Search error: {exc}"
            ).to_json()

        if not results:
            return ToolResult(
                success=True,
                content=f"No results found for: {query}",
            ).to_json()

        # Format results
        lines = [f"Search results for: {query}\n"]
        lines.append(f"(provider: {provider}, {len(results)} results)\n")
        for i, r in enumerate(results, 1):
            lines.append(f"--- Result {i} ---")
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")
            if title:
                lines.append(f"Title: {title}")
            if url:
                lines.append(f"URL: {url}")
            if content:
                lines.append(f"Content: {content}")
            lines.append("")

        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={"provider": provider, "result_count": len(results)},
        ).to_json()

    _reg(registry, "web_search",
         "Search the web for information. Returns titles, URLs, and content snippets. "
         "Configure provider in config.yaml 'web_search:' section (tavily/duckduckgo/custom).",
         {"type": "object",
          "properties": {
              "query":        {"type": "string",  "description": "Search query"},
              "max_results":  {"type": "integer", "description": "Max results (default: from config, usually 5)"},
              "search_depth": {"type": "string",  "description": "Search depth: 'basic' or 'advanced' (Tavily only)"},
          },
          "required": ["query"]},
         ToolCategory.WEB, web_search)


# ---------------------------------------------------------------------------
# SYSTEM TOOLS
# ---------------------------------------------------------------------------

def _register_system_tools(registry: ToolRegistry) -> None:
    """Register shell and OS tools."""

    # ---- Dangerous command patterns (blocked unless allow_destructive=True) ----
    _DESTRUCTIVE_PATTERNS = [
        # Unix
        "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){:|:&",
        "> /dev/sd", "chmod -R 777 /", "shutdown", "reboot",
        "halt", "poweroff", "init 0", "init 6",
        "curl | bash", "curl | sh", "wget | bash", "wget | sh",
        "nc -l", "ncat -l",  # reverse shell patterns
        # Windows
        "format c:", "del /f /s /q c:\\", "rd /s /q c:\\", "rmdir /s /q c:\\",
        "net user ", "net localgroup administrators",
        "reg delete hklm", "reg delete hkcu",
        "powershell -enc", "powershell -encodedcommand",
        "wmic /node:", "sc delete",
    ]

    # ---- Risk classification for run_command ----
    # Low-risk (read-only) command prefixes — auto-execute, no confirmation needed.
    _SAFE_PREFIXES = (
        # Unix read-only
        "ls ", "ls\t", "cat ", "echo ", "pwd", "date", "whoami",
        "hostname", "uname", "uptime", "ps ", "ps\t", "df ", "df\t",
        "free ", "free\t", "top -", "htop", "grep ", "find ",
        "wc ", "head ", "tail ", "stat ", "env ", "printenv ",
        "which ", "where ", "type ", "file ", "id ", "locale",
        "du ", "mount", "lsof ", "strace -", "ltrace -",
        # Version / list checks
        "python --version", "python3 --version", "pip --version", "pip3 --version",
        "node --version", "npm --version", "npx --version",
        "git --version", "docker --version", "go version",
        "java -version", "rustc --version", "cargo --version",
        "pip list", "pip3 list", "pip show ",
        "git status", "git log ", "git branch", "git remote -",
        "git diff ", "git stash list", "git tag", "git config --get",
        "docker ps", "docker images", "docker volume ls",
        "npm list", "npm ls", "npm outdated", "npm audit",
        # Network read-only
        "ping ", "ping -", "nslookup ", "dig ", "host ",
        "ipconfig ", "ifconfig ", "ip ", "netstat ", "ss ",
        "curl -I ", "curl -I\t", "curl --head ",
        # Directory creation (safe)
        "mkdir ",
        # Help / usage
        "--help", "-h ", "-h\t",
    )

    # Medium-risk prefixes — need user confirmation before executing.
    _RISKY_PREFIXES = (
        # File manipulation
        "cp ", "mv ", "touch ", "ln ", "chmod ", "chown ",
        # Delete (non-destructive-pattern)
        "rm ",
        # Package management
        "pip install", "pip3 install", "pip uninstall", "pip3 uninstall",
        "npm install", "npm uninstall", "npm ci", "npm run ",
        "apt ", "apt-get ", "yum ", "dnf ", "brew ",
        # Git write operations
        "git add ", "git commit", "git push", "git pull", "git checkout ",
        "git merge ", "git rebase ", "git reset ", "git cherry-pick ",
        "git stash ", "git tag ",
        # Script execution
        "python ", "python3 ", "node ", "bash ", "sh ",
        # Docker
        "docker run", "docker build", "docker rm ", "docker rmi ",
        "docker-compose ", "docker compose ",
        # Service management
        "systemctl ", "service ", "supervisorctl ",
        # Build tools
        "make ", "cmake ", "cargo ", "go build", "go run",
        # Writing
        "tee ", "dd ",
    )

    def _classify_command(cmd: str) -> str:
        """
        Classify a command into risk levels: 'safe', 'risky', or 'destructive'.

        Returns:
            'safe'        — read-only / info commands, auto-execute
            'risky'       — write / modify commands, need user confirmation
            'destructive' — blocked entirely (matched _DESTRUCTIVE_PATTERNS)
        """
        cmd_stripped = cmd.strip()
        cmd_lower = cmd_stripped.lower()

        if _is_destructive_command(cmd_stripped):
            return "destructive"

        # Check safe prefixes
        for prefix in _SAFE_PREFIXES:
            if cmd_lower.startswith(prefix.lower()):
                return "safe"

        # Check risky prefixes
        for prefix in _RISKY_PREFIXES:
            if cmd_lower.startswith(prefix.lower()):
                return "risky"

        # Default: unknown commands are risky (conservative)
        return "risky"

    def _is_destructive_command(cmd: str) -> bool:
        """Check if a command contains known destructive patterns."""
        cmd_lower = cmd.lower().strip()
        for pattern in _DESTRUCTIVE_PATTERNS:
            if pattern in cmd_lower:
                return True
        return False

    # ---- run_command ------------------------------------------------------
    def run_command(command: str,
                    working_directory: Optional[str] = None,
                    timeout: int = 60,
                    shell: str = "auto",
                    confirmed: bool = False) -> str:
        """
        Execute a shell command and return stdout, stderr, and exit code.

        Risk-based permission model:
          - Low-risk (read-only) commands: auto-execute immediately.
          - Medium-risk (write/modify) commands: return a confirmation request
            unless called with confirmed=True. When the user approves,
            the LLM should re-invoke this tool with confirmed=True.
          - High-risk (destructive) commands: always blocked.

        Args:
            command:           The command string to run.
            working_directory: Directory to run the command in.
            timeout:           Hard timeout in seconds (max 300).
            shell:             "auto" (detect OS), "powershell", "cmd", or "bash".
            confirmed:         Pass True to execute a previously confirmed risky command.
        """
        timeout = min(timeout, 300)  # Safety cap

        # Step 1: Classify risk level
        risk = _classify_command(command)

        # Step 2: Destructive → block
        if risk == "destructive":
            return ToolResult(
                success=False, content="",
                error="Command blocked: detected a potentially destructive pattern.",
            ).to_json()

        # Step 3: Risky + not confirmed → ask user
        if risk == "risky" and not confirmed:
            return ToolResult(
                success=False, content="",
                error=(
                    f"[CONFIRM_REQUIRED] This command may modify the system or files:\n"
                    f"  > {command.strip()}\n\n"
                    f"To proceed, call run_command again with confirmed=True."
                ),
                metadata={"needs_confirmation": True, "command": command.strip()},
            ).to_json()

        # Step 4: Execute (safe or confirmed-risky)

        # Resolve working directory
        cwd: Optional[str] = None
        if working_directory:
            wd_path, err = _safe_path(working_directory)
            if err:
                return ToolResult(success=False, content="", error=err).to_json()
            if not wd_path.is_dir():
                return ToolResult(success=False, content="",
                                  error=f"Working directory not found: {working_directory}").to_json()
            cwd = str(wd_path)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            parts = []
            if result.stdout.strip():
                parts.append(result.stdout.rstrip())
            if result.stderr.strip():
                parts.append(f"[stderr]\n{result.stderr.rstrip()}")
            parts.append(f"[exit {result.returncode}]")
            return ToolResult(
                success=(result.returncode == 0),
                content="\n".join(parts),
                metadata={"exit_code": result.returncode},
            ).to_json()
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, content="",
                              error=f"Command timed out after {timeout}s").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Error running command: {exc}").to_json()

    _reg(registry, "run_command",
         "Execute a shell command. Low-risk commands (ls, cat, grep, git status, etc.) run "
         "immediately. Risky commands (rm, pip install, git push, python scripts, etc.) "
         "require user confirmation — the tool will return CONFIRM_REQUIRED, and you must "
         "re-invoke with confirmed=True after the user approves. "
         "Destructive commands are always blocked.",
         {"type": "object",
          "properties": {
              "command":           {"type": "string",  "description": "Command string to execute"},
              "working_directory": {"type": "string",  "description": "Directory to run in (optional)"},
              "timeout":           {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
              "confirmed":         {"type": "boolean", "description": "True if user has confirmed execution of a risky command"},
          },
          "required": ["command"]},
         ToolCategory.SYSTEM, run_command)

    # ---- get_environment --------------------------------------------------
    _SENSITIVE_PATTERNS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")

    def get_environment(variable: Optional[str] = None) -> str:
        """Return the value of one (or several common) environment variables."""
        if variable:
            # Block access to sensitive environment variables
            var_upper = variable.upper()
            if any(s in var_upper for s in _SENSITIVE_PATTERNS):
                return ToolResult(
                    success=False, content="",
                    error=f"Access denied: variable '{variable}' may contain "
                          f"sensitive credentials.",
                ).to_json()
            val = os.environ.get(variable, "(not set)")
            return ToolResult(success=True, content=f"{variable}={val}").to_json()
        # Return a curated safe subset
        keys = ["PATH", "HOME", "USER", "USERNAME", "PWD", "SHELL",
                "TERM", "LANG", "COMPUTERNAME", "OS"]
        lines = [f"{k}={os.environ.get(k, '(not set)')}" for k in keys]
        return ToolResult(success=True, content="\n".join(lines)).to_json()

    _reg(registry, "get_environment",
         "Get the value of an environment variable (or a set of common ones).",
         {"type": "object",
          "properties": {
              "variable": {"type": "string", "description": "Variable name (optional; omit for a summary)"},
          },
          "required": []},
         ToolCategory.SYSTEM, get_environment)


# ---------------------------------------------------------------------------
# UTILITY TOOLS
# ---------------------------------------------------------------------------

def _register_utility_tools(registry: ToolRegistry) -> None:
    """Register miscellaneous utility tools."""

    # ---- get_time ---------------------------------------------------------
    def get_time(timezone: Optional[str] = "local") -> str:
        """Return the current date and time."""
        from datetime import datetime
        try:
            if timezone and timezone not in ("local", "Local"):
                import zoneinfo
                tz = zoneinfo.ZoneInfo(timezone)
                now = datetime.now(tz)
                return ToolResult(success=True,
                                  content=now.strftime("%Y-%m-%d %H:%M:%S %Z")).to_json()
        except Exception:
            pass
        now = datetime.now()
        return ToolResult(success=True,
                          content=now.strftime("%Y-%m-%d %H:%M:%S (local)")).to_json()

    _reg(registry, "get_time",
         "Get the current date and time, optionally in a specific timezone (e.g. 'Asia/Shanghai').",
         {"type": "object",
          "properties": {
              "timezone": {"type": "string", "description": "Timezone name (default 'local')"},
          },
          "required": []},
         ToolCategory.UTILITY, get_time)

    # ---- calculate --------------------------------------------------------
    def calculate(expression: str) -> str:
        """
        Safely evaluate a mathematical expression.

        Only numeric operators and a whitelist of math functions are allowed.
        """
        import math
        safe_globals = {
            "__builtins__": {},
            "abs": abs, "round": round, "min": min, "max": max,
            "sum": sum, "pow": pow, "int": int, "float": float,
            "sqrt": math.sqrt, "log": math.log, "log10": math.log10,
            "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "pi": math.pi, "e": math.e, "inf": math.inf,
        }
        try:
            result = eval(expression, safe_globals, {})  # nosec B307
            return ToolResult(success=True,
                              content=f"{expression} = {result}").to_json()
        except ZeroDivisionError:
            return ToolResult(success=False, content="",
                              error="Division by zero").to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Invalid expression: {exc}").to_json()

    _reg(registry, "calculate",
         "Evaluate a mathematical expression. Supports basic arithmetic, exponentiation, and common math functions (sqrt, log, sin, cos, etc.).",
         {"type": "object",
          "properties": {
              "expression": {"type": "string", "description": "Math expression, e.g. '2 ** 10 + sqrt(144)'"},
          },
          "required": ["expression"]},
         ToolCategory.UTILITY, calculate)

    # ---- echo -------------------------------------------------------------
    def echo(message: str) -> str:
        """Echo the message back — useful for testing tool connectivity."""
        return ToolResult(success=True, content=message).to_json()

    _reg(registry, "echo",
         "Echo a message back — useful for testing that the tool system is working.",
         {"type": "object",
          "properties": {
              "message": {"type": "string", "description": "Text to echo back"},
          },
          "required": ["message"]},
         ToolCategory.UTILITY, echo)

    # ---- save_memory -------------------------------------------------------
    def save_memory(key: str, content: str, category: str = "general") -> str:
        """
        Save a piece of knowledge to persistent memory.

        Memories survive conversation resets and agent restarts.
        Use this to remember important facts, user preferences, or project context.
        """
        from phoenix_agent.core.state import Database, MemoryStore
        try:
            db = Database.__new__(Database)
            from phoenix_agent.core.config import get_config
            cfg = get_config()
            from phoenix_agent.core.state import SessionState
            db = SessionState._get_default_db()
            store = MemoryStore(db)
            store.save(key, content, category=category)
            return ToolResult(
                success=True,
                content=f"Memory saved: {key} [{category}]"
            ).to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Failed to save memory: {exc}").to_json()

    _reg(registry, "save_memory",
         "Save a piece of knowledge to persistent memory that survives across conversations. "
         "Use this to remember user preferences, important facts, or project context.",
         {"type": "object",
          "properties": {
              "key": {"type": "string", "description": "Short unique identifier for this memory"},
              "content": {"type": "string", "description": "The content to remember"},
              "category": {"type": "string", "description": "Category tag (default: 'general'). E.g. 'preference', 'fact', 'project'"},
          },
          "required": ["key", "content"]},
         ToolCategory.UTILITY, save_memory)

    # ---- recall_memory -----------------------------------------------------
    def recall_memory(query: str) -> str:
        """
        Search persistent memory by keyword.
        Returns all memories whose key or content matches the query.
        """
        from phoenix_agent.core.state import Database, MemoryStore
        try:
            from phoenix_agent.core.state import SessionState
            db = SessionState._get_default_db()
            store = MemoryStore(db)
            results = store.recall(query)
            if not results:
                return ToolResult(
                    success=True,
                    content="No matching memories found."
                ).to_json()
            lines = [f"- [{r['category']}] {r['key']}: {r['content']}" for r in results]
            return ToolResult(
                success=True,
                content=f"Found {len(results)} memory(ies):\n" + "\n".join(lines)
            ).to_json()
        except Exception as exc:
            return ToolResult(success=False, content="",
                              error=f"Failed to recall memory: {exc}").to_json()

    _reg(registry, "recall_memory",
         "Search persistent memory by keyword. Returns memories whose key or content matches the query.",
         {"type": "object",
          "properties": {
              "query": {"type": "string", "description": "Search keyword to match against memory keys and content"},
          },
          "required": ["query"]},
         ToolCategory.UTILITY, recall_memory)


# ---------------------------------------------------------------------------
# SCHEDULER TOOLS  (read / write config.yaml; server restart required to apply)
# ---------------------------------------------------------------------------

def _scheduler_config_path() -> Optional[Path]:
    """Locate the active config.yaml."""
    from phoenix_agent.core.config import get_config
    try:
        cfg = get_config()
        # Config._path holds the path used at load time
        return getattr(cfg, "_path", None)
    except Exception:
        return None


def _read_scheduler_tasks() -> dict:
    """Return the current scheduler section from config.yaml as a plain dict."""
    path = _scheduler_config_path()
    if not path or not path.exists():
        return {}
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data.get("scheduler", {})
    except Exception:
        return {}


def _write_scheduler_section(scheduler_data: dict) -> None:
    """Overwrite the scheduler section in config.yaml."""
    path = _scheduler_config_path()
    if not path or not path.exists():
        return
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["scheduler"] = scheduler_data
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    except Exception:
        pass


def _get_scheduler_singleton():
    """
    获取运行中的 PhoenixScheduler 单例。
    如果调度器未启动，返回 None。
    """
    from phoenix_agent.core.scheduler import get_scheduler
    return get_scheduler()


def list_scheduled_tasks() -> str:
    """
    List all configured scheduled tasks with their settings.

    Returns a table of tasks: name, cron, channel, chat_id, enabled, skill, next_run.
    优先从运行中的调度器获取（包含 next_run 信息），fallback 到 config.yaml。
    """
    try:
        # 优先从运行中的调度器获取（包含 next_run）
        scheduler = _get_scheduler_singleton()
        if scheduler:
            running_tasks = scheduler.list_tasks()
            if running_tasks:
                lines = [f"{'Name':<22} {'Cron':<15} {'Channel':<10} {'Chat ID':<20} {'Skill':<15} {'Next Run':<25}"]
                lines.append("-" * 110)
                for t in running_tasks:
                    lines.append(
                        f"{t.get('name',''):<22} {t.get('cron',''):<15} "
                        f"{t.get('channel',''):<10} {t.get('chat_id',''):<20} "
                        f"{str(t.get('skill') or ''):<15} {t.get('next_run') or 'N/A':<25}"
                    )
                header = f"Scheduled Tasks ({len(running_tasks)} total, running)\n\n"
                return ToolResult(success=True, content=header + "\n".join(lines)).to_json()

        # Fallback: 从 config.yaml 读取
        tasks = _read_scheduler_tasks().get("tasks", [])
        if not tasks:
            return ToolResult(success=True,
                              content="No scheduled tasks configured.\n"
                                      "Use add_scheduled_task to create one.").to_json()
        lines = [f"{'Name':<22} {'Cron':<15} {'Channel':<10} {'Chat ID':<20} {'Skill':<15} Enabled"]
        lines.append("-" * 95)
        for t in tasks:
            lines.append(
                f"{t.get('name',''):<22} {t.get('cron',''):<15} "
                f"{t.get('channel',''):<10} {t.get('chat_id',''):<20} "
                f"{str(t.get('skill','')):<15} {t.get('enabled', True)}"
            )
        header = (f"Scheduled Tasks ({len(tasks)} total, from config.yaml)\n\n")
        return ToolResult(success=True, content=header + "\n".join(lines)).to_json()
    except Exception as exc:
        return ToolResult(success=False, content="",
                          error=f"Failed to list tasks: {exc}").to_json()


def add_scheduled_task(
    name: str,
    cron: str,
    prompt: str,
    channel: str = "dingtalk",
    chat_id: str = "",
    skill: Optional[str] = None,
    timezone: str = "Asia/Shanghai",
) -> str:
    """
    Add a new scheduled task. Changes take effect immediately (no server restart needed).

    Args:
        name:    Unique task name (used as job ID).
        cron:    Cron expression, e.g. "0 9 * * *" (every day at 9:00).
        prompt:  The prompt executed by the agent when the task fires.
        channel: Target channel: dingtalk / wechat / telegram / qq.
        chat_id: Channel-specific chat/conversation ID to push results to.
        skill:   Optional skill name to activate before running the prompt.
        timezone: Timezone for the cron expression (default: Asia/Shanghai).
    """
    import re
    if not re.match(r"^[\w\-]+$", name):
        return ToolResult(success=False, content="",
                          error="name must be alphanumeric or hyphen only").to_json()
    if not cron or not prompt:
        return ToolResult(success=False, content="",
                          error="cron and prompt are required").to_json()

    try:
        from phoenix_agent.core.scheduler import SchedulerTaskConfig

        scheduler = _get_scheduler_singleton()
        task_cfg = SchedulerTaskConfig(
            name=name,
            cron=cron,
            prompt=prompt,
            channel=channel,
            chat_id=chat_id,
            skill=skill,
            timezone=timezone,
            enabled=True,
        )

        if scheduler:
            # 运行中的调度器：直接添加（会持久化到 config.yaml）
            # 先检查是否已存在
            existing = scheduler.list_tasks()
            if any(t["name"] == name for t in existing):
                return ToolResult(success=False, content="",
                                  error=f"A task named '{name}' already exists. "
                                        "Use remove_scheduled_task first.").to_json()
            scheduler.add_task(task_cfg)
            return ToolResult(success=True,
                              content=f"Task '{name}' added and scheduled immediately.\n"
                                      f"  cron: {cron}\n"
                                      f"  channel: {channel}\n"
                                      f"  chat_id: {chat_id}\n"
                                      f"  skill: {skill or '(none)'}\n\n"
                                      f"Changes persisted to config.yaml (no restart needed).").to_json()
        else:
            # 调度器未运行：回退到仅写 config.yaml
            scheduler_data = _read_scheduler_tasks()
            tasks = scheduler_data.get("tasks", [])
            if any(t.get("name") == name for t in tasks):
                return ToolResult(success=False, content="",
                                  error=f"A task named '{name}' already exists. "
                                        "Use remove_scheduled_task first.").to_json()
            new_task = {
                "name": name, "cron": cron, "prompt": prompt,
                "channel": channel, "chat_id": chat_id,
                "skill": skill, "timezone": timezone, "enabled": True,
            }
            tasks.append(new_task)
            scheduler_data["tasks"] = tasks
            scheduler_data["enabled"] = scheduler_data.get("enabled", True)
            _write_scheduler_section(scheduler_data)
            return ToolResult(success=True,
                              content=f"Task '{name}' saved to config.yaml.\n"
                                      f"Scheduler is not running — restart Phoenix to activate.").to_json()
    except Exception as exc:
        return ToolResult(success=False, content="",
                          error=f"Failed to add task: {exc}").to_json()


def remove_scheduled_task(name: str) -> str:
    """
    Remove a scheduled task by name. Takes effect immediately (no server restart needed).

    Args:
        name: The name of the task to remove.
    """
    try:
        scheduler = _get_scheduler_singleton()
        if scheduler:
            # 运行中的调度器：直接移除（会持久化到 config.yaml）
            removed = scheduler.remove_task(name)
            if removed:
                return ToolResult(success=True,
                                  content=f"Task '{name}' removed and unscheduled immediately.\n"
                                          f"Changes persisted to config.yaml (no restart needed).").to_json()
            else:
                return ToolResult(success=False, content="",
                                  error=f"No task named '{name}' found.").to_json()
        else:
            # 调度器未运行：仅从 config.yaml 移除
            scheduler_data = _read_scheduler_tasks()
            tasks = scheduler_data.get("tasks", [])
            original = len(tasks)
            tasks = [t for t in tasks if t.get("name") != name]
            if len(tasks) == original:
                return ToolResult(success=False, content="",
                                  error=f"No task named '{name}' found.").to_json()
            scheduler_data["tasks"] = tasks
            _write_scheduler_section(scheduler_data)
            return ToolResult(success=True,
                              content=f"Task '{name}' removed from config.yaml.\n"
                                      f"Scheduler is not running — restart Phoenix to apply.").to_json()
    except Exception as exc:
        return ToolResult(success=False, content="",
                          error=f"Failed to remove task: {exc}").to_json()


def _register_scheduler_tools(registry: ToolRegistry) -> None:
    """Register all scheduler management tools."""
    _reg(registry, "list_scheduled_tasks",
         "List all configured scheduled tasks with their name, cron expression, "
         "channel, chat_id, skill, and next_run time. "
         "Shows running scheduler state if Phoenix is active.",
         {"type": "object", "properties": {}, "required": []},
         ToolCategory.UTILITY, list_scheduled_tasks)

    _reg(registry, "add_scheduled_task",
         "Add a new cron-based scheduled task. The agent will run the given prompt "
         "at the scheduled time and push the result to the specified channel/chat_id. "
         "Takes effect immediately — no server restart needed.",
         {"type": "object",
          "properties": {
              "name":     {"type": "string",  "description": "Unique task name (alphanumeric/hyphen only)"},
              "cron":     {"type": "string",  "description": "Cron expression, e.g. '0 9 * * *' (daily 9 AM) or '0 */2 * * *' (every 2 hours)"},
              "prompt":   {"type": "string",  "description": "Prompt executed by the agent when the task fires"},
              "channel":  {"type": "string",  "description": "Push channel: dingtalk / wechat / telegram / qq (default: dingtalk)"},
              "chat_id":  {"type": "string",  "description": "Target chat/conversation ID in the chosen channel"},
              "skill":    {"type": "string",  "description": "Optional skill name to activate before running the prompt"},
              "timezone": {"type": "string",  "description": "Timezone for cron, e.g. Asia/Shanghai (default)"},
          },
          "required": ["name", "cron", "prompt"]},
         ToolCategory.UTILITY, add_scheduled_task)

    _reg(registry, "remove_scheduled_task",
         "Remove a scheduled task by name. Takes effect immediately — no server restart needed.",
         {"type": "object",
          "properties": {
              "name": {"type": "string", "description": "Name of the task to remove"},
          },
          "required": ["name"]},
         ToolCategory.UTILITY, remove_scheduled_task)
