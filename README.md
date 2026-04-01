[中文](README.zh.md) · [Install Guide](https://github.com/parkgogogo/tmux-orche/raw/main/install.md)

# tmux-orche

tmux-backed Codex orchestration for OpenClaw fire-and-forget workflows.

`tmux-orche` lets OpenClaw hand work to Codex, return immediately, and continue later through the same persistent tmux session. That keeps OpenClaw from burning tokens while Codex works in the background.

## OpenClaw Workflow

1. OpenClaw creates or reuses a Codex session with `orche session-new`.
2. OpenClaw sends the task with `orche send`.
3. `orche` returns immediately.
4. Codex keeps running in tmux.
5. When notify arrives, OpenClaw or another agent inspects the same session with `status`, `read`, or `history`.
6. The session stays available until it is explicitly closed.

## Quick Start

Create or reuse a session:

```bash
orche session-new \
  --cwd /path/to/repo \
  --agent codex \
  --name repo-codex-main \
  --discord-channel-id 123456789012345678
```

Send work and return immediately:

```bash
orche send --session repo-codex-main "analyze the failing tests and propose a fix"
```

Inspect the same session later:

```bash
orche status --session repo-codex-main
orche read --session repo-codex-main --lines 120
orche history --session repo-codex-main --limit 20
```

Close it when done:

```bash
orche close --session repo-codex-main
```

## Installation

Full step-by-step install guide: <https://github.com/parkgogogo/tmux-orche/raw/main/install.md>

Install from PyPI:

```bash
pip install tmux-orche
```

Install with `uv`:

```bash
uv tool install tmux-orche
```

Install from source:

```bash
git clone https://github.com/parkgogogo/orche
cd orche
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

## Commands

- `orche session-new --cwd /repo --agent codex --name repo-codex-main --discord-channel-id 123456789012345678`
  Create or reuse a persistent Codex tmux session.
- `orche send --session repo-codex-main "review the recent auth changes"`
  Send a task into an existing session and return immediately.
- `orche status --session repo-codex-main`
  Check whether the session and Codex process are still running.
- `orche read --session repo-codex-main --lines 80`
  Read recent terminal output from the live session.
- `orche history --session repo-codex-main --limit 20`
  Show recent local control actions for that session.
- `orche close --session repo-codex-main`
  Close the session when the work is finished.
- `orche config list`
  Show current runtime configuration.

## Config

Manage runtime settings:

```bash
orche config list
orche config get discord.channel-id
orche config set discord.channel-id 123456789012345678
orche config set discord.bot-token "$BOT_TOKEN"
orche config set discord.mention-user-id 123456789012345678
orche config set notify.enabled true
```

Config file:

```text
~/.config/orche/config.json
```

State directory:

```text
~/.local/share/orche/
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

## Prerequisites

- `tmux`
- `tmux-bridge`
- `codex` CLI
- Python `3.9+`

## License

[MIT](LICENSE)
