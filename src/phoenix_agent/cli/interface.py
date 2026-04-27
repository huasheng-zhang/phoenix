"""
Command-Line Interface Module

Interactive REPL for Phoenix Agent.

Key design decisions:
- Rich library for colored output and markdown rendering
- Tool call events are surfaced visually so the user can see the agent working
- Supports both interactive and single-query modes
- Session management via /sessions command
"""

import sys
import logging
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from phoenix_agent import Agent
from phoenix_agent.core.config import get_config

logger = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def print_welcome() -> None:
    """Print the startup banner."""
    banner = (
        "\n"
        "  [bold cyan]Phoenix Agent[/bold cyan]  [dim]v1.0[/dim]\n"
        "\n"
        "  An autonomous AI agent that [bold]executes tasks[/bold] using tools.\n"
        "  Ask it to read files, run commands, fetch URLs, do math…\n"
        "\n"
        "  Commands: [cyan]/help[/cyan]  [cyan]/tools[/cyan]  [cyan]/sessions[/cyan]  "
        "[cyan]/reset[/cyan]  [cyan]/clear[/cyan]  [cyan]/quit[/cyan]\n"
    )
    console.print(Panel(banner, border_style="cyan", expand=False))


def print_help() -> None:
    """Print the help panel."""
    lines = """
  [bold]Slash Commands[/bold]

  [cyan]/help[/cyan]          Show this help
  [cyan]/tools[/cyan]         List all available tools
  [cyan]/sessions[/cyan]      Show recent conversation sessions
  [cyan]/reset[/cyan]         Start a new conversation (clears history)
  [cyan]/clear[/cyan]         Clear the terminal screen
  [cyan]/quit[/cyan]          Exit

  [bold]What Phoenix can do[/bold]

  • Read, write, edit, search files                  [dim]read_file, edit_file, grep, …[/dim]
  • Run shell commands, scripts, git, pip            [dim]run_command[/dim]
  • Fetch web pages                                  [dim]web_fetch[/dim]
  • Perform calculations                             [dim]calculate[/dim]
  • Move / delete / create files and directories     [dim]move_file, delete_file, …[/dim]

  Just describe what you want in plain language and Phoenix will figure out the steps.
"""
    console.print(Panel(lines, title="[bold green]Help[/bold green]",
                        border_style="green", expand=False))


def print_tool_list(agent: Agent) -> None:
    """Print a formatted table of available tools."""
    enabled = agent.config.tools.enabled
    disabled = agent.config.tools.disabled
    tools = agent.tools.get_definitions(
        enabled=enabled if enabled else None,
        disabled=disabled if disabled else None,
    )
    if not tools:
        console.print("[yellow]No tools available.[/yellow]")
        return

    tbl = Table(title="Available Tools", show_lines=True)
    tbl.add_column("Name",        style="cyan",   no_wrap=True)
    tbl.add_column("Category",    style="yellow", no_wrap=True)
    tbl.add_column("Description")

    for t in sorted(tools, key=lambda x: x["function"]["name"]):
        func = t["function"]
        tbl.add_row(
            func.get("name", ""),
            t.get("category", "utility"),
            func.get("description", "")[:80],
        )
    console.print(tbl)


def print_response(response: str) -> None:
    """Render the agent's final text response with markdown formatting."""
    if not response:
        return
    try:
        console.print(Markdown(response))
    except Exception:
        console.print(response)


def print_tool_call(tool_name: str, args: dict, result_preview: str) -> None:
    """
    Print a compact summary of a completed tool call.

    Shown between the user prompt and the final agent reply so the user
    can see exactly what actions the agent took.
    """
    # Summarise large argument values
    summarised = {}
    for k, v in args.items():
        sv = str(v)
        summarised[k] = sv[:120] + "…" if len(sv) > 120 else sv

    arg_str = "  ".join(f"[dim]{k}[/dim]=[green]{v}[/green]"
                        for k, v in summarised.items())

    # One-line preview of the result
    preview = result_preview.strip().splitlines()[0][:100] if result_preview.strip() else ""

    console.print(
        f"  [bold yellow]▶ {tool_name}[/bold yellow]  {arg_str}\n"
        f"  [dim]  ↳ {preview}[/dim]"
    )


