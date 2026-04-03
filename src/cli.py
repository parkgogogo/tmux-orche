from __future__ import annotations

import json
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
    attach_session,
    append_action_history,
    bridge_keys,
    bridge_read,
    bridge_type,
    build_status,
    cancel_session,
    release_turn_notification,
    close_session,
    current_session_id,
    default_session_name,
    ensure_native_session,
    ensure_session,
    get_config_value,
    load_config,
    load_history_entries,
    list_sessions,
    list_config_values,
    latest_turn_summary,
    log_event,
    log_exception,
    claim_turn_notification,
    resolve_session_context,
    run_session_watchdog,
    send_prompt,
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

app = typer.Typer(
    name="orche",
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Modern CLI for tmux-backed agent orchestration with tmux-bridge.",
    add_completion=False,
)
config_app = typer.Typer(help="Manage orche runtime configuration.")
sessions_app = typer.Typer(help="Manage stored sessions.")
app.add_typer(config_app, name="config")
app.add_typer(sessions_app, name="sessions")
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


def _start_agent_session(
    *,
    cwd: Path,
    agent: str,
    name: Optional[str],
    runtime_home: Optional[Path],
    notify_to: Optional[str],
    notify_target: Optional[str],
) -> None:
    if not str(notify_to or "").strip() or not str(notify_target or "").strip():
        raise OrcheError("session-new requires both --notify-to and --notify-target")
    session = _session_name(name, cwd, agent)
    pane_id = ensure_session(
        session,
        cwd.resolve(),
        agent,
        runtime_home=None if runtime_home is None else str(runtime_home),
        notify_to=notify_to,
        notify_target=notify_target,
    )
    append_action_history(session, cwd.resolve(), agent, "session-new", pane_id=pane_id)
    console.print(session)


def _default_cwd() -> Path:
    return Path.cwd().resolve()


def _open_agent_session(
    *,
    cwd: Optional[Path],
    agent: str,
    session_name: Optional[str],
    cli_args: list[str],
) -> None:
    resolved_cwd = _resolve_path(cwd or _default_cwd(), must_exist=True, require_dir=True)
    if resolved_cwd is None:
        raise OrcheError("Failed to resolve cwd")
    session = _session_name(session_name, resolved_cwd, agent)
    pane_id = ensure_native_session(
        session,
        resolved_cwd,
        agent,
        cli_args=cli_args,
    )
    append_action_history(session, resolved_cwd, agent, "attach", pane_id=pane_id)
    attach_session(session, pane_id=pane_id)


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
    if info.get("runtime_home"):
        body.append(f"\n{info.get('runtime_label') or 'Runtime'}: ", style="bold cyan")
        body.append(str(info["runtime_home"]))
        body.append("\nManaged: ", style="bold cyan")
        body.append("yes" if info.get("runtime_home_managed") else "no")
    if info.get("discord_session"):
        body.append("\nDiscord session: ", style="bold cyan")
        body.append(str(info["discord_session"]))
    notify_binding = info.get("notify_binding")
    if isinstance(notify_binding, dict) and notify_binding:
        body.append("\nNotify binding: ", style="bold cyan")
        body.append(json.dumps(notify_binding, ensure_ascii=False))
    if info.get("pending_turn_id"):
        body.append("\nPending turn: ", style="bold cyan")
        body.append(str(info["pending_turn_id"]))
    watchdog = info.get("watchdog")
    if isinstance(watchdog, dict) and watchdog:
        body.append("\nWatchdog: ", style="bold cyan")
        body.append(str(watchdog.get("state") or "-"))
        pid = str(watchdog.get("pid") or "").strip()
        if pid:
            body.append(f" (pid={pid})")
        last_event = str(watchdog.get("last_event") or "").strip()
        if last_event:
            body.append("\nWatchdog event: ", style="bold cyan")
            body.append(last_event)
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


@app.command("session-id")
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


