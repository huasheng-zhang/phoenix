"""CLI Package for Phoenix Agent."""
# main entry point: the full argparse CLI (supports `phoenix serve`)
from phoenix_agent.cli.main import main

# Re-export helpers for programmatic use
from phoenix_agent.cli.interface import run_interactive, run_single

__all__ = ["main", "run_interactive", "run_single"]
