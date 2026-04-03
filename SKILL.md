---
name: orche
description: Use `orche` when OpenClaw or another agent needs to delegate work to a supported CLI agent in a persistent tmux session and return immediately. Use it for managed `session-new` handoff, required single-channel notify bindings, querying the current session id from inside tmux, reusing sessions, checking status, reading output, reviewing session history, closing finished sessions, or managing shared runtime config.
---

# orche

Use `orche` as the handoff boundary between OpenClaw and a long-running tmux-backed agent session.

## OpenClaw Workflow

1. Create or reuse a persistent managed session with `orche session-new`.
2. Send the task with `orche send`.
3. Return immediately. Do not wait for the agent in the same turn.
4. When notify arrives, inspect the same session with `status`, `read`, or `history`.
5. Close the session when the work is done.

## Quick Start

```bash
# Create or reuse a managed Codex session with a required single notify binding
orche session-new --cwd /repo --agent codex --name repo-codex-main --notify-to tmux-bridge --notify-target repo-codex-reviewer

# Send work and return immediately
orche send --session repo-codex-main "analyze this codebase"

# Check later when notify arrives
orche status --session repo-codex-main
orche read --session repo-codex-main --lines 80
orche history --session repo-codex-main --limit 20

# Query the current session id from inside the agent tmux pane
orche session-id

```

## Commands

- `session-new`: create or reuse a persistent managed tmux session with orche metadata, optional managed runtime home, and a required single notify binding
- `send`: send a task into an existing managed session and return immediately
- `status`: show whether the session and agent process are still running
- `read`: inspect recent terminal output from the live session
- `history`: inspect recent local control actions for that session
- `session-id`, `whoami`: print the current orche session id from `ORCHE_SESSION` or tmux metadata
- `close`: terminate the session when it is no longer needed
- `config`: read and update shared runtime configuration

Use `session-new` for AI-driven delegation flows where orche needs to preserve session metadata, notify bindings, and session lifecycle control.

## Notify

Set exactly one notify channel at session creation time with `session-new`. `--notify-to` and `--notify-target` are required:

```bash
orche session-new \
  --cwd /repo \
  --agent claude \
  --name repo-claude-review \
  --notify-to discord \
  --notify-target 123456789012345678
```

`--notify-to` selects the provider. `--notify-target` carries the provider-specific target value.

Current built-in providers include:

- `discord`: use a Discord channel id as `--notify-target`
- `tmux-bridge`: use a target tmux session name as `--notify-target`

Notify is single-channel per session. To change the notify target or provider, close the session and create a new one.

Inside a managed tmux pane, use `orche session-id` or `orche whoami` to discover the current session id before wiring a target session or other notify flow.

## Config

Use `orche config` to manage shared runtime settings:

```bash
orche config list
orche config set discord.bot-token "$TOKEN"
orche config set discord.mention-user-id "123"
orche config set notify.enabled true
```

Config path:

```text
~/.config/orche/config.json
```

## Agent Plugins

Agents are loaded through a plugin registry. The current built-in plugins are `codex` and `claude`.

- Add or update an agent plugin module under `src/agents/`
- Expose a `PLUGINS` list from that module
- Register the module in `src/agents/registry.py`
- The agent then becomes available to `orche session-new --agent ...`
This keeps agent-specific launch, ready detection, interrupt, and runtime behavior isolated from the generic tmux/session orchestration layer.

## Troubleshooting

### Cancel a stuck turn

If a managed agent session is stuck, running in the wrong direction, or needs to be stopped without losing the session:

```bash
orche cancel --session repo-codex-main
```

This interrupts the current agent turn but keeps the session alive, allowing you to read output and send a corrected task.

Compare with close:

- `cancel`: interrupt current turn, keep session
- `close`: end entire session
