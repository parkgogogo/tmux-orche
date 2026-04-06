from __future__ import annotations

import json
import os
import re
import secrets
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
from typer.core import TyperGroup

from backend import (
    BACKEND,
    OrcheError,
    attach_session,
    append_action_history,
    bridge_keys,
    bridge_read,
    bridge_type,
    build_status,
    cancel_session,
    claim_turn_notification,
    close_session,
    complete_pending_turn,
    current_session_id,
    default_session_name,
    ensure_native_session,
    ensure_session,
    get_config_value,
    list_config_values,
    list_sessions,
    load_config,
    load_history_entries,
    latest_turn_summary,
    log_event,
    log_exception,
    release_turn_notification,
    resolve_session_context,
    run_session_watchdog,
    send_prompt,
    session_exists,
    set_config_value,
    supported_agent_names,
)
from notify import (
    NotificationService,
    ResolvedRoute,
    build_message_from_payload,
    dispatch_event,
    load_notify_config,
    parse_payload,
    resolve_routes,
)
from paths import ensure_directories
from version import __version__


class ShortHelpTyperGroup(TyperGroup):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] == "-h":
            args = ["--help", *args[1:]]
        return super().parse_args(ctx, args)

app = typer.Typer(
    name="orche",
    cls=ShortHelpTyperGroup,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Persistent tmux-backed agent sessions.",
    add_completion=False,
)
config_app = typer.Typer(
    cls=ShortHelpTyperGroup,
    help="Manage shared runtime configuration.",
)
app.add_typer(config_app, name="config")
console = Console()
stderr = Console(stderr=True)

_UNKNOWN_COMMAND = re.compile(r"No such command ['\"]?(?P<command>[^'\".]+)['\"]?\.")
_OPEN_CONTEXT = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
}


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
    event,
    routes: list[ResolvedRoute],
) -> None:
    console.print("notify config:")
    console.print(f"  enabled: {_bool_label(notify_config.enabled)}")
    console.print(f"  provider: {notify_config.provider or '-'}")
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
    notify_binding = runtime_config.get("notify_binding")
    if isinstance(notify_binding, dict):
        console.print(f"  runtime.notify_binding: {json.dumps(notify_binding, ensure_ascii=False)}")
    console.print(f"  payload_bytes: {len(payload_text.encode('utf-8'))}")
    parsed_payload = parse_payload(payload_text)
    if parsed_payload is None:
        console.print("  payload_json: invalid")
    else:
        payload_event = (
            parsed_payload.get("event")
            or parsed_payload.get("type")
            or parsed_payload.get("kind")
            or parsed_payload.get("notification_type")
            or parsed_payload.get("name")
            or "-"
        )
        console.print(f"  payload_json: valid (event={payload_event})")
    if event is None:
        console.print("notify event: <none>")
        return
    console.print("notify event:")
    console.print(f"  event: {event.event or '-'}")
    console.print(f"  session: {event.session or '-'}")
    console.print(f"  cwd: {event.cwd or '-'}")
    console.print(f"  status: {event.status or '-'}")
    console.print("  summary:")
    console.print(event.summary)
    console.print("  routes:")
    if not routes:
        console.print("    <none>")
        return
    for route in routes:
        console.print(f"    {route.provider}: {route.target or '-'}")


def _notify_runtime_config(runtime_config: dict, session: str) -> dict:
    merged = dict(runtime_config)
    if not session:
        return merged
    _cwd, _agent, meta = resolve_session_context(session=session)
    if not meta:
        return merged
    for key in (
        "session",
        "cwd",
        "agent",
        "pane_id",
        "runtime_home",
        "runtime_home_managed",
        "runtime_label",
        "codex_home",
        "codex_home_managed",
        "notify_binding",
    ):
        value = meta.get(key)
        if value not in (None, ""):
            merged[key] = value
    if not merged.get("notify_binding"):
        legacy_discord_channel = str(meta.get("discord_channel_id") or "").strip()
        if legacy_discord_channel:
            merged["notify_binding"] = {
                "provider": "discord",
                "target": legacy_discord_channel,
                "session": str(meta.get("discord_session") or ""),
            }
        else:
            legacy_routes = meta.get("notify_routes")
            if isinstance(legacy_routes, dict):
                tmux_route = legacy_routes.get("tmux-bridge")
                if isinstance(tmux_route, dict):
                    target = str(tmux_route.get("target_session") or tmux_route.get("target") or "").strip()
                    if target:
                        merged["notify_binding"] = {
                            "provider": "tmux-bridge",
                            "target": target,
                        }
    notify_binding = merged.get("notify_binding")
    if isinstance(notify_binding, dict) and str(notify_binding.get("provider") or "").strip() == "discord":
        target = str(notify_binding.get("target") or "").strip()
        if target:
            merged["discord_channel_id"] = target
            merged["codex_turn_complete_channel_id"] = target
            merged["discord_session"] = str(notify_binding.get("session") or merged.get("discord_session") or "")
    return merged


