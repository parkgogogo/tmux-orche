# tmux-orche

Modern tmux-backed Codex orchestration for persistent CLI sessions.

`tmux-orche` turns a one-off `orch.py` script into an installable Python CLI with a clean command surface, XDG-based configuration, and a reusable tmux + `tmux-bridge` backend for long-lived Codex orchestration sessions.

## Features

- Installable `orche` command built with Typer + Rich
- Persistent tmux sessions instead of one-shot subprocess execution
- Fire-and-forget prompt submission with later inspection via the same session
- XDG-compliant config and state paths
- Compatible with the existing tmux + `tmux-bridge` orchestration model

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

Create a named session and bind a Discord channel for notifications:

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
| `orche session-new` | Create or reuse a persistent Codex session. | `--cwd`, `--agent`, `--name`, `--discord-channel-id` |
| `orche prompt` | Send a fire-and-forget prompt to an existing session. | `--session`, `--prompt` |
| `orche status` | Show resolved pane, cwd, running state, and session metadata. | `--session` |
| `orche read` | Read recent pane output through `tmux-bridge`. | `--session`, `--lines` |
| `orche type` | Type text into the session without submitting it. | `--session`, `--text` |
| `orche keys` | Send one or more key presses to the session. | `--session`, `--key` |
| `orche cancel` | Send `Ctrl-C` to the active session. | `--session` |
| `orche close` | Kill the tmux window and remove local session metadata. | `--session` |

For built-in help:

```bash
orche --help
orche session-new --help
orche prompt --help
```

## Configuration

`tmux-orche` follows the XDG Base Directory convention.

Config file:

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

The runtime config file stores fields such as:

- active `session`
- resolved `discord_session`
- `codex_turn_complete_channel_id`
- current `cwd`, `agent`, and `pane_id`

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

## License

MIT