# ---------------------------------------------------------------------------
# Main interactive loop
# ---------------------------------------------------------------------------

def run_interactive(
    config_path: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    """
    Start the interactive agent REPL.

    Args:
        config_path: Optional path to a YAML config file.
        session_id:  Optional session ID to resume.
    """
    print_welcome()

    # --- initialise agent -------------------------------------------------
    try:
        config = get_config(path=config_path) if config_path else None
        agent = Agent(config=config, session_id=session_id)
    except Exception as exc:
        console.print(f"[bold red]Failed to initialise agent:[/bold red] {exc}")
        sys.exit(1)

    # Wire up the tool-call callback so we can print progress
    def _on_tool_call(name: str, args: dict, result) -> None:
        import json as _json
        try:
            parsed = _json.loads(result.to_json())
            preview = parsed.get("content", "") or parsed.get("error", "")
        except Exception:
            preview = str(result)
        print_tool_call(name, args, preview)

    agent.on_tool_call = _on_tool_call

    # --- REPL --------------------------------------------------------------
    while True:
        try:
            raw = console.input("\n[bold green]You:[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Goodbye![/yellow]")
            break

        if not raw:
            continue

        # -- slash commands -------------------------------------------------
        if raw.startswith("/"):
            cmd = raw.lower().split()[0]
            if cmd in ("/quit", "/exit", "/q"):
                console.print("[yellow]Goodbye![/yellow]")
                break
            elif cmd == "/help":
                print_help()
            elif cmd == "/reset":
                agent.reset()
                console.print("[yellow]Conversation reset.[/yellow]")
            elif cmd == "/tools":
                print_tool_list(agent)
            elif cmd == "/sessions":
                try:
                    from phoenix_agent.core.state import Database
                    db = Database(agent.config.storage.db_path)
                    sessions = db.list_sessions(limit=10)
                    if sessions:
                        console.print("[bold]Recent sessions:[/bold]")
                        for s in sessions:
                            sid = s["id"][:8]
                            title = s.get("title", "Untitled")
                            console.print(f"  [cyan]{sid}…[/cyan]  {title}")
                    else:
                        console.print("[dim]No sessions recorded yet.[/dim]")
                except Exception as exc:
                    console.print(f"[red]Error loading sessions:[/red] {exc}")
            elif cmd == "/clear":
                console.clear()
                print_welcome()
            else:
                console.print(f"[yellow]Unknown command:[/yellow] {cmd}  "
                              f"(type [cyan]/help[/cyan] for a list)")
            continue

        # -- agent turn -----------------------------------------------------
        console.print()
        console.rule("[dim]Agent[/dim]", style="blue")
        console.print()

        try:
            # Show a spinner while the agent works
            with Live(
                Spinner("dots", text=Text("thinking…", style="dim")),
                console=console,
                refresh_per_second=12,
                transient=True,
            ):
                response = agent.run(raw)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            continue
        except Exception as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            logger.exception("Error processing message")
            continue

        console.print()
        print_response(response)
        console.print()


# ---------------------------------------------------------------------------
# Single-shot mode
# ---------------------------------------------------------------------------

def run_single(message: str, config_path: Optional[str] = None) -> str:
    """
    Process one message and return the response (non-interactive).

    Args:
        message:     The user message to process.
        config_path: Optional path to a YAML config file.

    Returns:
        The agent's final text response.
    """
    config = get_config(path=config_path) if config_path else None
    agent = Agent(config=config)
    return agent.run(message)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Argument-parsing entry point used by the ``phoenix`` console script."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="phoenix",
        description="Phoenix Agent — an autonomous AI agent that executes tasks using tools.",
    )
    parser.add_argument("-c", "--config",  metavar="FILE",  help="Path to YAML config file")
    parser.add_argument("-s", "--session", metavar="ID",    help="Session ID to resume")
    parser.add_argument("-q", "--query",   metavar="TEXT",  help="Run a single query and exit")
    parser.add_argument("--debug", action="store_true",     help="Enable debug-level logging")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    if args.query:
        response = run_single(args.query, config_path=args.config)
        print(response)
    else:
        run_interactive(config_path=args.config, session_id=args.session)


if __name__ == "__main__":
    main()
