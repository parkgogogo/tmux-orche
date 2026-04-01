from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .backend import (
    BACKEND,
    OrcheError,
    append_action_history,
    bridge_keys,
    bridge_read,
    bridge_type,
    build_status,
    cancel_session,
    close_session,
    default_session_name,
    ensure_session,
    get_config_value,
    load_history_entries,
    list_config_values,
    latest_turn_summary,
    log_exception,
    resolve_session_context,
    send_prompt,
    set_config_value,
)
from .paths import ensure_directories

app = typer.Typer(
    name="orche",
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Modern CLI for tmux-backed Codex orchestration with tmux-bridge.",
    add_completion=False,
)
config_app = typer.Typer(help="Manage orche runtime configuration.")
app.add_typer(config_app, name="config")
console = Console()
stderr = Console(stderr=True)


def _session_name(name: Optional[str], cwd: Path, agent: str) -> str:
    return name or default_session_name(cwd.resolve(), agent, "main")


def _handle_error(exc: BaseException) -> None:
    if isinstance(exc, subprocess.CalledProcessError):
        log_exception("subprocess.error", exc, cmd=exc.cmd, returncode=exc.returncode)
        detail = (exc.stderr or "").strip() or str(exc)
    else:
        detail = str(exc)
    stderr.print(f"[bold red]Error:[/bold red] {detail}")
    raise typer.Exit(code=1)


def _render_status(info: dict) -> None:
    body = Text()
    body.append("Backend: ", style="bold cyan")
    body.append(f"{info.get('backend', BACKEND)}\n")
    body.append("Session: ", style="bold cyan")
    body.append(f"{info.get('session', '-')}\n")
    body.append("Pane: ", style="bold cyan")
    body.append(f"{info.get('pane_id', '-')}\n")
    body.append("Codex: ", style="bold cyan")
    body.append("running\n" if info.get("codex_running") else "stopped\n", style="green" if info.get("codex_running") else "red")
    body.append("Pane exists: ", style="bold cyan")
    body.append("yes\n" if info.get("pane_exists") else "no\n")
    body.append("CWD: ", style="bold cyan")
    body.append(f"{info.get('cwd', '-')}")
    if info.get("discord_session"):
        body.append("\nDiscord session: ", style="bold cyan")
        body.append(str(info["discord_session"]))
    console.print(Panel.fit(body, title="orche status", border_style="blue"))


@app.callback()
def main_callback(
    version: Optional[bool] = typer.Option(None, "--version", help="Show version and exit."),
) -> None:
    ensure_directories()
    if version:
        console.print(f"orche {__version__}")
        raise typer.Exit()


@app.command("backend")
def backend() -> None:
    console.print(BACKEND)


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Config key to read."),
) -> None:
    try:
        console.print(get_config_value(key))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key to update."),
    value: str = typer.Argument(..., help="Value to write."),
) -> None:
    try:
        set_config_value(key, value)
        console.print(get_config_value(key))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@config_app.command("list")
def config_list() -> None:
    try:
        table = Table(title="orche config")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")
        for key, value in list_config_values().items():
            table.add_row(key, value)
        console.print(table)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("session-new")
def session_new(
    cwd: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Working directory for the Codex session."),
    agent: str = typer.Option(..., help="Agent name. Currently only 'codex' is supported."),
    name: Optional[str] = typer.Option(None, "--name", help="Explicit session name. Defaults to <repo>-<agent>-main."),
    discord_channel_id: Optional[str] = typer.Option(None, "--discord-channel-id", help="Numeric Discord channel ID for notifications."),
) -> None:
    try:
        if agent != "codex":
            raise OrcheError("Only agent=codex is currently supported")
        session = _session_name(name, cwd, agent)
        pane_id = ensure_session(
            session,
            cwd.resolve(),
            agent,
            discord_channel_id=discord_channel_id,
        )
        append_action_history(session, cwd.resolve(), agent, "session-new", pane_id=pane_id)
        console.print(session)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("prompt")
def prompt(
    session: str = typer.Option(..., "--session", help="Session name."),
    prompt: str = typer.Option(..., "--prompt", help="Prompt text to send."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; create it with session-new first")
        send_prompt(session, cwd, agent, prompt)
        console.print(f"Sent. Session: {session}")
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("status")
def status(
    session: str = typer.Option(..., "--session", help="Session name."),
) -> None:
    try:
        _render_status(build_status(session))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("read")
def read(
    session: str = typer.Option(..., "--session", help="Session name."),
    lines: int = typer.Option(50, "--lines", min=1, help="Number of lines to read."),
) -> None:
    try:
        console.print(bridge_read(session, lines))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("type")
def type_text(
    session: str = typer.Option(..., "--session", help="Session name."),
    text: str = typer.Option(..., "--text", help="Text to type without pressing Enter."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; create it with session-new first")
        bridge_type(session, text)
        append_action_history(session, cwd, agent, "type", text=text)
        console.print(session)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("keys")
def keys(
    session: str = typer.Option(..., "--session", help="Session name."),
    key: List[str] = typer.Option(..., "--key", help="Key name to send. Repeat for multiple keys."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; create it with session-new first")
        bridge_keys(session, key)
        append_action_history(session, cwd, agent, "keys", keys=list(key))
        console.print(session)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("cancel")
def cancel(
    session: str = typer.Option(..., "--session", help="Session name."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; create it with session-new first")
        pane_id = cancel_session(session)
        append_action_history(session, cwd, agent, "cancel", pane_id=pane_id)
        console.print(f"Sent Ctrl-C to {pane_id}")
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("close")
def close(
    session: str = typer.Option(..., "--session", help="Session name."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session)
        pane_id = close_session(session)
        if cwd is not None and agent is not None:
            append_action_history(session, cwd, agent, "close", pane_id=pane_id)
        console.print(session)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("turn-summary")
def turn_summary(
    session: str = typer.Option(..., "--session", help="Session name."),
) -> None:
    try:
        console.print(latest_turn_summary(session))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("_turn-summary", hidden=True)
def turn_summary_hidden(
    session: str = typer.Option(..., "--session", help="Session name."),
) -> None:
    turn_summary(session=session)


@app.command("history", hidden=True)
def history(
    session: str = typer.Option(..., "--session", help="Session name."),
    limit: int = typer.Option(20, "--limit", min=1, help="Number of history entries to show."),
) -> None:
    entries = load_history_entries(session)[-limit:]
    if not entries:
        console.print("No history yet")
        return
    for entry in entries:
        console.print(f"{entry.get('timestamp', '-')}\t{entry.get('action', '-')}\t{entry.get('session', '-')}")
        if entry.get("prompt"):
            console.print(f"prompt: {entry['prompt']}")
        if entry.get("keys"):
            console.print(f"keys: {' '.join(entry['keys'])}")
        if entry.get("text"):
            console.print(f"text: {entry['text']}")
        console.print()


def main() -> int:
    try:
        app(standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)
    except Exception as exc:  # pragma: no cover
        log_exception("cli.error", exc)
        _handle_error(exc)
    return 0
