<h1 align="center">tmux-orche 🎼</h1>

<p align="center">
  <a href="https://github.com/parkgogogo/tmux-orche/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/tmux-required-green" alt="tmux required">
</p>

<p align="center">
  <b>Control plane for tmux-backed agent orchestration.</b><br>
  Hire agents. Route their results. Take over when you need to.
</p>

<p align="center">
  <a href="README.zh.md">中文</a> · <a href="#quick-start">Quick Start</a> · <a href="#installation">Installation</a> · <a href="./docs">Docs</a>
</p>

<p align="center">
  <img src="./assets/Ghostty.gif" alt="Codex and Claude say hello through orche" width="720">
</p>

## What is tmux-orche?

When an agent delegates work to another agent, the hard part isn't starting the task—it's **managing the loop**. You have to keep polling to see if the worker is done, burning tokens on every check. Long tasks are especially painful. If the worker hangs, you only find out after wasting dozens of polling rounds. And when you finally want to jump in and fix something, you have no idea which terminal the agent is running in.

**tmux-orche** turns your tmux sessions into durable, named agent workers. Open a session, send the task, walk away. When the worker finishes, the result routes back automatically—no polling required. If something gets stuck, you or another agent can attach to the exact live terminal and take over.

- **Stable session names** instead of raw pane IDs like `%17`
- **Explicit routing** — results come back exactly where they should
- **Durable terminal state** that survives beyond one prompt
- **Human takeover** when automation needs a nudge

## How It Works

```
                        orche open --notify tmux:supervisor
                        orche prompt worker "do the task"
                                    │
  ┌─────────────┐                   ▼                  ┌─────────────┐
  │  Supervisor  │               tmux session          │   Worker    │
  │  (Codex /    │◂── notify ──  with durable   ──▸   │  (Codex /   │
  │   Claude)    │               terminal state        │   Claude)   │
  └─────────────┘                                      └─────────────┘
        │                                                     │
        │  result arrives,                                    │  task completes,
        │  next turn starts                                   │  fires notify
        ▼                                                     ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │                      Notify Providers                            │
  │   tmux:session   ·   discord:channel   ·   telegram:chat        │
  └──────────────────────────────────────────────────────────────────┘
```

1. **Supervisor** calls `orche open` to create a named worker session with a notify route
2. **Worker** runs inside tmux with full terminal state — durable and inspectable
3. When the task completes, **notify** routes the result back (tmux, Discord, or Telegram)
4. Supervisor's next turn starts automatically — **no polling, no retry logic**

## Quick Start

```bash
# 1. Install
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | sh

# 2. Open a worker, delegate a task, and walk away
orche open \
  --cwd ./my-repo \
  --agent codex \
  --name my-worker \
  --notify tmux:self \
  --prompt "refactor the auth module"

# 3. The result routes back to your current tmux pane when done.
#    Meanwhile, check on progress anytime:
orche status my-worker        # is it alive? turn pending?
orche read my-worker          # peek at terminal output
orche attach my-worker        # take over the live terminal
```

If you need to target a specific pane explicitly, use `--notify tmux:%12`. Use `tmux:<session>` only when you intentionally want to route back to another named `orche` session.

## Usage Scenarios

### 1. Codex / Claude Multi-Agent Loop

Let agents collaborate inside tmux — for example, Claude reviews while Codex writes code:

```bash
# Open a reviewer session
orche open --cwd ./repo --agent claude --name repo-reviewer

# Open a worker that reports back to the reviewer
orche open \
  --cwd ./repo \
  --agent codex \
  --name repo-worker \
  --notify tmux:repo-reviewer

# Delegate work and walk away
orche prompt repo-worker "refactor the auth module"
```

![Claude + Codex workflow](./assets/b9f91b78-1852-453a-8b0d-2cae29185174.png)

*A Codex supervisor using 2 Codex and 2 Claude agents to simultaneously perform code review.*

> **Tip:** For quick single-agent sessions, use the shorthand:
> ```bash
> orche codex   # same as: orche open --agent codex
> orche claude  # same as: orche open --agent claude
> ```

### 2. OpenClaw Supervision Loop

