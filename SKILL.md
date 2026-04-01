---
name: orche
description: Use `orche` when OpenClaw or another agent needs to delegate work to Codex in a persistent tmux session and return immediately. Use it for fire-and-forget Codex handoff, reusing an existing session, checking status, reading output, reviewing session history, closing finished sessions, or managing runtime config.
---

# orche

Use `orche` as the handoff boundary between OpenClaw and a long-running Codex session.

## OpenClaw Workflow

1. Create or reuse a persistent Codex session with `orche session-new`.
2. Send the task with `orche send`.
3. Return immediately. Do not wait for Codex in the same turn.
4. When notify arrives, inspect the same session with `status`, `read`, or `history`.
5. Close the session when the work is done.

## Quick Start

```bash
# Create or reuse a Codex session
orche session-new --cwd /repo --agent codex --name repo-codex-main --discord-channel-id 123

# Send work and return immediately
orche send --session repo-codex-main "analyze this codebase"

# Check later when notify arrives
orche status --session repo-codex-main
orche read --session repo-codex-main --lines 80
orche history --session repo-codex-main --limit 20
```

## Commands

- `session-new`: create or reuse a persistent Codex tmux session
- `send`: send a task into an existing session and return immediately
- `status`: show whether the session and Codex process are still running
- `read`: inspect recent terminal output from the live session
- `history`: inspect recent local control actions for that session
- `close`: terminate the session when it is no longer needed
- `config`: read and update runtime configuration

## Config

Use `orche config` to manage runtime settings:

```bash
orche config list
orche config get discord.channel-id
orche config set discord.channel-id "123"
orche config set discord.bot-token "$TOKEN"
orche config set discord.mention-user-id "123"
orche config set notify.enabled true
```

Config path:

```text
~/.config/orche/config.json
```

## Troubleshooting

### Cancel a stuck turn

If Codex is stuck, running in the wrong direction, or needs to be stopped without losing the session:

```bash
orche cancel --session repo-codex-main
```

This interrupts the current Codex turn but keeps the session alive, allowing you to read output and send a corrected task.

Compare with close:

- `cancel`: Interrupt current turn, keep session (for stuck or still-running tasks)
- `close`: End entire session (for completed or abandoned tasks)
