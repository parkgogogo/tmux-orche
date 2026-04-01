# tmux-orche

[中文](README.zh.md)

tmux-backed Codex orchestration for OpenClaw and other fire-and-forget workflows.

## Overview

`tmux-orche` is primarily designed to solve one practical problem: OpenClaw needs to hand work off to Codex without staying attached to the full conversation until Codex finishes.

The pattern is:

1. OpenClaw receives a user request.
2. OpenClaw calls `orche` to create or reuse a persistent tmux session that runs Codex.
3. `orche` returns immediately.
4. OpenClaw stops waiting, so it does not keep burning tokens while Codex works.
5. Codex continues in the background inside tmux.
6. When the task completes, a notify hook posts the result back to the same Discord channel.

This fire-and-forget model is the core value of `tmux-orche`: it lets OpenClaw hand off long-running Codex work while keeping OpenClaw's own token usage low.

## Codex Config Limitation

Codex does not support selecting an arbitrary config file path with a CLI flag.

In practice:

- `codex -c ...` overrides individual config values
- it does not mean "load this config.toml file"
- Codex uses its standard config locations such as `~/.codex/config.toml` or project-local `.codex/config.toml`

For multi-session or multi-channel setups, the practical workaround is `CODEX_HOME`.

By default, `tmux-orche` manages this automatically:

- it creates `/tmp/orche-codex-<session>/`
- it copies the contents of `~/.codex/` into that directory
- it rewrites the copied `config.toml` notify entry for the current session and channel
- it launches Codex with `CODEX_HOME` pointing at that temporary directory
- it removes the temporary directory when the session is closed

`--codex-home` remains available as an advanced manual override, but it is no longer required for the normal workflow.

## Primary Use Case

The intended production workflow looks like this:

1. A user sends a task in a Discord server and mentions `@OpenClaw`.
2. OpenClaw reads the message in the main chat channel.
3. OpenClaw calls `orche session-new` and `orche send`.
4. `orche` returns immediately, and OpenClaw ends the turn.
5. Codex keeps running in a persistent tmux session in the background.
6. A notify hook sends a completion message back to the same Discord channel.
7. The user sees the notification and can continue the conversation.

This is why tmux persistence matters: the Codex process survives after OpenClaw has already returned control to Discord.

## Prerequisites

### Runtime Requirements

`orche` depends on these tools:

- `tmux`
- `tmux-bridge`
- `codex` CLI
- Python `3.9+`

### Discord Environment

The core OpenClaw + Codex workflow assumes a Discord server with:

- one Discord Guild
- one channel such as `#coding`, used for both:
  - OpenClaw receiving user messages
  - Codex posting completion notifications back into the same channel

It also assumes two Discord bots:

- `OpenClaw Bot`: receives user messages in that channel and calls `orche`
- `Codex Notify Bot`: posts completion notifications back into that same channel after Codex finishes

### OpenClaw Configuration

An OpenClaw deployment typically enables Discord in `~/.openclaw/openclaw.json`.

Relevant fields include:

- `channels.discord.enabled: true`
- `channels.discord.token`: OpenClaw Bot Token
- `channels.discord.guilds`: allowed guild and user configuration

Example:

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "YOUR_OPENCLAW_BOT_TOKEN",
      "guilds": {
        "123456789012345678": {
          "enabled": true,
          "allowed_users": ["234567890123456789"]
        }
      }
    }
  }
}
```

## Architecture

```text
Discord user
    |
    v
