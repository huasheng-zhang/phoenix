# Phoenix Agent - A lightweight, extensible AI agent framework

## Overview

Phoenix Agent is a Python-based AI agent framework inspired by modern LLM agent architectures. It provides a clean, modular system for building AI agents with tool-calling capabilities.

## Key Features

- **Modular Architecture**: Clean separation between core, tools, providers, and CLI
- **Multi-Provider Support**: OpenAI, Anthropic, and OpenAI-compatible APIs
- **Tool System**: Extensible tool registry with type-safe decorators
- **Skill System**: Self-contained capability bundles with auto-triggering (since v1.1)
- **Channel Integration**: DingTalk, WeChat, QQ, Telegram adapters
- **State Management**: SQLite-backed persistent session storage with JSON history
- **Async-First**: Built on asyncio for concurrent operations
- **Security-Focused**: Sandboxed execution, input validation, no arbitrary code injection

## Installation

```bash
# Development installation
pip install -e ".[dev]"

# Production installation
pip install -e .
```

## Quick Start

```python
from phoenix_agent import Agent

# Initialize with default settings
agent = Agent()

# Run a conversation
response = agent.run("What is the capital of France?")
print(response)
```

## Configuration

Phoenix Agent uses a YAML configuration file (`config.yaml`) and environment variables.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PHOENIX_API_KEY` | API key for LLM provider | - |
| `PHOENIX_BASE_URL` | Base URL for API endpoint | - |
| `PHOENIX_MODEL` | Model name | `gpt-4o` |
| `PHOENIX_HOME` | Phoenix home directory | `~/.phoenix` |
| `PHOENIX_SKILL_PATHS` | Extra skill search paths (semicolon-separated on Windows) | - |

### Configuration File

Create `~/.phoenix/config.yaml`:

```yaml
provider:
  type: "openai"  # openai, anthropic, or openai-compatible
  model: "gpt-4o"
  api_key: "${PHOENIX_API_KEY}"
  base_url: null  # Optional for OpenAI-compatible

agent:
  max_iterations: 50
  temperature: 0.7
  system_prompt: "You are a helpful AI assistant."

tools:
  enabled:
    - "file"
    - "web"
    - "terminal"
```

## Architecture

```
phoenix_agent/
├── core/           # Core agent logic
│   ├── config.py   # Configuration management
│   ├── agent.py   # Main agent loop (with skill integration)
│   ├── state.py   # Session state management
│   └── message.py # Message types and handling
├── tools/          # Tool system
│   ├── registry.py # Tool registry and discovery
│   └── builtin.py  # Built-in tools (15 tools)
├── skills/         # Skill extension system
│   ├── manifest.py # SKILL.yaml parsing
│   ├── skill.py    # Runtime skill lifecycle
│   └── registry.py # Skill discovery and matching
├── channels/       # Chat platform adapters
│   ├── dingtalk.py # DingTalk (webhook/stream/internal)
│   ├── wechat.py   # WeChat Work / Official Account
│   ├── qq.py       # QQ via OneBot v11
│   └── telegram.py # Telegram Bot API
├── providers/      # LLM provider integrations
│   └── base.py     # Base provider interface
├── cli/            # Command-line interface
│   └── main.py     # CLI entry point
└── storage/        # Data persistence
    └── db.py       # SQLite storage layer
```

## Skill System

Skills are self-contained capability bundles that extend the agent beyond basic tools. Each skill provides a dedicated system prompt, curated tools, and optional reference documents.

### Create a Skill

```bash
# Scaffold a new skill
phoenix skill create my-analyzer

# This creates:
# skills/my-analyzer/
# ├── SKILL.yaml      # Manifest (metadata, triggers, tools)
# ├── prompt.md       # System prompt / instructions
# ├── references/     # Optional knowledge docs
# ├── tools/          # Optional skill-specific Python tools
# └── hooks/          # Optional lifecycle hooks
```

### SKILL.yaml Manifest

```yaml
name: my-analyzer
version: "1.0.0"
description: "Analyze data and generate reports"
triggers:
  - analyze.*data
  - 数据分析
  - process spreadsheet
tools:
  - read_file
  - run_command
tools_extra: []        # Skill-specific tools: "module:function"
env: {}                 # Environment variables
settings: {}            # Skill settings
```

### Skill Lifecycle

1. **Discovery**: Skills are auto-discovered from `~/.phoenix/skills/` and `./skills/`
2. **Matching**: When a user message matches a skill's trigger patterns, the skill is auto-activated
3. **Activation**: The skill's system prompt is injected, required tools are enabled, env vars are set
4. **Deactivation**: Skill tools are deregistered, env vars restored

### CLI Commands

```bash
phoenix skill list           # List all discovered skills
phoenix skill show <name>    # Show skill details and prompt preview
phoenix skill create <name>  # Scaffold a new skill directory
```

## Security

- All tool inputs are validated against JSON schemas
- Shell commands are sandboxed with path restrictions
- No arbitrary code execution without explicit tool invocation
- API keys never logged or stored in plaintext configs

## License

MIT License - see LICENSE file for details.
