# Agent Roles

This directory contains pre-built specialist agent roles for Phoenix Agent's
multi-agent collaboration system. Each YAML file defines a worker agent that
the main Supervisor agent can delegate tasks to.

## Quick Start

Roles are auto-discovered from this directory when Phoenix starts. No extra
configuration needed — just make sure the `roles/` folder exists alongside your
`config.yaml`.

## Available Roles

| Role | File | Description | Key Tools |
|------|------|-------------|-----------|
| **researcher** | `researcher.yaml` | Web search and information gathering | web_search, web_fetch |
| **coder** | `coder.yaml` | Write, debug, and refactor code | write_file, run_command |
| **reviewer** | `reviewer.yaml` | Code review (security, performance, style) | read_file, grep |
| **writer** | `writer.yaml` | Documentation, README, guides, blog posts | write_file, edit_file |
| **analyst** | `analyst.yaml` | Log analysis, metrics, trend identification | grep, calculate |
| **devops** | `devops.yaml` | Server admin, deployment, CI/CD, monitoring | run_command |
| **translator** | `translator.yaml` | Translation (中/英/日), localization | web_search |
| **summarizer** | `summarizer.yaml` | Condense long content into summaries | read_file, web_fetch |
| **tester** | `tester.yaml` | Write, run, and debug test suites | write_file, run_command |
| **planner** | `planner.yaml` | Architecture design, task decomposition | grep, web_search |

## How Delegation Works

The main Phoenix Agent acts as a **Supervisor**. When it receives a complex task,
it can:

1. `list_agent_roles` — discover available specialists
2. `delegate_to_agent(role, task)` — send a full task (specialist can use tools)
3. `ask_agent(role, question)` — quick consultation (no tool iteration)

Example conversation:

```
User: 帮我审查一下 agent.py 的代码质量
Agent: 我会把代码审查任务委派给 reviewer 专家...
Agent: [calls delegate_to_agent(role="reviewer", task="Review agent.py for...")]
Agent: 审查结果如下：发现 3 个 Major 问题，2 个 Minor 建议...
```

## Creating Custom Roles

Create a new YAML file in this directory (or `~/.phoenix/roles/` for global roles):

```yaml
name: my-specialist
description: "What this specialist does"
system_prompt: |
  You are a specialist that...
tools:
  - read_file
  - run_command
model: null           # null = use default model
max_iterations: 20   # null = use agent default (50)
temperature: 0.3     # null = use agent default (0.7)
```

### Available Tools for Roles

| Category | Tools |
|----------|-------|
| File | read_file, write_file, edit_file, list_directory, create_directory, move_file, delete_file, glob_files |
| Web | web_fetch, web_search |
| System | run_command, get_environment, get_time |
| Utility | calculate, echo, save_memory, recall_memory |
| File Sharing | send_file |
| Scheduler | list_scheduled_tasks, add_scheduled_task, remove_scheduled_task |
| Multi-Agent | list_agent_roles, delegate_to_agent, ask_agent |

## Role Priority

When multiple sources define the same role name, the last one wins:
1. `~/.phoenix/roles/*.yaml` (user-level)
2. `./roles/*.yaml` (project-level)
3. `agent_roles:` section in `config.yaml` (inline config)

## Tips

- Keep `temperature` low (0.1-0.3) for roles that need precision (coder, reviewer, tester)
- Use higher `temperature` (0.5-0.7) for creative roles (writer, planner)
- Limit `max_iterations` for simple roles (translator: 10, summarizer: 15)
- Give generous `max_iterations` to complex roles (coder: 30, tester: 30)
