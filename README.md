<p align="center">
  <img src="./assets/b9f91b78-1852-453a-8b0d-2cae29185174.png" alt="tmux-orche" width="100%">
</p>

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
  <a href="README.zh.md">中文</a> · <a href="#installation">Installation</a> · <a href="./docs">Docs</a>
</p>

## What is tmux-orche?

When an agent delegates work to another agent, the hard part isn't starting the task—it's **managing the loop**.

Before `orche`, you have to **keep polling** to see if the worker is done, burning tokens on every status check. **Long tasks** are especially painful: the worker might run for ten minutes, and you're stuck either waiting idly or building fragile retry logic. If the worker **hangs or stalls**, you only find out after wasting dozens of polling rounds. And when you finally want to **jump in and fix something**, you have no idea which terminal the agent is actually running in.

**tmux-orche** solves this by turning your tmux sessions into durable, named agent workers. Instead of polling, you open a named session, send the task, and walk away. The worker keeps running inside tmux with full terminal state. When it's done, the result routes back automatically. If something gets stuck, you—or another agent—can attach to the exact live terminal and take over.

Whether you are running Codex, Claude, or any OpenClaw-compatible agent, `orche` gives you:

- **Stable session names** instead of raw pane IDs like `%17`
- **Explicit routing** for results to come back exactly where they should
- **Durable terminal state** that survives beyond one prompt
- **Human takeover** when automation needs a nudge

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

### For Agents

If you want another agent to install `tmux-orche` for you, paste this raw guide link into that agent session:

`https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/install.md`

### Install SKILL (Recommended)

Installing SKILL correctly for your agent can help them quickly learn how to use `orche`.

For example, to install the `orche` skill from this repository for Codex:

```bash
mkdir -p ~/.codex/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/codex-claude/SKILL.md \
  -o ~/.codex/skills/orche/SKILL.md
```

For Claude:

```bash
mkdir -p ~/.claude/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/codex-claude/SKILL.md \
  -o ~/.claude/skills/orche/SKILL.md
```

For OpenClaw:

```bash
mkdir -p ~/.openclaw/skills/orche
curl -fsSL https://raw.githubusercontent.com/parkgogogo/tmux-orche/main/skills/openclaw/SKILL.md \
  -o ~/.openclaw/skills/orche/SKILL.md
```

## Commands

`orche` exposes a small set of CLI commands that agents call directly to manage the orchestration loop:

- **`orche open`** — Create or reuse a named control endpoint. An agent calls this to spin up a durable worker with a fixed working directory and an explicit notify route.
- **`orche prompt`** — Delegate work into an existing session. This is how a supervisor agent sends a task to a worker without blocking.
- **`orche status`** — Check whether the pane and agent are alive, and whether a turn is pending. Useful for agents to decide whether to wait or take action.
- **`orche read`** — Inspect recent terminal output without stealing the TTY. Agents use this to catch up on what a worker has produced so far.
- **`orche attach`** — Take over the live terminal. When an agent gets stuck, a human (or another agent) can drop straight into the exact tmux pane.
- **`orche close`** — End the session and clean up state. Called when the worker is no longer needed.

There are also a few helper commands for advanced control:

- **`orche input`** — Type text into a session without pressing Enter.
- **`orche key`** — Send special keys such as `Enter`, `Escape`, or `C-c`.
- **`orche list`** — List locally known sessions.
- **`orche cancel`** — Interrupt the current turn but keep the session alive.
- **`orche config`** — Read or update shared runtime config.

## Usage Scenarios

### 1. Codex / Claude Multi-Agent Loop

Use `orche` when you want agents to collaborate inside tmux. For example, let Claude review while Codex writes code:

For the common case where you just want to open a default worker quickly, `orche` also provides shorthand commands:

```bash
orche codex   # same as: orche open --agent codex
orche claude  # same as: orche open --agent claude
```

These shortcuts are convenient when you want a tmux session with the default agent and do not need to spell out `orche open`.

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

*The image above shows a Codex supervisor using 2 Codex and 2 Claude agents to simultaneously perform code review.*

### 2. OpenClaw Supervision Loop

When using OpenClaw, the configured model may not handle long-running tasks as well as Codex or Claude, especially for coding work. OpenClaw's built-in `acpx` also has many practical issues. A pragmatic approach is to use `orche` to let OpenClaw create Codex or Claude sessions and delegate tasks to them; when the work is done, Codex feeds the result back into the group chat, forming a closed loop.

> This setup requires creating a separate Discord Bot for Codex and configuring it correctly with `orche config`. See [Configuration](#configuration) for details.

When OpenClaw is supervising the worker and the loop should close back into Discord:

```bash
orche open \
  --cwd ./repo \
  --agent codex \
  --name repo-worker \
  --notify discord:123456789012345678

orche prompt repo-worker "analyze the failing tests"
```

![OpenClaw supervision](./assets/openclaw-supervision-loop.png)

OpenClaw opens the worker, the worker runs in tmux with durable state, and completion events route back through Discord so the supervisor can decide what happens next.

### 3. Codex and Claude Say Hello

This is the smallest possible multi-agent loop: open one Claude session, open one Codex session, and let them exchange a short hello through `orche`.

```bash
# Open Claude first
orche open --cwd ./repo --agent claude --name hello-claude

# Open Codex and route results back to Claude
orche open \
  --cwd ./repo \
  --agent codex \
  --name hello-codex \
  --notify tmux:hello-claude

# Let them greet each other
orche prompt hello-codex "Say hello to Claude and keep it short."
orche prompt hello-claude "Reply to Codex with a short hello."
```

<video src="./assets/hello.mp4" controls width="100%"></video>

## Configuration

`orche` stores user configuration in `~/.config/orche/config.json` (or `$XDG_CONFIG_HOME/orche/config.json`).

Common settings you may want to adjust:

```bash
# Override the Claude CLI command
orche config set claude.command /opt/tools/claude-wrapper

# Override Claude source paths mirrored into managed runtimes
orche config set claude.home-path ~/custom/.claude
orche config set claude.config-path ~/custom/claude.json

# Set Discord notify credentials
orche config set discord.bot-token "$BOT_TOKEN"
orche config set discord.mention-user-id 123456789012345678
orche config set discord.webhook-url "$WEBHOOK_URL"

# Adjust managed session idle TTL (default 43200s, <=0 disables expiry)
orche config set managed.ttl-seconds 1800

# Enable or disable notify delivery globally
orche config set notify.enabled true
```

You can also view and reset values:

```bash
orche config list
orche config get claude.command
orche config reset claude.command
```

## Roadmap

- [x] Support Discord notifications
- [x] Support Telegram notifications
- [ ] Support more agents, like pi
- [ ] Support codex as an independent subagent form, with independent skills / mcp, etc., specializing agent capabilities

Because both `notify` and `agent` are designed as plugins, you can also develop your own. Check out:

- [Developing Agent Plugins](./docs/agent-plugin-dev.md)
- [Developing Notify Plugins](./docs/notify-plugin-dev.md)

## Acknowledgements

`tmux-orche` was inspired by [ShawnPana/smux](https://github.com/ShawnPana/smux), which gave us many ideas on tmux session management and agent orchestration.

## License

[MIT](LICENSE)
