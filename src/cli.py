from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import click
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backend import (
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
    load_config,
    load_history_entries,
    list_config_values,
    latest_turn_summary,
    log_event,
    log_exception,
    resolve_session_context,
    send_prompt,
    set_config_value,
)
from notify import NotificationService, build_message_from_payload, load_notify_config, parse_payload
from paths import ensure_directories
from version import __version__

app = typer.Typer(
    name="orche",
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Modern CLI for tmux-backed Codex orchestration with tmux-bridge.",
    add_completion=False,
)
config_app = typer.Typer(help="Manage orche runtime configuration.")
app.add_typer(config_app, name="config")
console = Console()
stderr = Console(stderr=True)

_UNKNOWN_COMMAND = re.compile(r"No such command ['\"]?(?P<command>[^'\".]+)['\"]?\.")


def _bool_label(value: bool) -> str:
    return "yes" if value else "no"


def _configured_label(value: str) -> str:
    return "set" if str(value).strip() else "unset"


def _print_notify_verbose(
    *,
    runtime_config: dict,
    notify_config,
    session: str,
    channel_id: str,
    payload_text: str,
    message,
) -> None:
    console.print("notify config:")
    console.print(f"  enabled: {_bool_label(notify_config.enabled)}")
    console.print(f"  providers: {', '.join(notify_config.providers) or '-'}")
    console.print(f"  discord.bot_token: {_configured_label(notify_config.discord.bot_token)}")
    console.print(f"  discord.webhook_url: {_configured_label(notify_config.discord.webhook_url)}")
    console.print(
        "  discord.mention_user_id: "
        f"{notify_config.discord.mention_user_id or '-'}"
    )
    console.print(
        "  runtime.channel_id: "
        f"{channel_id or runtime_config.get('discord_channel_id') or '-'}"
    )
    console.print(f"  runtime.session: {session or runtime_config.get('session') or '-'}")
    console.print(f"  payload_bytes: {len(payload_text.encode('utf-8'))}")
    parsed_payload = parse_payload(payload_text)
    if parsed_payload is None:
        console.print("  payload_json: invalid")
    else:
        event = (
            parsed_payload.get("event")
            or parsed_payload.get("type")
            or parsed_payload.get("kind")
            or parsed_payload.get("notification_type")
            or parsed_payload.get("name")
            or "-"
        )
        console.print(f"  payload_json: valid (event={event})")
    if message is None:
        console.print("notify message: <none>")
        return
    console.print("notify message:")
    console.print(f"  channel_id: {message.channel_id or '-'}")
    console.print(f"  session: {message.session or '-'}")
    console.print(f"  status: {message.status or '-'}")
    console.print("  content:")
    console.print(message.content)


def _session_name(name: Optional[str], cwd: Path, agent: str) -> str:
    return name or default_session_name(cwd.resolve(), agent, "main")


def _format_click_message(exc: click.ClickException) -> str:
    message = getattr(exc, "message", "") or ""
    match = _UNKNOWN_COMMAND.search(message)
    if match:
        return f"Unknown command: {match.group('command')}"
    return exc.format_message().strip()


def _format_error_detail(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or "").strip() or str(exc)
    if isinstance(exc, click.ClickException):
        return _format_click_message(exc)
    return str(exc)


def _handle_error(exc: BaseException) -> None:
    if isinstance(exc, subprocess.CalledProcessError):
        log_exception("subprocess.error", exc, cmd=exc.cmd, returncode=exc.returncode)
    detail = _format_error_detail(exc)
    stderr.print(f"[bold red]Error:[/bold red] {detail}")
    raise typer.Exit(code=1)


def _print_error(exc: BaseException) -> None:
    detail = _format_error_detail(exc)
    stderr.print(f"[bold red]Error:[/bold red] {detail}")


def _resolve_path(value: Optional[Path], *, must_exist: bool = False, require_dir: bool = False) -> Optional[Path]:
    if value is None:
        return None
    path = value.expanduser()
    if must_exist and not path.exists():
        raise typer.BadParameter(f"Path does not exist: {value}")
    if require_dir and not path.is_dir():
        raise typer.BadParameter(f"Directory does not exist: {value}")
    return path.resolve()


def _resolve_cwd(
    _ctx: typer.Context,
    _param: typer.CallbackParam,
    value: Optional[Path],
) -> Optional[Path]:
    return _resolve_path(value, must_exist=True, require_dir=True)