@OpenClaw in main channel (#coding)
    |
    v
OpenClaw Bot
    |
    +--> orche session-new
    |
    +--> orche send
    |
    v
OpenClaw returns immediately
    |
    v
Persistent tmux session
    |
    v
Codex runs in background
    |
    v
notify hook
    |
    v
Codex Notify Bot -> same Discord channel (#coding)
```

### End-to-End Flow

1. User mentions `@OpenClaw` with a coding task.
2. OpenClaw validates the Discord message and allowed guild/user context.
3. OpenClaw starts or reuses a Codex tmux session through `orche`.
4. OpenClaw sends the task to Codex and exits immediately.
5. Codex works asynchronously in tmux.
6. A notify hook posts the result back to the same channel.
7. The user receives the completion signal without OpenClaw holding the entire session open.

## Installation

Agent-guided install guide: <https://github.com/parkgogogo/tmux-orche/raw/main/install.md>

### Install with pip

From PyPI:

```bash
pip install tmux-orche
```

From a local checkout:

```bash
pip install .
```

### Install with uv

As a tool:

```bash
uv tool install tmux-orche
```

From a local checkout:

```bash
uv tool install .
```

If you prefer a project environment instead of a global tool install:

```bash
uv pip install .
```

### Install from source

```bash
git clone https://github.com/parkgogogo/orche
cd orche
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

After installation, the `orche` command should be available on your `PATH`.

## Quick Start

Create or reuse a persistent Codex session:

```bash
orche session-new --cwd /path/to/repo --agent codex
```

Create a named session and bind a Discord channel for notify hooks:

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

By default, `orche` will automatically create and manage a temporary `CODEX_HOME` under `/tmp/orche-codex-<session>/`.

Send a message into an existing session:

```bash
orche send --session repo-codex-main "analyze the test failures"
```

Check session status:

```bash
orche status --session repo-codex-main
```

Read the latest terminal output:

```bash
orche read --session repo-codex-main --lines 80
```

Type text without pressing Enter:

```bash
orche type --session repo-codex-main --text "focus on database queries first"
```

Send keys:

```bash
orche keys --session repo-codex-main --key Enter
orche keys --session repo-codex-main --key Escape --key Enter
```

Get the latest turn summary:

```bash
orche turn-summary --session repo-codex-main
```

Interrupt the running task:

```bash
orche cancel --session repo-codex-main
```

Close the session window:

```bash
orche close --session repo-codex-main
```

## Commands

| Command | Description | Key Options |
| --- | --- | --- |
| `orche backend` | Print the active backend type. | None |
| `orche config get` | Read a supported runtime config value. | `<key>` |
| `orche config set` | Write a supported runtime config value. | `<key>`, `<value>` |
| `orche config list` | List supported runtime config values. | None |
| `orche session-new` | Create or reuse a persistent Codex session. | `--cwd`, `--agent`, `--name`, `--codex-home`, `--discord-channel-id` |
| `orche send` | Send a fire-and-forget message to an existing session. | `--session`, `<message>` |
| `orche status` | Show resolved pane, cwd, running state, and session metadata. | `--session` |
| `orche read` | Read recent pane output through `tmux-bridge`. | `--session`, `--lines` |
| `orche type` | Type text into the session without submitting it. | `--session`, `--text` |
| `orche keys` | Send one or more key presses to the session. | `--session`, `--key` |
| `orche cancel` | Send `Ctrl-C` to the active session. | `--session` |
| `orche turn-summary` | Print the latest inferred turn summary for a session. | `--session` |
| `orche close` | Kill the tmux window and remove local session metadata. | `--session` |

For built-in help:

```bash
orche --help
orche session-new --help
orche send --help
```

## Configuration

`tmux-orche` follows the XDG Base Directory convention.

Primary config file:

```text
~/.config/orche/config.json
```

State directory:

```text
~/.local/share/orche/
```

Typical state files include:

- `meta/<session>.json` for per-session metadata
- `history/<session>.jsonl` for local action history
- `locks/<session>.lock` for session coordination
- `logs/orche.log` for runtime event logging

The runtime config stores fields such as:

- active `session`
- resolved `discord_session`
- `codex_turn_complete_channel_id`
- `codex_home`
- `codex_home_managed`
- current `cwd`, `agent`, and `pane_id`

Notification hooks and helper scripts should read `~/.config/orche/config.json` or use `orche config get ...`.

You can manage notification-related config directly from the CLI:

```bash
orche config set discord.bot-token "YOUR_DISCORD_BOT_TOKEN"
orche config set discord.channel-id "123456789012345678"
orche config set discord.webhook-url "https://discord.com/api/webhooks/..."
orche config set notify.enabled true
orche config list
```

Supported config keys:

- `discord.bot-token`
- `discord.channel-id`
- `discord.webhook-url`
- `notify.enabled`

## Auto-Managed CODEX_HOME

Each `orche` session gets its own Codex home automatically.

For a session named `repo-codex-main`, `orche` will use a path like:

```text
/tmp/orche-codex-repo-codex-main/
```

On `session-new`, `orche` will:

1. create the temporary directory if it does not already exist
2. copy the contents of `~/.codex/` into it
3. write a session-specific `hooks/discord-turn-notify.sh`
4. rewrite the copied `config.toml` so the notify command targets the current session and channel
5. launch Codex with `CODEX_HOME` set to that directory

On `close`, `orche` removes the auto-managed temporary directory.

This gives you:

- one `CODEX_HOME` per session
- one copied `config.toml` per session
- isolated notify settings per session
- isolated Codex history and session state per session

If you need full manual control, `--codex-home` is still available as an advanced override.

## Notification Workflow

`tmux-orche` uses the existing Codex native notify pipeline, but in the default workflow it manages a per-session copied hook automatically. This repository also includes the source hook variant at [`scripts/notify-discord.sh`](./scripts/notify-discord.sh), which `orche` writes into each managed `CODEX_HOME`.

The shell hook is intentionally thin. It just forwards Codex notify events into the Python notify pipeline (`orche _notify-discord`), where payload parsing, message construction, registry lookup, and provider delivery are all testable in isolation.

### What `orche` Provides

- `orche session-new` writes the active session context to `~/.config/orche/config.json`
- `orche session-new` creates a per-session temporary `CODEX_HOME` and rewrites its `config.toml`
- `orche turn-summary --session <name>` exposes the current turn-summary logic in a CLI-friendly way
- `orche _turn-summary --session <name>` is also available as a hidden compatibility alias
- `orche config get/set/list` provides a stable interface for notification secrets and channel settings

This keeps the responsibility split cleanly:

- Codex emits notify events
- the hook handles external delivery
- `orche` provides session metadata and summary extraction

### Automatic Notify Setup

The default flow is automatic.

When you run:

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

`orche` will:

1. copy `~/.codex/` into `/tmp/orche-codex-repo-codex-main/`
2. write `hooks/discord-turn-notify.sh` into that copied home
3. rewrite the copied `config.toml` so the notify command passes the current session and channel
4. start Codex with `CODEX_HOME=/tmp/orche-codex-repo-codex-main/`

This means your global `~/.codex/config.toml` acts as the base template, but each running session gets its own isolated notify configuration automatically.

### What You Still Need To Configure

You still need:

- a valid base `~/.codex/` directory for copying
- Discord credentials via:
  - `orche config set discord.bot-token ...`
  - or `DISCORD_BOT_TOKEN`
- a Discord channel ID via:
  - `orche config set discord.channel-id ...`
  - or `orche session-new --discord-channel-id ...`

Example:

```bash
orche config set discord.bot-token "your-token"
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

### Using `orche` with the Existing Hook

1. Start or reuse a session:

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

2. Send work:

```bash
orche send --session repo-codex-main "review the latest changes"
```

3. When Codex emits a native notify event, the copied session-specific hook reads:

- `codex_turn_complete_channel_id`
- `session`
- `cwd`
- `agent`
- `pane_id`

from the current session context and `~/.config/orche/config.json`.

4. If the hook needs a short completion summary, it should call:

```bash
orche turn-summary --session repo-codex-main
```

### Hook Integration Note

If you are maintaining a custom hook and it still shells out to the old `orch.py _turn-summary`, update that call site to:

```bash
orche turn-summary --session "$session"
```

or, if you want minimal behavior change:

```bash
orche _turn-summary --session "$session"
```

The rest of the notification design can stay exactly as it is.

## Testing

The notify stack is split into small layers so it can be tested without sending real Discord messages:

- payload parsing and message shaping live in `orche.notify.payload`
- provider delivery lives in `orche.notify.discord`
- registry and fan-out live in `orche.notify.registry` and `orche.notify.service`
- the shell hook is only a launcher for `orche _notify-discord`

All automated tests mock HTTP delivery. No test sends a real Discord message.

Run the test suite locally:

```bash
python -m pip install -e .[test]
pytest
```

Coverage is enforced at 100% for the `orche.notify` package in CI.

## License

MIT
