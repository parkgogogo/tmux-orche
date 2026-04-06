# tmux-orche Installation Guide

Install `tmux-orche` when you want persistent agent sessions, explicit handoff, and the ability to inspect or take over the exact same terminal later.

This guide is organized for the shortest path to a working setup:

1. verify dependencies
2. install `tmux-orche`
3. verify the CLI
4. run a minimal real session

## 1. Prerequisites

`tmux-orche` requires:

- `tmux`
- at least one supported agent CLI: `codex` or `claude`

You do **not** need to install `smux` or a separate `tmux-bridge` binary. The tmux notify path is built in.

Python `3.9+` is required only for `pip`, `uv`, or source installs. It is **not** required when you install a prebuilt binary with `install.sh`.

### Check `tmux`

```bash
command -v tmux
tmux -V
```

If `tmux` is missing:

macOS with Homebrew:

```bash
brew install tmux
```

Ubuntu / Debian:

```bash
sudo apt update
sudo apt install -y tmux
```

Fedora:

```bash
sudo dnf install -y tmux
```

### Check agent CLIs

```bash
command -v codex
codex --version

command -v claude
claude --version
```

Install and log in to whichever agent you plan to use:

```bash
codex login
claude login
```

At least one of them must be available.

### Check Python

Skip this section if you plan to use the prebuilt binary installer.

```bash
python3 --version
```

If needed, install Python `3.9+` via your normal system package manager, Homebrew, `pyenv`, or equivalent.

## 2. Install tmux-orche

Choose one installation path.

### Option A: prebuilt binary via `install.sh`

This path does not require a preinstalled Python runtime.

```bash
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | sh
```

Currently published prebuilt targets:

- `darwin-arm64`
- `darwin-x64`
- `linux-x64`

By default the script installs `orche` to:

```bash
~/.local/bin/orche
```

The binary runtime is unpacked under:

```bash
~/.local/share/orche/releases/
```

Optional environment variables:

```bash
ORCHE_INSTALL_VERSION=v0.4.37
ORCHE_INSTALL_PREFIX="$HOME/bin"
```

Examples:

```bash
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | ORCHE_INSTALL_PREFIX="$HOME/bin" sh
curl -fsSL https://github.com/parkgogogo/tmux-orche/raw/main/install.sh | ORCHE_INSTALL_VERSION=v0.4.37 sh
```

To update a binary install later:

```bash
orche update
```

`orche update` is intended for installs managed by `install.sh`. If you installed via `pip`, `uv`, or source checkout, update with the same tool you used to install it.

### Option B: `pip`

From PyPI:

```bash
python3 -m pip install tmux-orche
```

From a local checkout:

```bash
python3 -m pip install .
```

### Option C: `uv`

As a global tool:

```bash
uv tool install tmux-orche
```

From a local checkout:

```bash
uv tool install .
```

Into the current environment:

```bash
uv pip install .
```

### Option D: source checkout

```bash
git clone https://github.com/parkgogogo/orche
cd orche
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install .
```

## 3. Verify the CLI

Make sure the executable is on `PATH`:

```bash
command -v orche
orche --help
```

Make sure config and session commands work:

```bash
orche config list
orche open --help
orche list
```

Optional source-checkout sanity test:

```bash
python3 -m compileall src
```

## 4. Minimal Real Session

The fastest useful validation is to open a real session and inspect it.

### Managed session

```bash
orche open --cwd /path/to/repo --agent codex --name repo-worker --notify tmux:repo-reviewer
orche prompt repo-worker "reply with READY and nothing else"
orche status repo-worker
orche read repo-worker --lines 80
```

### Native session

Use native mode only if you need raw agent CLI args:

```bash
orche open --cwd /path/to/repo --agent claude -- --print --help
```

Rules:

- raw agent args must come after `--`
- native sessions do not take `--notify`
- managed sessions are the default recommendation

## 5. How To Take Over Mid-Flight

You have two normal ways to inspect or take control:

```bash
orche status <session>
orche read <session> --lines 120
orche attach <session>
```

`orche attach` is the cleanest way to jump into the live TTY.

If you want the lower-level tmux view, you can also inspect the tmux server directly:

```bash
tmux ls
tmux attach -t orche-smux
```

`tmux-orche` sessions live as windows inside the tmux session, so `orche list` is usually the clearer operational view.

## 6. Notify Model

Notify is explicit.

`orche open --notify` accepts exactly one value:

- `tmux:<target-session>`
- `discord:<channel-id>`

Examples:

```bash
orche open --cwd /repo --agent codex --name repo-reviewer --notify discord:123456789012345678
orche open --cwd /repo --agent codex --name repo-worker --notify tmux:repo-reviewer
```

There is no default notify route. If you want delivery, bind it explicitly when opening the session.

## 7. Troubleshooting

### `orche: command not found`

The install location is not on `PATH`.

```bash
python3 -m pip show tmux-orche
python3 -m site --user-base
```

If you used `uv tool install`, make sure the uv tool bin directory is on `PATH`.

If you used `install.sh`, make sure the install prefix is on `PATH`:

```bash
echo "$PATH"
ls ~/.local/bin/orche
```

### `tmux is not installed`

Install `tmux`, then verify:

```bash
command -v tmux
tmux -V
```

### `codex` or `claude` is missing

Verify:

```bash
command -v codex
command -v claude
```

### The agent starts but is not logged in

```bash
codex login
claude login
```

### Opening a session fails

Check the basics again:

```bash
command -v tmux
command -v codex
command -v claude
```

Then try the smallest useful command:

```bash
orche open --cwd /path/to/repo --agent codex
```

### Notify does not fire

Check whether you opened the session with an explicit notify binding:

```bash
orche status <session>
```

Examples:

```bash
orche open --cwd /repo --agent codex --notify discord:123456789012345678
orche open --cwd /repo --agent codex --notify tmux:repo-reviewer
```

### The session needs manual inspection

```bash
orche status <session>
orche read <session> --lines 120
orche attach <session>
```

### Config issues

```bash
orche config list
cat ~/.config/orche/config.json
```

For watchdog-generated summaries:

```bash
orche turn-summary --session <session>
```
