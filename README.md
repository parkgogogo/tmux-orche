[中文](README.zh.md) · [Install Guide](https://github.com/parkgogogo/tmux-orche/raw/main/install.md)

# tmux-orche

Control plane for tmux-backed agent orchestration.

`tmux-orche` exists for one job: let agents call other agents as durable subagents, with explicit routing, recoverable terminal state, and human takeover when needed.

It is not just a wrapper around tmux panes. It gives your agent graph stable session names, control-loop routing, and a way to inspect or attach to the exact live terminal that is doing the work.

## Installation

Full install guide: <https://github.com/parkgogogo/tmux-orche/raw/main/install.md>

Install the latest prebuilt binary without Python:

```bash
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | sh
```

Supported prebuilt targets: `darwin-arm64`, `darwin-x64`, `linux-x64`.

Update a binary install in place:

```bash
orche update
```

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

## Why It Exists

If one agent is going to supervise another, you need more than "run a command in some pane".

You need:

- a stable session name for each worker
- an explicit route for results to come back
- terminal state that survives beyond one prompt
- a way to inspect progress without stealing the TTY
- a way to take over the live terminal when automation is not enough

That is the gap `orche` fills.

## Control Loops

`orche` is most useful when you want a real loop to close, not just a one-shot command to finish.

### OpenClaw -> Codex or Claude -> Discord

Use `discord:<channel-id>` when OpenClaw is supervising the worker and the loop should close back into Discord/OpenClaw.

This is the "external supervisor" path:

- OpenClaw opens or reuses a worker session
- the worker runs in tmux with durable state
- completion or needs-input events route back through Discord notify
- OpenClaw can decide what to do next

### Codex reviewer -> worker -> tmux bridge

Use `tmux:<session>` when another agent session is the supervisor.

This is the "in-terminal reviewer" path:

- a reviewer session delegates work to a worker session
- the worker reports back to the reviewer through tmux bridge
- the reviewer can inspect, continue delegation, or escalate to a human

That is the core model: `orche` is the control plane that lets one agent session address another agent session reliably.

## Why Named Sessions Matter

Raw tmux panes are not a control plane.

With `orche`, you work with `repo-reviewer`, `repo-worker`, or `auth-fixer`, not `%17`.

That difference matters because a named session can carry:

- a working directory
- an agent type
- a persistent tmux pane
- an explicit notify route
- later inspection and human takeover

## Core Workflow

The normal loop is:

1. `open`
2. `prompt`
3. leave
4. `status` or `read` later
5. `attach` if a human needs to take over

## Quick Start

### Fast native attach shortcuts

Open a new native session in the current directory and attach immediately:

```bash
orche codex --model gpt-5.4
orche claude -- --print --help
```

These shortcuts:

- always use the current directory as `cwd`
- forward trailing args to the underlying agent CLI
- create a fresh session name like `<repo>-<agent>-<random>`

### Reviewer-worker loop via tmux bridge

Open a reviewer that receives worker results:

```bash
orche open --cwd /repo --agent codex --name repo-reviewer
```

Open a worker that reports back to the reviewer:

```bash
orche open \
  --cwd /repo \
  --agent codex \
  --name repo-worker \
  --notify tmux:repo-reviewer
```

Send work to the worker:

```bash
orche prompt repo-worker "implement the parser refactor"
```

Check the reviewer later:

```bash
orche read repo-reviewer --lines 120
orche status repo-worker
```

Take over the worker if needed:

```bash
orche attach repo-worker
```

### OpenClaw loop via Discord

Open a worker that reports back through Discord:

```bash
orche open \
  --cwd /repo \
  --agent codex \
  --name repo-worker \
  --notify discord:123456789012345678
```

Send work:

```bash
orche prompt repo-worker "analyze the failing tests and propose a fix"
```

Inspect later or attach directly:

```bash
orche status repo-worker
orche read repo-worker --lines 120
orche attach repo-worker
```

## Best Fit Scenarios

`tmux-orche` is a good fit when you want:

- one reviewer session coordinating multiple workers
- OpenClaw supervising Codex or Claude through Discord notify
- durable worker sessions that accept multiple follow-up prompts
- explicit session-to-session routing inside tmux
- a live terminal takeover path when the loop gets stuck

It is less useful when you only need one short-lived command and do not plan to revisit the session.

## Testing

The repo keeps normal unit/integration tests separate from real end-to-end tests.

Real E2E means:

- real `orche` CLI
- real `tmux`
- real `codex`
- real session-to-session prompt and notify flow

The real E2E suites are:

- `tests/test_notify_e2e.py`
- `tests/test_session_collaboration_e2e.py`

They are opt-in and require a working local environment:

```bash
ORCHE_RUN_E2E=1 python3 -m pytest -q tests/test_notify_e2e.py tests/test_session_collaboration_e2e.py
```

If `tmux` or `codex` is missing, or Codex is not logged in, those suites skip instead of simulating success.

## Managed vs Native Sessions

### Managed session

Use managed mode for normal orchestration:

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify tmux:repo-reviewer
```

This is the default recommendation because `orche` can manage session metadata and routing coherently.

### Native session

Use native mode when you need raw agent CLI args:

```bash
orche open --cwd /repo --agent claude -- --print --help
```

Rules:

- raw agent args must come after `--`
- native sessions do not use `--notify`
- do not mix raw agent args with managed notify routing

## Command Model

- `orche open`
  Create or reuse a named control endpoint.
- `orche codex` / `orche claude`
  Open a fresh native session for the current directory and attach immediately.
- `orche prompt`
  Delegate work into an existing session.
- `orche status`
  Check whether the pane and agent are alive, and whether a turn is pending.
- `orche read`
  Inspect recent terminal output without taking over the TTY.
- `orche attach`
  Attach your terminal to the live tmux session.
- `orche input`
  Type text without pressing Enter.
- `orche key`
  Send special keys such as `Enter`, `Escape`, or `C-c`.
- `orche list`
  List locally known sessions.
- `orche cancel`
  Interrupt the current turn but keep the session alive.
- `orche close`
  End the session and clean up state.
- `orche whoami`
  Print the current session id.
- `orche config`
  Read or update shared runtime config.

## CLI Entry Shortcuts

Use the short flags on CLI entry surfaces:

```bash
orche -h
orche -v
orche config -h
```

Notes:

- `-h` is supported on the root command and command groups
- `-v` is supported on the root command only
- leaf commands still use `--help`, for example `orche attach --help`

## Notify and Routing

Notify is how control loops close.

`orche open --notify` accepts:

- `tmux:<target-session>`
- `discord:<channel-id>`

Use `tmux:<session>` when another agent session should receive the result:

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify tmux:repo-reviewer
```

Use `discord:<channel-id>` when the supervisor is OpenClaw or another Discord-facing control loop:

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify discord:123456789012345678
```

Notes:

- routing is explicit; there is no implicit global default
- changing the notify target means opening a new session
- `tmux` routing is for agent-to-agent loops
- `discord` routing is for OpenClaw or external supervision loops

## Config

```bash
orche config list
orche config get claude.command
orche config get claude.home-path
orche config set claude.command /opt/tools/claude-wrapper
orche config set claude.home-path ~/custom/.claude
orche config set claude.config-path ~/custom/claude.json
orche config set discord.bot-token "$BOT_TOKEN"
orche config set discord.mention-user-id 123456789012345678
orche config set notify.enabled true
```

`orche config get/set/list` reads and writes the same JSON config file. You can update values through the CLI or edit the file directly.

Config file:

```text
~/.config/orche/config.json
```

If `XDG_CONFIG_HOME` is set, `orche` uses:

```text
$XDG_CONFIG_HOME/orche/config.json
```

State directory:

```text
~/.local/share/orche/
```

If `XDG_DATA_HOME` is set, `orche` uses:

```text
$XDG_DATA_HOME/orche/
```

Supported user config keys:

- `claude.command`
  Override the Claude CLI command that `orche` launches. Default is `claude`.
- `claude.home-path`
  Override the Claude source home directory mirrored into managed runtimes. Default is `~/.claude`.
- `claude.config-path`
  Override the Claude source config path used for trust sync. Default is `~/.claude.json`.
- `discord.bot-token`
  Set the Discord bot token used for bot-token delivery.
- `discord.mention-user-id`
  Set the Discord user id to mention in delivered notifications.
- `discord.webhook-url`
  Set the Discord webhook URL used for webhook delivery.
- `notify.enabled`
  Enable or disable notify delivery globally.

Notes:

- `config.json` may also contain session or runtime fields written by `orche` itself.
- Those internal fields are not part of the stable hand-edited config surface.
- Prefer the keys above for user-managed configuration.

### Claude custom config

Use these when your Claude installation is wrapped or its source home/config is not in the default location.

Set a custom Claude executable:

```bash
orche config set claude.command /opt/tools/claude-wrapper
```

Set a custom Claude source home path:

```bash
orche config set claude.home-path ~/custom/.claude
```

Set a custom Claude source config path for trust sync:

```bash
orche config set claude.config-path ~/custom/claude.json
```

What each key changes:

- `claude.command` changes the binary or wrapper command that `orche` executes when it launches Claude.
- `claude.home-path` changes which Claude home directory `orche` mirrors into managed Claude runtimes.
- `claude.config-path` changes which Claude config file `orche` reads when it syncs trust settings into a managed worker runtime.

Typical cases:

- your system command is not literally named `claude`
- you use a wrapper script such as `/opt/tools/claude-wrapper`
- your Claude home directory is not `~/.claude`
- your Claude config lives somewhere other than `~/.claude.json`

Example `config.json`:

```json
{
  "claude_command": "/opt/tools/claude-wrapper",
  "claude_home_path": "/Users/you/custom/.claude",
  "claude_config_path": "/Users/you/custom/claude.json"
}
```

Notes:

- `claude.home-path` and `claude.config-path` affect the source Claude state that `orche` mirrors into managed Claude sessions.
- after changing any of these keys, new Claude sessions use the updated value immediately.
- managed Codex runtimes keep using an isolated `CODEX_HOME`; `orche` also writes `check_for_update_on_startup = false` and `[notice].hide_rate_limit_model_nudge = true` there to avoid startup update checks and model-switch nudges interfering with managed sessions.

## Prerequisites

- `tmux`
- `codex` CLI and/or `claude` CLI
- Python `3.9+`

## License

[MIT](LICENSE)
