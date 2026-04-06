# Changelog

## v0.4.31 - 2026-04-06

- Sync Claude workspace trust into the source `~/.claude.json` project entry before cloning managed runtimes so managed Claude sessions no longer miss the trust-folder state that Codex already inherits.
- Preserve existing Claude source project metadata while adding `hasTrustDialogAccepted`, with atomic writes and a backup file for safety.
- Add regression coverage for managed Claude trust sync and existing source-config preservation.

## v0.4.28 - 2026-04-06

- Fix the managed `--notify` launch regression introduced by the inline tmux worker changes so OpenClaw and other managed-session flows no longer crash with an unbound `host_pane_id`.
- Add regression coverage to keep managed notify launches stable across the inline-pane and dedicated-session paths.

## v0.4.27 - 2026-04-06

- Republish the inline tmux worker pane and explicit CLI success-output changes with a corrected semantic version tag after the previous tag pointed at the wrong commit.

## v0.4.26 - 2026-04-06

- Add inline tmux worker panes for Codex/Claude reviewer-to-worker tmux notify flows so delegated workers can stay visible in the current tmux layout.
- Make `attach`, `close`, and `whoami` behave correctly when multiple `orche` sessions share the same tmux session through inline pane mode.
- Standardize action-oriented CLI success output so commands such as `open`, `prompt`, `attach`, `input`, `key`, `cancel`, and `close` emit explicit machine-readable success messages.

## v0.4.18 - 2026-04-05

- Fix Codex notify payload parsing for current `codex-cli` hook payloads that send hyphenated fields such as `thread-id` and `turn-id`.
- Align Discord and tmux notify status labels with native worker states so `stalled` and `needs-input` no longer collapse to generic `warning`.
- Add regression coverage for watchdog reminder status mapping and current Codex notify payload compatibility.

## v0.4.10 - 2026-04-04

- Avoid tmux window creation failures like `index 0 in use` by explicitly targeting the next available window index when opening new `orche` windows.
- Add regression coverage for window-index allocation so new sessions remain stable in tmux layouts with sparse or preoccupied indexes.
- Make CI assertions environment-safe by removing hard-coded repo paths and normalizing Rich ANSI output in CLI tests.

## v0.4.9 - 2026-04-04

- Export `ORCHE_SESSION` for native Codex and Claude launches so agents can reliably resolve their current session from inside the worker pane.
- Add regression coverage for native launch commands to ensure session context is preserved across `open -- ...` and `orche codex`.
- Refine `SKILL.md` to require session detection with `orche whoami` before choosing tmux notify targets, and to prefer managed sessions when notify routing matters.

## v0.4.8 - 2026-04-04

- Add `orche codex` and `orche claude` shortcut commands that open a fresh native session in the current directory and attach immediately.
- Generate unique shortcut session names with a random suffix so repeated launches in the same repo do not collide.
- Support `-h` on the root command and command groups, plus `-v` on the root command, without expanding short aliases to leaf commands.

## v0.4.3 - 2026-04-03

- Fix Discord notify routing so session-scoped deliveries prefer the session metadata `discord_channel_id` instead of whichever channel was most recently written into global runtime config.
- Prevent `status` from showing a different session's `discord_session` through global-config fallback.
- Add regression coverage for cross-session notify channel mix-ups.

## v0.4.2 - 2026-04-02

- Sync project `trust_level = "trusted"` into the source Codex `config.toml` before cloning managed homes, with atomic writes and a backup file to avoid trust prompt regressions.
- Preserve top-level `notify` handling while refreshing managed Codex homes from the current source config on every reuse.
- Add regression coverage for trust sync, source-config backup behavior, invalid TOML protection, and managed-home refresh behavior.
- Add `sessions list` and `sessions clearall` commands.
- Keep Codex `notify` at the top level of generated `config.toml`.
