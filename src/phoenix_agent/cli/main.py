"""
Main CLI Entry Point for Phoenix Agent

Usage:
    phoenix                    # Start interactive mode
    phoenix -q "Hello"        # Single query mode
    phoenix serve              # Start channel webhook server
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


if __name__ == "__main__":
    main()