def _resolve_optional_path(
    _ctx: typer.Context,
    _param: typer.CallbackParam,
    value: Optional[Path],
) -> Optional[Path]:
    return _resolve_path(value)


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
    if info.get("codex_home"):
        body.append("\nCODEX_HOME: ", style="bold cyan")
        body.append(str(info["codex_home"]))
        body.append("\nManaged: ", style="bold cyan")
        body.append("yes" if info.get("codex_home_managed") else "no")
    if info.get("discord_session"):
        body.append("\nDiscord session: ", style="bold cyan")
        body.append(str(info["discord_session"]))
    console.print(Panel.fit(body, title="orche status", border_style="blue"))


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(None, "--version", help="Show version and exit."),
) -> None:
    ensure_directories()
    if version:
        console.print(f"orche {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
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
    cwd: Path = typer.Option(..., callback=_resolve_cwd, file_okay=False, dir_okay=True, resolve_path=False, help="Working directory for the Codex session."),
    agent: str = typer.Option(..., help="Agent name. Currently only 'codex' is supported."),
    name: Optional[str] = typer.Option(None, "--name", help="Explicit session name. Defaults to <repo>-<agent>-main."),
    codex_home: Optional[Path] = typer.Option(None, "--codex-home", callback=_resolve_optional_path, resolve_path=False, help="Optional manual CODEX_HOME override. If omitted, orche manages /tmp/orche-codex-<session> automatically."),
    discord_channel_id: Optional[str] = typer.Option(None, "--discord-channel-id", help="Numeric Discord channel ID to send completion notifications back to."),
) -> None:
    try:
        if agent != "codex":
            raise OrcheError("Only agent=codex is currently supported")
        session = _session_name(name, cwd, agent)
        pane_id = ensure_session(
            session,
            cwd.resolve(),
            agent,
            codex_home=None if codex_home is None else str(codex_home),
            discord_channel_id=discord_channel_id,
        )
        append_action_history(session, cwd.resolve(), agent, "session-new", pane_id=pane_id)
        console.print(session)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("send")
def send(
    session: str = typer.Option(..., "--session", help="Session name."),
    message: str = typer.Argument(..., help="Message text to send."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; create it with session-new first")
        send_prompt(session, cwd, agent, message)
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


@app.command("_notify-discord", hidden=True)
def notify_discord_hidden(
    payload: Optional[str] = typer.Argument(None, help="Optional JSON payload. If omitted, stdin is used."),
    session: Optional[str] = typer.Option(None, "--session", help="Explicit session override."),
    channel_id: Optional[str] = typer.Option(None, "--channel-id", help="Explicit Discord channel override."),
    status: str = typer.Option("success", "--status", help="Delivery status label."),
    verbose: bool = typer.Option(False, "--verbose", help="Print config, payload, and delivery details."),
) -> None:
    try:
        payload_text = payload
        if payload_text is None and not sys.stdin.isatty():
            payload_text = sys.stdin.read()
        runtime_config = load_config()
        resolved_channel_id = channel_id or os.environ.get("ORCHE_DISCORD_CHANNEL_ID", "")
        resolved_session = session or os.environ.get("ORCHE_SESSION", "")
        notify_config = load_notify_config(runtime_config)
        message = build_message_from_payload(
            payload_text or "",
            notify_config=notify_config,
            runtime_config=runtime_config,
            summary_loader=latest_turn_summary,
            explicit_channel_id=resolved_channel_id,
            explicit_session=resolved_session,
            status=status,
        )
        if verbose:
            _print_notify_verbose(
                runtime_config=runtime_config,
                notify_config=notify_config,
                session=resolved_session,
                channel_id=resolved_channel_id,
                payload_text=payload_text or "",
                message=message,
            )
        if not notify_config.enabled:
            console.print("notify skipped: notify.enabled is false")
            log_event(
                "notify.skipped",
                reason="disabled",
                session=resolved_session,
                channel_id=resolved_channel_id,
            )
            return
        if message is None:
            console.print("notify skipped: payload/config did not produce a deliverable message")
            log_event(
                "notify.skipped",
                reason="message-none",
                session=resolved_session,
                channel_id=resolved_channel_id,
            )
            return
        service = NotificationService()
        results = service.send(message, notify_config)
        if not results:
            console.print("notify skipped: no notifier providers resolved")
            log_event(
                "notify.skipped",
                reason="no-providers",
                session=resolved_session or message.session,
                channel_id=resolved_channel_id or message.channel_id,
            )
            return
        has_failure = False
        for result in results:
            state = "ok" if result.ok else "failed"
            detail = result.detail or "-"
            line = f"notify {state}: provider={result.provider} detail={detail}"
            if result.ok:
                console.print(line)
            else:
                stderr.print(line)
                has_failure = True
            log_event(
                "notify.delivery",
                provider=result.provider,
                ok=result.ok,
                detail=result.detail,
                session=resolved_session or message.session,
                channel_id=resolved_channel_id or message.channel_id,
            )
        if has_failure:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:  # pragma: no cover
        stderr.print(f"notify error: {exc}")
        log_exception("notify.error", exc)
        raise typer.Exit(code=1)


@app.command("history")
def history(
    session: str = typer.Option(..., "--session", help="Session name."),
    limit: int = typer.Option(20, "--limit", min=1, help="Number of history entries to show."),
) -> None:
    entries = load_history_entries(session)[-limit:]
    if not entries:
        console.print("No history yet")
        return
    for entry in entries:
        details = ""
        if entry.get("prompt"):
            details = f'prompt: "{entry["prompt"]}"'
        elif entry.get("text"):
            details = f'text: "{entry["text"]}"'
        elif entry.get("keys"):
            details = f'keys: {" ".join(entry["keys"])}'
        line = f"{entry.get('timestamp', '-')}\t{entry.get('action', '-')}\t{entry.get('session', '-')}"
        if details:
            line += f"\t{details}"
        console.print(line)


def main() -> int:
    try:
        app(standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        _print_error(exc)
        return 1
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _print_error(exc)
        return 1
    except Exception as exc:  # pragma: no cover
        log_exception("cli.error", exc)
        _print_error(exc)
        return 1
    return 0