`orche` lets OpenClaw delegate compute-heavy or long-running tasks to specialized agents like Codex or Claude. The worker runs in tmux with durable state, and completion events route back through Discord so OpenClaw can decide what happens next.

> This setup requires creating a separate Discord Bot for Codex and configuring it with `orche config`. See [Configuration](#configuration) for details.

```bash
orche open \
  --cwd ./repo \
  --agent codex \
  --name repo-worker \
  --notify discord:123456789012345678

orche prompt repo-worker "analyze the failing tests"
```

![OpenClaw supervision](./assets/openclaw-supervision-loop.png)

## Commands

| Command | Description |
|---------|-------------|
| `orche open` | Create or reuse a named worker session with a notify route |
| `orche prompt` | Delegate a task into an existing session |
| `orche status` | Check pane/agent health and turn state |
| `orche read` | Peek at terminal output without stealing the TTY |
| `orche attach` | Take over the live terminal |
| `orche close` | End the session and clean up |
| `orche input` | Type text into a session without pressing Enter |
| `orche key` | Send special keys (`Enter`, `Escape`, `C-c`) |
| `orche list` | List locally known sessions |
| `orche cancel` | Interrupt the current turn, keep the session |
| `orche config` | Read or update shared runtime config |

Full reference: [docs/commands.md](./docs/commands.md)

## Installation

### Dependencies

- [tmux](https://github.com/tmux/tmux)
- Python 3.9+ for `pip`, `uv`, or source installs
- `codex` CLI and/or `claude` CLI (depending on which agents you use)

### Quick Install

Install the latest prebuilt binary without Python:

```bash
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | sh
```

Update in place:

```bash
orche update
```

Or install with `uv`:

```bash
uv tool install tmux-orche
```

If `install.sh` or `uv` is not a fit, check `install.md` for `pip`, source checkout, and troubleshooting paths.

Verify the install:

```bash
orche --help
```

## Development

Install the local development toolchain:

```bash
uv sync --extra dev
```

Common checks:

```bash
uv run ruff check .
uv run basedpyright
```

### For Agents

If you want another agent to install `tmux-orche` for you, paste this raw guide link into that agent session:

`https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/install.md`

### Install SKILL (Recommended)

Installing SKILL helps your agent quickly learn how to use `orche`. Pick the one that matches your agent:

<details>
<summary>Codex / Claude / OpenClaw SKILL install commands</summary>

**Codex:**
```bash
mkdir -p ~/.codex/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/codex-claude/SKILL.md \
  -o ~/.codex/skills/orche/SKILL.md
```

**Claude:**
```bash
mkdir -p ~/.claude/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/codex-claude/SKILL.md \
  -o ~/.claude/skills/orche/SKILL.md
```

**OpenClaw:**
```bash
mkdir -p ~/.openclaw/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/openclaw/SKILL.md \
  -o ~/.openclaw/skills/orche/SKILL.md
```

</details>

## Configuration

`orche` stores config in `~/.config/orche/config.json` (or `$XDG_CONFIG_HOME/orche/config.json`).

```bash
# Set Discord notify credentials
orche config set discord.bot-token "$BOT_TOKEN"
orche config set discord.webhook-url "$WEBHOOK_URL"

# Adjust session idle TTL (default 43200s, <=0 disables expiry)
orche config set managed.ttl-seconds 1800

# Override the Claude CLI command
orche config set claude.command /opt/tools/claude-wrapper
```

```bash
orche config list              # view all
orche config get <key>         # view one
orche config reset <key>       # reset to default
```

Full reference: [docs/config.md](./docs/config.md)

## Extending

Both `agent` and `notify` are plugin-based — you can add your own:

- [Developing Agent Plugins](./docs/agent-plugin-dev.md)
- [Developing Notify Plugins](./docs/notify-plugin-dev.md)

### Roadmap

- [x] Discord notifications
- [x] Telegram notifications
- [ ] More agents (e.g. pi)
- [ ] Codex as independent subagent with dedicated skills / MCP

## Acknowledgements

`tmux-orche` was inspired by [ShawnPana/smux](https://github.com/ShawnPana/smux), which gave us many ideas on tmux session management and agent orchestration.

## License

[MIT](LICENSE)
