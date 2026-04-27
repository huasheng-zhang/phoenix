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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_path(raw: str) -> tuple[Optional[Path], Optional[str]]:
    """
    Resolve and validate a user-supplied path.

    Returns (resolved_path, None) on success or (None, error_message) on
    rejection.  Rejects paths that contain ``..`` to prevent traversal.
    """
    if ".." in raw:
        return None, "Path traversal ('..') is not allowed"
    try:
        resolved = Path(raw).expanduser().resolve()
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

    def web_fetch(url: str, timeout: int = 30) -> str:
        """
        Fetch the content of a URL and return it as text.

        Follows redirects and returns up to 50 000 characters of content.
        """
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


# ---------------------------------------------------------------------------
# SYSTEM TOOLS
# ---------------------------------------------------------------------------

def _register_system_tools(registry: ToolRegistry) -> None:
    """Register shell and OS tools."""

    # ---- run_command ------------------------------------------------------
    def run_command(command: str,
                    working_directory: Optional[str] = None,
                    timeout: int = 60,
                    shell: str = "auto") -> str:
        """
        Execute a shell command and return stdout, stderr, and exit code.

        Args:
            command:           The command string to run.
            working_directory: Directory to run the command in.
            timeout:           Hard timeout in seconds (max 300).
            shell:             "auto" (detect OS), "powershell", "cmd", or "bash".
        """
        timeout = min(timeout, 300)  # Safety cap

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
         "Execute a shell command and return its output (stdout, stderr, exit code). Use this to run scripts, git commands, pip installs, build tools, etc.",
         {"type": "object",
          "properties": {
              "command":           {"type": "string",  "description": "Command string to execute"},
              "working_directory": {"type": "string",  "description": "Directory to run in (optional)"},
              "timeout":           {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
          },
          "required": ["command"]},
         ToolCategory.SYSTEM, run_command)

    # ---- get_environment --------------------------------------------------
    def get_environment(variable: Optional[str] = None) -> str:
        """Return the value of one (or several common) environment variables."""
        if variable:
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
