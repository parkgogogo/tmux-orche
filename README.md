# tmux-orche

[中文](README.zh.md)

Modern tmux-backed Codex orchestration for persistent CLI sessions.

`tmux-orche` turns a one-off `orch.py` script into an installable Python CLI with a clean command surface, XDG-based configuration, and a reusable tmux + `tmux-bridge` backend for long-lived Codex orchestration sessions.

## Features

- Installable `orche` command built with Typer + Rich
- Persistent tmux sessions instead of one-shot subprocess execution
- Fire-and-forget prompt submission with later inspection via the same session
- XDG-compliant config and state paths
- Compatible with the existing tmux + `tmux-bridge` orchestration model
- Works with Codex native notify hooks through XDG config at `~/.config/orche/config.json`

## Installation

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

Create a named session and bind a Discord channel for native Codex notify hooks:

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

Send a prompt into an existing session:

```bash
orche prompt --session repo-codex-main --prompt "analyze the test failures"
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
| `orche session-new` | Create or reuse a persistent Codex session. | `--cwd`, `--agent`, `--name`, `--discord-channel-id` |
| `orche prompt` | Send a fire-and-forget prompt to an existing session. | `--session`, `--prompt` |
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
orche prompt --help
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

## Requirements

`orche` depends on these external tools being installed and available:

- `tmux`
- `tmux-bridge`
- `codex` CLI

Python requirements:

- Python `3.9+`

The backend expects:

- tmux can create and manage persistent windows
- `tmux-bridge` can resolve, read, type, and send keys to panes
- `codex` can be launched inside tmux with `--no-alt-screen`

## Backend Model

The workflow is intentionally simple:

1. `session-new` creates or reuses a named tmux-backed Codex session.
2. `prompt` submits work into that existing session and returns immediately.
3. `status` and `read` inspect the live session later.
4. `type`, `keys`, `cancel`, and `close` let you continue steering the same session.

This keeps the orchestration stateful without forcing the CLI itself to stay attached.

## Notification Workflow

`tmux-orche` is designed to work with the existing Codex native notify pipeline. This repository also includes a local hook variant at [`scripts/notify-discord.sh`](./scripts/notify-discord.sh), adapted from the mature `discord-turn-notify.sh` design without modifying the original script in `~/.codex/hooks/`.

### Architecture

```text
Codex native notify
    |
    v
discord-turn-notify.sh
    |
    +--> reads ~/.config/orche/config.json written by orche
    |
    +--> reads Codex JSON payload
    |
    +--> calls orche turn-summary when it needs a concise summary
    |
    v
Discord
```

### What `orche` Provides

- `orche session-new` writes the active session context to both:
  - `~/.config/orche/config.json`
- `orche turn-summary --session <name>` exposes the current turn-summary logic in a CLI-friendly way
- `orche _turn-summary --session <name>` is also available as a hidden compatibility alias
- `orche config get/set/list` provides a stable interface for notification secrets and channel settings

This keeps the design minimal:

- Codex owns notify events
- the existing shell hook owns Discord delivery
- `orche` only provides session metadata and summary extraction

### Codex Native Notify Setup

In `~/.codex/config.toml`:

```toml
notify = ["/bin/bash", "/Users/dnq/.codex/hooks/discord-turn-notify.sh"]
```

That hook can then read `~/.config/orche/config.json`, inspect the Codex payload, and post to Discord.

If you want to use the repo-local adapted hook instead, point Codex at:

```toml
notify = ["/bin/bash", "/path/to/orche/scripts/notify-discord.sh"]
```

### Codex Notify Setup

Automatic setup is intentionally not built into `orche`.

Reasons:

- `~/.codex/config.toml` is global user config, not repo-local state
- users may already have a custom `notify` pipeline that should not be overwritten
- Discord bot tokens are sensitive and should not be silently written into scripts or config files

The recommended setup is manual and explicit.

1. Ensure `~/.codex/config.toml` exists.

2. Add the notify hook entry:

```toml
notify = ["/bin/bash", "/Users/dnq/.codex/hooks/discord-turn-notify.sh"]
```

3. Create `~/.codex/hooks/discord-turn-notify.sh` if it does not already exist.

4. In that hook:
   - read `~/.config/orche/config.json` to get `codex_turn_complete_channel_id`, `session`, and `cwd`
   - read the Codex JSON payload passed into the hook
   - call `orche turn-summary --session "$session"` when you need a concise completion summary

5. Provide secrets safely:
   - prefer `orche config set discord.bot-token ...` or `DISCORD_BOT_TOKEN`
   - avoid hardcoding tokens into tracked files
   - set `discord.channel-id` with `orche config set` or via `orche session-new --discord-channel-id ...`

Example shell pattern:

```bash
orche config set discord.bot-token "your-token"
orche config set discord.channel-id "123456789012345678"
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

Then inside the hook:

```bash
session="$(jq -r '.session // ""' ~/.config/orche/config.json)"
summary="$(orche turn-summary --session "$session" 2>/dev/null || true)"
channel_id="$(orche config get discord.channel-id)"
bot_token="${DISCORD_BOT_TOKEN:-$(orche config get discord.bot-token)}"
```

This keeps the responsibility split cleanly:

- Codex emits notify events
- the hook handles external delivery
- `orche` provides session metadata and summary extraction

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
orche prompt --session repo-codex-main --prompt "review the latest changes"
```

3. When Codex emits a native notify event, the hook reads:

- `codex_turn_complete_channel_id`
- `session`
- `cwd`
- `agent`
- `pane_id`

from `~/.config/orche/config.json`.

4. If the hook needs a short completion summary, it should call:

```bash
orche turn-summary --session repo-codex-main
```

### Hook Integration Note

If your current hook still shells out to the old `orch.py _turn-summary`, update that call site to:

```bash
orche turn-summary --session "$session"
```

or, if you want minimal behavior change:

```bash
orche _turn-summary --session "$session"
```

The rest of the notification design can stay exactly as it is.

## License

MIT
