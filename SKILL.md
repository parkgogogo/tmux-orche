---
name: orche
description: Use `orche` when OpenClaw or another agent needs fire-and-forget delegation into a persistent tmux-backed CLI agent session. Use it for managed `session-new` handoff, required single-channel notify bindings, querying the current session id from inside tmux, reusing sessions across multiple turns, checking status, reading output, reviewing session history, closing finished sessions, or managing shared runtime config.
---

# orche

Use `orche` as the fire-and-forget handoff boundary between OpenClaw and a long-running tmux-backed agent session.

## Design Philosophy

`orche` is built around one idea: fire-and-forget.

OpenClaw should be able to hand work to Codex or another supported CLI agent, return immediately, and keep moving. The delegated agent keeps working in the background inside tmux. OpenClaw does not need to stay attached to that work, stream every token, or block the current turn waiting for completion.

This matters because blocking delegation is expensive and awkward for agent workflows:

- OpenClaw burns time and context waiting for a long-running task
- the delegated agent may need minutes, not seconds
- interactive CLI agents already work well in tmux if their session stays alive
- the caller usually only needs the result later, not a live transcript right now

`orche` solves that by making the session persistent. Create a session once, reuse it across many sends, and inspect it later through `status`, `read`, `history`, and notify. The session is not a one-shot subprocess. It is a durable workspace for ongoing delegated work.

## Fire-And-Forget Flow

```text
OpenClaw / AI caller
  -> orche session-new
  -> orche send
  -> return immediately

Background tmux session
  -> agent keeps running
  -> agent works asynchronously
  -> notify fires on completion or turn stop

Later follow-up
  -> orche status / read / history
  -> optional next send into the same session
  -> close when the work is done
```

## Workflow

1. Create or reuse a persistent managed session with `orche session-new`.
2. Send the task with `orche send`.
3. Return immediately. Do not wait for the agent in the same turn.
4. Let the agent continue working in the background tmux session.
5. When notify arrives, inspect the same session with `status`, `read`, or `history`.
6. Reuse the same session for the next task, or close it when the work is done.

## Why Persistent Sessions

`orche` sessions are designed to be created once and reused many times.

- the agent keeps its live terminal state
- the session keeps its identity and notify binding
- follow-up tasks can target the same running agent
- OpenClaw can inspect progress without reconstructing state from scratch

This is the core difference from a transient subprocess model. A persistent session turns delegation into an ongoing collaboration channel instead of a one-off shell command.

## Compared To Blocking Calls

Traditional blocking delegation usually looks like this:

1. Spawn a subprocess
2. Wait for it to finish
3. Hold the caller open the whole time
4. Lose the interactive session when the process exits

`orche` instead gives you:

- immediate return after handoff
- background execution in tmux
- durable session identity
- later inspection and continuation
- notify-driven re-entry instead of polling a blocking call

For AI callers, that means less waiting, less wasted context, and cleaner delegation boundaries.

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

## CLI Agent Workflow

`orche` also supports multi-session CLI agent collaboration. A common pattern is one tmux session acting as the worker and another tmux session acting as the reviewer.

Example roles:

- session A: worker session that performs implementation work
- session B: reviewer session that waits for notifications and inspects results

### Session-To-Session Flow

1. Create session B first and keep it available as the review target.
2. Create session A with `--notify-to tmux-bridge --notify-target <session-b>`.
3. Send work into session A.
4. Session A runs in the background and completes its turn.
5. `tmux-bridge` delivers the notify event directly into session B.
6. Session B reads the worker result, reviews it, and can send a follow-up task back through its own workflow.

### Example Commands

```bash
# reviewer session
orche session-new \
  --cwd /repo \
  --agent codex \
  --name repo-reviewer \
  --notify-to discord \
  --notify-target 123456789012345678

# worker session, notify reviewer through tmux-bridge
orche session-new \
  --cwd /repo \
  --agent codex \
  --name repo-worker \
  --notify-to tmux-bridge \
  --notify-target repo-reviewer

# send implementation work to the worker
orche send --session repo-worker "implement the parser refactor"

# later, inspect what the reviewer session received
orche read --session repo-reviewer --lines 120
```

### Session Isolation

Each orche session keeps its own:

- tmux pane and window identity
- runtime metadata
- notify binding
- control history

That isolation allows multiple source sessions to run concurrently without mixing their state.

### Concurrent Notify Safety

When multiple source sessions notify the same target session, orche serializes writes with a target-session I/O lock.

This matters because:

- worker A and worker C may both finish at nearly the same time
- both may target the same reviewer session B
- the lock prevents interleaved writes from corrupting the reviewer pane output

So the advanced collaboration model is safe for fan-in patterns such as many workers reporting into one reviewer session.

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