def _session_name(name: Optional[str], cwd: Path, agent: str) -> str:
    return name or default_session_name(cwd.resolve(), agent, secrets.token_hex(3))


def _shortcut_session_name(cwd: Path, agent: str) -> str:
    return default_session_name(cwd.resolve(), agent, secrets.token_hex(3))


def _default_cwd() -> Path:
    return Path.cwd().resolve()


def _parse_notify_binding(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    provider, separator, target = raw.partition(":")
    provider = provider.strip().lower()
    target = target.strip()
    if not separator or not provider or not target:
        raise OrcheError("--notify must be in the form <provider>:<target>")
    if provider == "tmux":
        provider = "tmux-bridge"
    if provider not in {"discord", "tmux-bridge"}:
        raise OrcheError("--notify provider must be one of: discord, tmux")
    return provider, target


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


def _print_action_ok(action: str, **fields: object) -> None:
    parts = [f"{key}={value}" for key, value in fields.items() if str(value).strip()]
    suffix = " " + " ".join(parts) if parts else ""
    console.print(f"{action} ok:{suffix}")


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


def _render_status(info: dict) -> None:
    body = Text()
    body.append("Backend: ", style="bold cyan")
    body.append(f"{info.get('backend', BACKEND)}\n")
    body.append("Session: ", style="bold cyan")
    body.append(f"{info.get('session', '-')}\n")
    body.append("Agent: ", style="bold cyan")
    body.append(f"{info.get('agent', '-')}\n")
    body.append("Pane: ", style="bold cyan")
    body.append(f"{info.get('pane_id', '-')}\n")
    body.append("Running: ", style="bold cyan")
    body.append("yes\n" if info.get("agent_running") else "no\n", style="green" if info.get("agent_running") else "red")
    body.append("Pane exists: ", style="bold cyan")
    body.append("yes\n" if info.get("pane_exists") else "no\n")
    body.append("CWD: ", style="bold cyan")
    body.append(f"{info.get('cwd', '-')}")
    notify_binding = info.get("notify_binding")
    if isinstance(notify_binding, dict) and notify_binding:
        body.append("\nNotify: ", style="bold cyan")
        body.append(json.dumps(notify_binding, ensure_ascii=False))
    if info.get("pending_turn_id"):
        body.append("\nPending turn: ", style="bold cyan")
        body.append(str(info["pending_turn_id"]))
    watchdog = info.get("watchdog")
    if isinstance(watchdog, dict) and watchdog:
        body.append("\nWatchdog: ", style="bold cyan")
        body.append(str(watchdog.get("state") or "-"))
    console.print(Panel.fit(body, title="orche status", border_style="blue"))


def _open_session(
    *,
    cwd: Path,
    agent: str,
    name: Optional[str],
    notify: Optional[str],
    cli_args: list[str],
) -> tuple[str, str]:
    session = _session_name(name, cwd, agent)
    if session_exists(session):
        raise OrcheError(
            f"Session {session} already exists. Use 'orche attach {session}' or choose a different --name."
        )
    notify_to, notify_target = _parse_notify_binding(notify)
    if cli_args and notify_to:
        raise OrcheError("open does not support combining --notify with raw agent args")
    if notify_to:
        pane_id = ensure_session(
            session,
            cwd.resolve(),
            agent,
            notify_to=notify_to,
            notify_target=notify_target,
        )
    else:
        pane_id = ensure_native_session(
            session,
            cwd.resolve(),
            agent,
            cli_args=cli_args,
        )
    append_action_history(session, cwd.resolve(), agent, "open", pane_id=pane_id)
    return session, pane_id


def _open_shortcut_session(ctx: typer.Context, agent: str) -> tuple[str, str]:
    resolved_cwd = _resolve_path(_default_cwd(), must_exist=True, require_dir=True)
    if resolved_cwd is None:
        raise OrcheError("Failed to resolve cwd")
    return _open_session(
        cwd=resolved_cwd,
        agent=agent,
        name=_shortcut_session_name(resolved_cwd, agent),
        notify=None,
        cli_args=list(ctx.args),
    )


def _record_session_action(session: str, action: str, **fields: object) -> None:
    cwd, agent, _meta = resolve_session_context(session=session)
    if cwd is not None and agent is not None:
        append_action_history(session, cwd, agent, action, **fields)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(None, "--version", "-v", help="Show version and exit."),
) -> None:
    ensure_directories()
    if version:
        console.print(f"orche {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command("backend", hidden=True)
def backend() -> None:
    console.print(BACKEND)


@app.command("session-id", hidden=True)
def session_id() -> None:
    try:
        console.print(current_session_id())
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("whoami")
def whoami() -> None:
    session_id()


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


@app.command("open", context_settings=_OPEN_CONTEXT)
def open_session(
    ctx: typer.Context,
    agent: str = typer.Option(..., help=f"Agent name. Supported: {', '.join(supported_agent_names())}."),
    cwd: Optional[Path] = typer.Option(None, callback=_resolve_cwd, file_okay=False, dir_okay=True, resolve_path=False, help="Working directory for the session. Defaults to the current directory."),
    name: Optional[str] = typer.Option(None, "--name", help="Explicit session name. Defaults to <repo>-<agent>-<random>."),
    notify: Optional[str] = typer.Option(None, "--notify", help="Notify target in the form discord:<channel-id> or tmux:<session>."),
) -> None:
    try:
        resolved_cwd = _resolve_path(cwd or _default_cwd(), must_exist=True, require_dir=True)
        if resolved_cwd is None:
            raise OrcheError("Failed to resolve cwd")
        session, _pane_id = _open_session(
            cwd=resolved_cwd,
            agent=agent,
            name=name,
            notify=notify,
            cli_args=list(ctx.args),
        )
        _print_action_ok("open", session=session)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("attach")
def attach(
    session: str = typer.Argument(..., help="Session name."),
) -> None:
    try:
        target = attach_session(session)
        _record_session_action(session, "attach")
        _print_action_ok("attach", session=session, target=target)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("codex", context_settings=_OPEN_CONTEXT)
def codex_shortcut(ctx: typer.Context) -> None:
    try:
        session, pane_id = _open_shortcut_session(ctx, "codex")
        attach_session(session, pane_id=pane_id)
        _record_session_action(session, "attach")
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("claude", context_settings=_OPEN_CONTEXT)
def claude_shortcut(ctx: typer.Context) -> None:
    try:
        session, pane_id = _open_shortcut_session(ctx, "claude")
        attach_session(session, pane_id=pane_id)
        _record_session_action(session, "attach")
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("prompt")
def prompt(
    session: str = typer.Argument(..., help="Session name."),
    message: str = typer.Argument(..., help="Prompt text to send."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; open it first")
        send_prompt(session, cwd, agent, message)
        _print_action_ok("prompt", session=session)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("status")
def status(
    session: str = typer.Argument(..., help="Session name."),
) -> None:
    try:
        _render_status(build_status(session))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("read")
def read(
    session: str = typer.Argument(..., help="Session name."),
    lines: int = typer.Option(50, "--lines", min=1, help="Number of lines to read."),
) -> None:
    try:
        console.print(bridge_read(session, lines))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("input")
def input_text(
    session: str = typer.Argument(..., help="Session name."),
    text: str = typer.Argument(..., help="Text to type without pressing Enter."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; open it first")
        bridge_type(session, text)
        append_action_history(session, cwd, agent, "input", text=text)
        _print_action_ok("input", session=session, chars=len(text))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command(
    "key",
    help=(
        "Send one or more tmux key names to a session in order. "
        "Use `orche input` for literal text, and combine it with `orche key` "
        "for TUI interactions such as typing text and then sending `Enter`."
    ),
)
def key(
    session: str = typer.Argument(..., help="Session name."),
    keys: List[str] = typer.Argument(
        ...,
        help=(
            "One or more tmux key names to send in sequence, for example "
            "`Enter`, `C-c`, `Escape`, `Tab`, `Up`, `Down`, `Left`, `Right`."
        ),
    ),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; open it first")
        bridge_keys(session, keys)
        append_action_history(session, cwd, agent, "key", keys=list(keys))
        _print_action_ok("key", session=session, keys=",".join(keys))
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("list")
def list_command() -> None:
    sessions = list_sessions()
    if not sessions:
        console.print("No sessions found")
        return
    table = Table(title="orche sessions")
    table.add_column("Session", style="cyan")
    table.add_column("Agent", style="white")
    table.add_column("CWD", style="white")
    table.add_column("Pane", style="white")
    for entry in sessions:
        table.add_row(
            str(entry.get("session") or "-"),
            str(entry.get("agent") or "-"),
            str(entry.get("cwd") or "-"),
            str(entry.get("pane_id") or "-"),
        )
    console.print(table)


@app.command("cancel")
def cancel(
    session: str = typer.Argument(..., help="Session name."),
) -> None:
    try:
        cwd, agent, _meta = resolve_session_context(session=session, require_cwd_agent=True)
        if cwd is None or agent is None:
            raise OrcheError(f"Session {session} is missing cwd/agent context; open it first")
        pane_id = cancel_session(session)
        append_action_history(session, cwd, agent, "cancel", pane_id=pane_id)
        _print_action_ok("cancel", session=session, pane=pane_id)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("close")
def close(
    session: Optional[str] = typer.Argument(None, help="Session name."),
    all_sessions: bool = typer.Option(False, "--all", help="Close all sessions."),
) -> None:
    try:
        if all_sessions:
            if session:
                raise OrcheError("close does not accept a session argument with --all")
            sessions = [str(entry.get("session") or "").strip() for entry in list_sessions()]
            sessions = [name for name in sessions if name]
            if not sessions:
                console.print("No sessions found")
                return
            closed: list[str] = []
            failures: list[str] = []
            for name in sessions:
                try:
                    cwd, agent, _meta = resolve_session_context(session=name)
                    pane_id = close_session(name)
                    if cwd is not None and agent is not None:
                        append_action_history(name, cwd, agent, "close", pane_id=pane_id)
                    closed.append(f"close ok: session={name} pane={pane_id}")
                except (OrcheError, subprocess.CalledProcessError) as exc:
                    failures.append(f"{name}: {_format_error_detail(exc)}")
            for line in closed:
                console.print(line)
            if failures:
                raise OrcheError("Failed to close some sessions: " + "; ".join(failures))
            return

        if not session:
            raise OrcheError("Session name is required unless you pass --all")
        cwd, agent, _meta = resolve_session_context(session=session)
        pane_id = close_session(session)
        if cwd is not None and agent is not None:
            append_action_history(session, cwd, agent, "close", pane_id=pane_id)
        _print_action_ok("close", session=session, pane=pane_id)
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


@app.command("turn-summary", hidden=True)
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


@app.command("notify-internal", hidden=True)
def notify_internal_command(
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
        runtime_config = _notify_runtime_config(runtime_config, resolved_session)
        notify_config = load_notify_config(runtime_config)
        event = build_message_from_payload(
            payload_text or "",
            notify_config=notify_config,
            runtime_config=runtime_config,
            summary_loader=latest_turn_summary,
            explicit_session=resolved_session,
            status=status,
        )
        routes = []
        if event is not None:
            routes = list(
                resolve_routes(
                    event=event,
                    runtime_config=runtime_config,
                    notify_config=notify_config,
                    explicit_channel_id=resolved_channel_id,
                )
            )
        if verbose:
            _print_notify_verbose(
                runtime_config=runtime_config,
                notify_config=notify_config,
                session=resolved_session,
                channel_id=resolved_channel_id,
                payload_text=payload_text or "",
                event=event,
                routes=routes,
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
        if event is None:
            console.print("notify skipped: payload/config did not produce a notify event")
            log_event(
                "notify.skipped",
                reason="event-none",
                session=resolved_session,
                channel_id=resolved_channel_id,
            )
            return
        event_source = str(event.metadata.get("source") or "hook")
        bypass_dedup = event.event == "completed" and event_source == "hook"
        if not bypass_dedup and not claim_turn_notification(
            event.session,
            event.event,
            turn_id=str(event.metadata.get("turn_id") or ""),
            prompt=str(event.metadata.get("input_message") or ""),
            source=event_source,
            status=event.status,
            summary=event.summary,
            notification_key=str(event.metadata.get("notification_key") or ""),
        ):
            console.print(f"notify skipped: duplicate {event.event} event")
            log_event(
                "notify.skipped",
                reason="duplicate",
                session=resolved_session or event.session,
                notify_event=event.event,
                source=event_source,
                turn_id=str(event.metadata.get("turn_id") or ""),
            )
            return
        if event.event == "completed":
            complete_pending_turn(
                event.session,
                summary=event.summary,
                turn_id=str(event.metadata.get("turn_id") or ""),
                prompt=str(event.metadata.get("input_message") or ""),
            )
        service = NotificationService()
        results = dispatch_event(
            event,
            runtime_config=runtime_config,
            notify_config=notify_config,
            routes=routes,
            service=service,
        )
        if not results:
            if bypass_dedup:
                console.print("notify skipped: no notifier routes resolved")
                log_event(
                    "notify.skipped",
                    reason="no-routes",
                    session=resolved_session or event.session,
                    channel_id=resolved_channel_id,
                    source=event_source,
                    turn_id=str(event.metadata.get("turn_id") or ""),
                )
                return
            release_turn_notification(
                event.session,
                event.event,
                turn_id=str(event.metadata.get("turn_id") or ""),
                prompt=str(event.metadata.get("input_message") or ""),
                notification_key=str(event.metadata.get("notification_key") or ""),
            )
            console.print("notify skipped: no notifier routes resolved")
            log_event(
                "notify.skipped",
                reason="no-routes",
                session=resolved_session or event.session,
                channel_id=resolved_channel_id,
            )
            return
        has_failure = False
        has_success = False
        for result in results:
            state = "ok" if result.ok else "failed"
            detail = result.detail or "-"
            line = f"notify {state}: provider={result.provider} detail={detail}"
            if result.ok:
                console.print(line)
                has_success = True
            else:
                stderr.print(line)
                has_failure = True
            log_event(
                "notify.delivery",
                provider=result.provider,
                ok=result.ok,
                detail=result.detail,
                session=resolved_session or event.session,
                channel_id=result.target or resolved_channel_id,
                source=event_source,
                turn_id=str(event.metadata.get("turn_id") or ""),
            )
        if has_failure and not has_success and not bypass_dedup:
            release_turn_notification(
                event.session,
                event.event,
                turn_id=str(event.metadata.get("turn_id") or ""),
                prompt=str(event.metadata.get("input_message") or ""),
                notification_key=str(event.metadata.get("notification_key") or ""),
            )
        if has_failure:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:  # pragma: no cover
        stderr.print(f"notify error: {exc}")
        log_exception("notify.error", exc)
        raise typer.Exit(code=1)


@app.command("watchdog-loop-internal", hidden=True)
def watchdog_loop_internal_command(
    session: str = typer.Option(..., "--session", help="Session name."),
    turn_id: str = typer.Option(..., "--turn-id", help="Turn id to watch."),
) -> None:
    try:
        run_session_watchdog(session, turn_id=turn_id)
    except Exception as exc:  # pragma: no cover
        log_exception("watchdog.loop.error", exc, session=session, turn_id=turn_id)
        raise typer.Exit(code=1)


@app.command("_notify-discord", hidden=True)
def notify_discord_hidden(
    payload: Optional[str] = typer.Argument(None, help="Optional JSON payload. If omitted, stdin is used."),
    session: Optional[str] = typer.Option(None, "--session", help="Explicit session override."),
    channel_id: Optional[str] = typer.Option(None, "--channel-id", help="Explicit Discord channel override."),
    status: str = typer.Option("success", "--status", help="Delivery status label."),
    verbose: bool = typer.Option(False, "--verbose", help="Print config, payload, and delivery details."),
) -> None:
    notify_internal_command(
        payload=payload,
        session=session,
        channel_id=channel_id,
        status=status,
        verbose=verbose,
    )


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


@app.command("clearall", hidden=True)
def clearall() -> None:
    sessions = list_sessions()
    if not sessions:
        console.print("No sessions found")
        return
    cleared = 0
    for entry in sessions:
        session = str(entry.get("session") or "").strip()
        if not session:
            continue
        close_session(session)
        cleared += 1
    console.print(f"Cleared {cleared} session(s)")


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


if __name__ == "__main__":
    raise SystemExit(main())
