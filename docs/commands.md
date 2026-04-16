# Command Reference

## Core Commands

- `orche open`
  Create or reuse a named control endpoint.

- `orche codex` / `orche claude`
  Open a fresh session for the current directory and attach immediately.

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

## Sessions

Use the session model for normal orchestration:

```bash
orche open --cwd /repo --agent codex --name repo-worker --notify tmux:repo-reviewer
```

`--notify` is optional. Add it when you want automatic routing back to another session or external target.

Rules:

- raw agent CLI args are not supported
- use `--notify` only when you want result routing
- use `--notify tmux:self` to route results back to the current tmux pane
- use `--notify tmux:%12` to route results to a specific live tmux pane
