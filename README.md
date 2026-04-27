# Phoenix Agent - A lightweight, extensible AI agent framework

## Overview

Phoenix Agent is a Python-based AI agent framework inspired by modern LLM agent architectures. It provides a clean, modular system for building AI agents with tool-calling capabilities.

## Key Features

- **Modular Architecture**: Clean separation between core, tools, providers, and CLI
- **Multi-Provider Support**: OpenAI, Anthropic, and OpenAI-compatible APIs
- **Tool System**: Extensible tool registry with type-safe decorators
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
│   ├── agent.py   # Main agent loop
│   ├── state.py   # Session state management
│   └── message.py # Message types and handling
├── tools/          # Tool system
│   ├── registry.py # Tool registry and discovery
│   └── builtin.py  # Built-in tools
├── providers/      # LLM provider integrations
│   └── base.py     # Base provider interface
├── cli/            # Command-line interface
│   └── interface.py
└── storage/        # Data persistence
    └── db.py       # SQLite storage layer
```

## Security

- All tool inputs are validated against JSON schemas
- Shell commands are sandboxed with path restrictions
- No arbitrary code execution without explicit tool invocation
- API keys never logged or stored in plaintext configs

## License

MIT License - see LICENSE file for details.
