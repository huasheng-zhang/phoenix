"""
Main CLI Entry Point for Phoenix Agent

Usage:
    phoenix                    # Start interactive mode
    phoenix -q "Hello"        # Single query mode
    phoenix serve              # Start channel webhook server
    phoenix skill list         # List installed skills
    phoenix skill show <name>  # Show skill details
    phoenix skill create <name> # Scaffold a new skill
    phoenix --config my.yaml  # Use custom config
    phoenix --debug            # Enable debug output
"""

import sys
import argparse
import logging


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add args shared by all sub-commands."""
    parser.add_argument(
        "-c", "--config",
        metavar="FILE",
        help="Path to configuration file (default: ~/.phoenix/config.yaml)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phoenix",
        description="Phoenix Agent — A lightweight AI agent framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    phoenix                            # Start interactive REPL
    phoenix -q "List files in ."      # One-shot query
    phoenix serve                      # Start channel webhook server
    phoenix serve --port 9090          # Custom port
    phoenix skill list                 # List installed skills
    phoenix skill show excel-analyst   # Show skill details
    phoenix skill create my-skill      # Scaffold a new skill
    phoenix --config my.yaml           # Use custom config file
    phoenix --debug                    # Enable verbose debug logs
        """,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )
    _add_common_args(parser)

    # Legacy single-query flag (works without sub-command for backwards compat)
    parser.add_argument(
        "-q", "--query",
        metavar="TEXT",
        help="Single query mode — process one message and exit",
    )
    parser.add_argument(
        "-s", "--session",
        metavar="SESSION_ID",
        help="Session ID to resume (interactive mode only)",
    )

    # ----- Sub-commands -----
    subparsers = parser.add_subparsers(dest="subcommand", metavar="COMMAND")

    # ---- phoenix serve ----
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the channel webhook server (DingTalk / WeChat / QQ / Telegram)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Launch an HTTP server that receives messages from configured chat channels\n"
            "and passes them to the Phoenix Agent for processing.\n\n"
            "Configure channels in ~/.phoenix/config.yaml under the 'channels:' section.\n\n"
            "Example config.yaml:\n\n"
            "    channels:\n"
            "      server:\n"
            "        host: 0.0.0.0\n"
            "        port: 8080\n"
            "      dingtalk:\n"
            "        enabled: true\n"
            "        mode: webhook\n"
            "        webhook_url: https://oapi.dingtalk.com/robot/send?access_token=xxx\n"
            "      telegram:\n"
            "        enabled: true\n"
            "        bot_token: 123456:ABCDEF\n"
            "        mode: polling\n"
        ),
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        metavar="HOST",
        help="Bind address (overrides config, default 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="Bind port (overrides config, default 8080)",
    )
    serve_parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on code changes (development only)",
    )
    _add_common_args(serve_parser)

    # ---- phoenix skill ----
    skill_parser = subparsers.add_parser(
        "skill",
        help="Manage skills (list, show, create)",
    )
    skill_sub = skill_parser.add_subparsers(dest="skill_action", metavar="ACTION")

    # phoenix skill list
    skill_sub.add_parser(
        "list",
        help="List all discovered skills",
    )

    # phoenix skill show <name>
    show_parser = skill_sub.add_parser(
        "show",
        help="Show details of a specific skill",
    )
    show_parser.add_argument("name", metavar="NAME", help="Skill name")

    # phoenix skill create <name>
    create_parser = skill_sub.add_parser(
        "create",
        help="Scaffold a new skill directory",
    )
    create_parser.add_argument("name", metavar="NAME", help="Skill name (slug)")
    create_parser.add_argument(
        "--path",
        metavar="DIR",
        help="Create in a specific directory (default: ./skills/)",
    )
    create_parser.add_argument(
        "--description",
        "-d",
        metavar="TEXT",
        default="",
        help="Skill description",
    )

    _add_common_args(skill_parser)

    return parser