@app.command("session-new")
def session_new(
    cwd: Path = typer.Option(..., callback=_resolve_cwd, file_okay=False, dir_okay=True, resolve_path=False, help="Working directory for the agent session."),
    agent: str = typer.Option(..., help=f"Agent name. Supported: {', '.join(supported_agent_names())}."),
    name: Optional[str] = typer.Option(None, "--name", help="Explicit session name. Defaults to <repo>-<agent>-main."),
    runtime_home: Optional[Path] = typer.Option(None, "--runtime-home", "--codex-home", callback=_resolve_optional_path, resolve_path=False, help="Optional manual agent runtime home override. If omitted, orche manages a per-session runtime directory when the agent supports it."),
    notify_to: Optional[str] = typer.Option(None, "--notify-to", help="Single notify provider to bind to this session."),
    notify_target: Optional[str] = typer.Option(None, "--notify-target", help="Provider-specific notify target value."),
) -> None:
    try:
        _start_agent_session(
            cwd=cwd,
            agent=agent,
            name=name,
            runtime_home=runtime_home,
            notify_to=notify_to,
            notify_target=notify_target,
        )
    except (OrcheError, subprocess.CalledProcessError) as exc:
        _handle_error(exc)


def _agent_session_command(agent: str):
    def command(
        ctx: typer.Context,
        cwd: Optional[Path] = typer.Option(None, callback=_resolve_cwd, file_okay=False, dir_okay=True, resolve_path=False, help=f"Working directory for the {agent} session. Defaults to the current directory."),
        session_name: Optional[str] = typer.Option(None, "--session-name", help="Optional tmux session name override. Defaults to <repo>-<agent>-main."),
    ) -> None:
        try:
            _open_agent_session(
                cwd=cwd,
                agent=agent,
                session_name=session_name,
                cli_args=list(ctx.args),
            )
        except (OrcheError, subprocess.CalledProcessError) as exc:
            _handle_error(exc)

    return command


_NATIVE_SHORTCUT_CONTEXT = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
}


app.command(
    "codex",
    help="Run native Codex inside a named tmux session.",
    context_settings=_NATIVE_SHORTCUT_CONTEXT,
    add_help_option=False,
)(_agent_session_command("codex"))
app.command(
    "claude",
    help="Run native Claude Code inside a named tmux session.",
    context_settings=_NATIVE_SHORTCUT_CONTEXT,
    add_help_option=False,
)(_agent_session_command("claude"))
app.command(
    "cc",
    help="Alias for claude.",
    context_settings=_NATIVE_SHORTCUT_CONTEXT,
    add_help_option=False,
)(_agent_session_command("claude"))


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
        if not claim_turn_notification(
            event.session,
            event.event,
            turn_id=str(event.metadata.get("turn_id") or ""),
            source=str(event.metadata.get("source") or "hook"),
            status=event.status,
            summary=event.summary,
        ):
            console.print(f"notify skipped: duplicate {event.event} event")
            log_event(
                "notify.skipped",
                reason="duplicate",
                session=resolved_session or event.session,
                notify_event=event.event,
            )
            return
        service = NotificationService()
        results = dispatch_event(
            event,
            runtime_config=runtime_config,
            notify_config=notify_config,
            routes=routes,
            service=service,
        )
        if not results:
            release_turn_notification(
                event.session,
                event.event,
                turn_id=str(event.metadata.get("turn_id") or ""),
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
            )
        if has_failure and not has_success:
            release_turn_notification(
                event.session,
                event.event,
                turn_id=str(event.metadata.get("turn_id") or ""),
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


@sessions_app.command("list")
def sessions_list() -> None:
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


@sessions_app.command("clearall")
def sessions_clearall() -> None:
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


def _notify_internal_entry(
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


def _watchdog_loop_internal_entry(
    session: str = typer.Option(..., "--session", help="Session name."),
    turn_id: str = typer.Option(..., "--turn-id", help="Turn id to watch."),
) -> None:
    watchdog_loop_internal_command(session=session, turn_id=turn_id)


app.command("notify-internal", hidden=True)(_notify_internal_entry)
app.command("watchdog-loop-internal", hidden=True)(_watchdog_loop_internal_entry)


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
