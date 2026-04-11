---
name: orche-openclaw
description: Use this skill when OpenClaw is supervising Codex or Claude through `orche`, and the worker must report back through Discord or Telegram using an explicit `discord:<channel-id>` or `telegram:<chat-id>` notify route. It enforces managed sessions, explicit notify binding, and a fire-and-forget workflow instead of polling or live babysitting.
---

# orche for OpenClaw

This skill is for one supervisor shape only:

- OpenClaw is the supervisor
- the worker runs in an `orche` session
- the control loop must close back through Discord or Telegram

Do not use this skill for agent-to-agent reviewer/worker loops inside tmux. That is a different skill.

## Non-Negotiable Rules

- Treat `notify` as the return path. If the worker must report back, open it with explicit `--notify discord:<channel-id>` or `--notify telegram:<chat-id>`.
- Treat the first task as `open --prompt` and follow-up tasks as `prompt`. After `orche prompt`, do not keep the current turn open just to watch the worker.
- Do not poll by default. Only inspect a worker if the user asked for progress, the worker likely needs input, or you need details for the next decision.
- Use managed sessions for delegated work. A delegated worker that must report back is not a native session.
- Do not invent routing. If you do not know the Discord channel id or Telegram chat id, stop and get it from the user or established context.
- Create a session once, then reuse it through `prompt`, `status`, `read`, `attach`, `input`, `key`, `cancel`, or `close`. Do not call `open` again with the same explicit session name; that errors instead of reusing it.

## Core Model

When OpenClaw delegates through `orche`, the session is the worker endpoint and `notify` is the return path.

Your job is not to keep watching the pane. Your job is to:

1. open a managed worker with explicit Discord or Telegram notify and the first task
2. reuse the session with `prompt` only for follow-up work
3. leave the worker alone
4. inspect only when a real decision requires it

If the worker does not need to report back, `orche` may not be the right tool for that task.

## Default Workflow

Use this sequence unless the user explicitly wants something else:

```bash
# 1. open a managed worker with an explicit Discord return path and first prompt
orche open --cwd /repo --agent codex --name repo-worker --notify discord:123456789012345678 --prompt "analyze the failing tests and propose a fix"

# 1a. or open a managed worker with an explicit Telegram return path and first prompt
orche open --cwd /repo --agent codex --name repo-worker --notify telegram:123456789 --prompt "analyze the failing tests and propose a fix"

# 2. end the current turn unless you have unrelated work that does not depend on the worker
```

Default behavior after the first prompt:

- do not busy-wait
- do not keep the turn alive just to monitor output
- if you have no independent work left, end the current turn immediately
- when the worker reports back through `notify`, that notify becomes the next input to the supervisor loop

For follow-up turns on the same worker, reuse the session:

```bash
orche prompt repo-worker "apply the approved fix and rerun the failing tests"
```

Later, inspect only if needed:

```bash
orche status repo-worker
orche read repo-worker --lines 120
```

Take over the live terminal only if necessary:

```bash
orche attach repo-worker
```

## Notify Policy

Notify is mandatory for delegated OpenClaw workers because it closes the control loop.

Rules:

- use either `discord:<channel-id>` or `telegram:<chat-id>` as the notify target
- do not default to tmux routing in this skill
- do not assume global Discord or Telegram config is enough by itself; the session still needs explicit notify binding
- rely on notify to resume the loop; do not keep the current turn open solely to wait for the worker
- changing the notify target means opening a new session, not mutating the existing one
- do not combine raw agent CLI args after `--` with `--notify`

Managed session example:

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify discord:123456789012345678 --prompt "analyze the failing tests and propose a fix"

# Telegram is equally valid:
orche open --cwd /repo --agent codex --name repo-worker --notify telegram:123456789 --prompt "analyze the failing tests and propose a fix"
```

Native sessions are for ad-hoc interactive work and are not the default here:

```bash
orche open --cwd /repo --agent claude -- --print --help
```

## Inspection Discipline

Prefer `status` before `read`.

Use `status` to answer:

- is the pane alive
- is the agent running
- is there a pending turn
- is watchdog reporting `running`, `stalled`, or `needs-input`

Use `read` only when you need transcript detail.

Use `input` and `key` only for real interactive prompts:

```bash
orche input repo-worker "yes"
orche key repo-worker Enter
```

Do not use `input` as a substitute for a normal follow-up task prompt.

## Recovery

If the worker is stuck but the session should survive:

```bash
orche cancel repo-worker
orche status repo-worker
orche read repo-worker --lines 120
```

If the session is no longer useful:

```bash
orche close repo-worker
```

## Anti-Patterns

Avoid these:

- opening a worker without `--notify` when the result must return to OpenClaw
- polling continuously after `prompt`
- keeping the current turn open only to watch the worker instead of ending it and waiting for notify
- attaching to every worker when `status` or `read` would be enough
- using `input` for normal task delegation
- combining raw agent args with `--notify`
- combining raw agent args with `--prompt`
- opening a second session for every tiny follow-up instead of reusing the existing named session