def main() -> None:
    """Main entry point for Phoenix Agent CLI."""
    parser = _build_parser()
    args = parser.parse_args()

    # ---- Logging ----
    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    # ---- Version banner ----
    from phoenix_agent import __version__
    print(f"Phoenix Agent v{__version__}")

    try:
        if args.subcommand == "serve":
            _cmd_serve(args)
        elif args.subcommand == "skill":
            _cmd_skill(args)
        elif args.query:
            _cmd_query(args)
        else:
            _cmd_interactive(args)

    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye!")
        sys.exit(0)
    except Exception as exc:
        print(f"\nError: {exc}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def _cmd_serve(args) -> None:
    """Start the channel webhook / long-polling server."""
    from phoenix_agent.channels.server import run_server
    from phoenix_agent.core.config import get_config

    cfg = get_config(path=args.config)

    # CLI args override config
    host = args.host or cfg.channels.host
    port = args.port or cfg.channels.port

    print(f"  Starting channel server on {host}:{port} ...")
    print("  Enabled channels:")
    for ch_name, ch_cfg in cfg.channels.channels.items():
        status = "[ENABLED]" if ch_cfg.enabled else "[DISABLED]"
        print(f"    [{status}] {ch_name:12s}  (webhook: {ch_cfg.webhook_path})")
    print()

    run_server(
        host=host,
        port=port,
        config=cfg,
        reload=args.reload,
        log_level="debug" if args.debug else "info",
    )


def _cmd_query(args) -> None:
    """Single-query mode: process one message and print the response."""
    from rich.console import Console
    from phoenix_agent.cli.interface import run_single

    console = Console()
    with console.status("[bold green]Processing…"):
        response = run_single(args.query, config_path=args.config)
    console.print(response)


def _cmd_interactive(args) -> None:
    """Interactive REPL mode."""
    from phoenix_agent.cli.interface import run_interactive

    run_interactive(config_path=args.config, session_id=args.session)


def _cmd_skill(args) -> None:
    """Handle 'phoenix skill <action>' sub-command."""
    action = getattr(args, "skill_action", None)

    if action == "list":
        _skill_list(args)
    elif action == "show":
        _skill_show(args)
    elif action == "create":
        _skill_create(args)
    else:
        # No action specified — default to list
        _skill_list(args)


def _skill_list(args) -> None:
    """List all discovered skills."""
    from rich.console import Console
    from rich.table import Table
    from phoenix_agent.skills.registry import SkillRegistry

    console = Console()
    registry = SkillRegistry.get_instance()
    discovered = registry.discover()

    if discovered:
        print(f"  Discovered {discovered} new skill(s)")
    print()

    skills = registry.list_skills()
    if not skills:
        console.print("[dim]No skills found.[/dim]")
        console.print()
        console.print("Skill search paths:")
        for p in registry._search_paths:
            marker = "[EXISTS]" if p.is_dir() else "[MISSING]"
            console.print(f"  [{marker}] {p}")
        return

    table = Table(title="Installed Skills", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Version")
    table.add_column("Description")
    table.add_column("Status")
    table.add_column("Triggers")

    for skill in skills:
        status = "[green]LOADED[/green]" if skill.is_loaded else "[dim]ready[/dim]"
        triggers = ", ".join(skill.manifest.triggers[:3])
        if len(skill.manifest.triggers) > 3:
            triggers += f" …+{len(skill.manifest.triggers) - 3}"
        table.add_row(
            skill.name,
            skill.manifest.version,
            skill.description or "-",
            status,
            triggers or "-",
        )

    console.print(table)


def _skill_show(args) -> None:
    """Show details of a specific skill."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from phoenix_agent.skills.registry import SkillRegistry

    console = Console()
    registry = SkillRegistry.get_instance()
    registry.discover()

    skill = registry.get(args.name)
    if not skill:
        console.print(f"[red]Skill '{args.name}' not found.[/red]")
        console.print("Run [bold]phoenix skill list[/bold] to see available skills.")
        return

    info = skill.summary()

    # Display manifest
    console.print(Panel(
        f"[bold]{info['name']}[/bold] v{info['version']}\n"
        f"{info['description']}\n\n"
        f"Status: {'[green]LOADED[/green]' if info['loaded'] else '[dim]ready[/dim]'}\n"
        f"Source: {info['source']}\n"
        f"Tools: {', '.join(info['tools']) or 'none'}\n"
        f"Extra tools: {', '.join(info['tools_extra']) or 'none'}\n"
        f"References: {info['references']} file(s)\n"
        f"Has prompt.md: {'yes' if info['has_prompt'] else 'no'}",
        title=f"Skill: {info['name']}",
    ))

    # Display triggers
    if info['triggers']:
        console.print("\n[bold]Trigger patterns:[/bold]")
        for t in info['triggers']:
            console.print(f"  /{t}/")

    # Display prompt.md preview
    if skill.manifest.prompt_path:
        console.print("\n[bold]prompt.md[/bold] (first 30 lines):")
        try:
            lines = skill.manifest.prompt_path.read_text(encoding="utf-8").splitlines()
            preview = "\n".join(lines[:30])
            if len(lines) > 30:
                preview += f"\n… ({len(lines) - 30} more lines)"
            console.print(Syntax(preview, "markdown", theme="monokai", line_numbers=False))
        except OSError:
            console.print("[dim]  (could not read file)[/dim]")


def _skill_create(args) -> None:
    """Scaffold a new skill directory."""
    from pathlib import Path

    # Determine target directory
    if args.path:
        target_dir = Path(args.path).resolve() / args.name
    else:
        target_dir = Path.cwd() / "skills" / args.name

    if target_dir.exists():
        print(f"[red]Error: {target_dir} already exists.[/red]")
        return

    # Create directories
    (target_dir / "references").mkdir(parents=True)
    (target_dir / "tools").mkdir(parents=True)
    (target_dir / "hooks").mkdir(parents=True)

    # Write SKILL.yaml
    skill_yaml = (
        f"name: {args.name}\n"
        f"version: \"1.0.0\"\n"
        f"description: \"{args.description or 'A new Phoenix Agent skill'}\"\n"
        f"triggers:\n"
        f"  # Add regex patterns to auto-match user input\n"
        f"  # - \"pattern1\"\n"
        f"  # - \"pattern2\"\n"
        f"tools:\n"
        f"  # Built-in tools this skill needs\n"
        f"  # - read_file\n"
        f"  # - write_file\n"
        f"tools_extra: []\n"
        f"env: {{}}\n"
        f"settings: {{}}\n"
    )
    (target_dir / "SKILL.yaml").write_text(skill_yaml, encoding="utf-8")

    # Write prompt.md
    prompt_md = (
        f"# Skill: {args.name}\n\n"
        f"{args.description or 'Describe what this skill does.'}\n\n"
        f"## Instructions\n\n"
        f"When this skill is active, follow these guidelines:\n\n"
        f"1. \n2. \n3. \n"
    )
    (target_dir / "prompt.md").write_text(prompt_md, encoding="utf-8")

    # Write placeholder __init__.py for tools/
    (target_dir / "tools" / "__init__.py").write_text("", encoding="utf-8")

    print(f"  Skill scaffolded at: {target_dir}")
    print(f"  Edit {target_dir / 'SKILL.yaml'} to configure triggers and tools.")
    print(f"  Edit {target_dir / 'prompt.md'} to write the system prompt.")


if __name__ == "__main__":
    main()

