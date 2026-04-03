from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

import agents.claude as claude_agent_module
import agents.codex as codex_agent_module
from agents import AgentPlugin, AgentRuntime, get_agent_plugin, supported_agents
from agents.claude import ClaudeAgent, default_claude_home_path
from agents.codex import CodexAgent, SOURCE_CONFIG_BACKUP_SUFFIX, default_codex_home_path
from agents.common import (
    normalize_runtime_home,
    remove_runtime_home,
    validate_discord_channel_id as common_validate_discord_channel_id,
    write_text_atomically,
)
from paths import config_path, ensure_directories, history_dir, locks_dir, meta_dir, orch_log_path

BACKEND = "smux"
TMUX_SESSION = "orche-smux"
DEFAULT_CAPTURE_LINES = 200
STARTUP_TIMEOUT = 90.0
CONFIG_COMMENT = (
    "orche runtime config. session is the active orche agent session label; "
    "discord_session is the Discord/OpenClaw session key used for notify routing."
)
TMUX_BRIDGE_FALLBACK = Path.home() / ".smux" / "bin" / "tmux-bridge"
DEFAULT_CODEX_HOME_ROOT = codex_agent_module.DEFAULT_RUNTIME_HOME_ROOT
DEFAULT_CODEX_SOURCE_HOME = codex_agent_module.DEFAULT_CODEX_SOURCE_HOME
SUPPORTED_NOTIFY_PROVIDERS = ("discord", "tmux-bridge")
CONFIG_KEY_MAP = {
    "discord.bot-token": "discord_bot_token",
    "discord.mention-user-id": "notify_mention_user_id",
    "discord.webhook-url": "discord_webhook_url",
    "notify.enabled": "notify_enabled",
}


class OrcheError(RuntimeError):
    pass


def shorten(text: object, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def longest_common_prefix(before: str, after: str) -> int:
    limit = min(len(before), len(after))
    index = 0
    while index < limit and before[index] == after[index]:
        index += 1
    return index


def turn_delta(before: str, after: str) -> str:
    if before and after and before in after:
        return after.split(before, 1)[1]
    return after[longest_common_prefix(before, after) :]


def extract_summary_candidate(text: str, *, prompt: str = "") -> str:
    lines: List[str] = []
    prompt_inline = compact_text(prompt)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```"):
            continue
        if line.startswith(("╭", "╰", "│", "› ", "└ ")):
            continue
        if line.startswith("• "):
            line = line[2:].strip()
            if not line:
                continue
        if re.match(r"^[─━]{6,}$", line):
            continue
        if re.match(r"^[\W_─━]{20,}$", line):
            continue
        if line.startswith(("Tip:", "Command:", "Chunk ID:", "Wall time:", "Output:")):
            continue
        if line in {"Explored", "Ran", "Read", "List", "Updated Plan"}:
            continue
        if line.startswith(("Explored", "Ran ", "Read ", "List ", "Edited ")):
            continue
        if line.startswith(("OpenAI Codex", "dnq@", "^C")):
            continue
        if "gpt-" in line and "% left" in line:
            continue
        if line.startswith(("session:", "cwd:")):
            continue
        if prompt_inline and compact_text(line) == prompt_inline:
            continue
        if prompt_inline and compact_text(line).endswith(prompt_inline):
            continue
        line = compact_text(line.replace("`", ""))
        if not line:
            continue
        lines.append(line)
    return lines[-1] if lines else ""


def log_event(event: str, **fields: Any) -> None:
    ensure_directories()
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        with orch_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_exception(event: str, exc: BaseException, **fields: Any) -> None:
    log_event(
        event,
        error_type=type(exc).__name__,
        error=str(exc),
        traceback=traceback.format_exc(),
        **fields,
    )


def slugify(text: str) -> str:
    out: List[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in ("-", "_", "/", "."):
            out.append("-")
    value = "".join(out)
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip("-") or "root"


def repo_name(cwd: Path) -> str:
    return slugify(cwd.resolve().name)


def normalize_codex_home(codex_home: Optional[Union[Path, str]]) -> str:
    return normalize_runtime_home(codex_home)


def default_session_name(cwd: Path, agent: str, purpose: str = "main") -> str:
    return f"{repo_name(cwd)}-{slugify(agent)}-{slugify(purpose)}"


def window_name(session: str) -> str:
    return f"orche-{slugify(session)}"


def session_key(session: str) -> str:
    return slugify(session)


def history_path(session: str) -> Path:
    return history_dir() / f"{session_key(session)}.jsonl"


def meta_path(session: str) -> Path:
    return meta_dir() / f"{session_key(session)}.json"


def lock_path(session: str) -> Path:
    return locks_dir() / f"{session_key(session)}.lock"


def notify_target_lock_path(session: str) -> Path:
    return locks_dir() / f"{session_key(session)}.notify.lock"


def run(
    cmd: List[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        check=check,
        capture_output=capture,
        cwd=None if cwd is None else str(cwd),
        env=env,
    )


def require_tmux() -> None:
    if shutil.which("tmux"):
        return
    raise OrcheError("tmux is not installed; smux backend requires tmux")


def require_tmux_bridge() -> str:
    candidate = shutil.which("tmux-bridge")
    if candidate:
        return candidate
    if TMUX_BRIDGE_FALLBACK.exists():
        return str(TMUX_BRIDGE_FALLBACK)
    raise OrcheError("tmux-bridge is not installed; run the official smux installer first")


def tmux(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    require_tmux()
    return run(["tmux", *args], check=check, capture=capture)


def tmux_bridge(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    bridge = require_tmux_bridge()
    return run([bridge, *args], check=check, capture=capture)


def pane_exists(pane_id: str) -> bool:
    result = tmux("display-message", "-p", "-t", pane_id, "#{pane_id}", check=False, capture=True)
    return result.returncode == 0 and result.stdout.strip() == pane_id


def tmux_session_exists() -> bool:
    result = tmux("has-session", "-t", TMUX_SESSION, check=False, capture=True)
    return result.returncode == 0


def list_windows() -> List[Dict[str, str]]:
    if not tmux_session_exists():
        return []
    result = tmux(
        "list-windows",
        "-t",
        TMUX_SESSION,
        "-F",
        "#{window_id}\t#{window_name}",
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return []
    windows: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            windows.append({"window_id": parts[0], "window_name": parts[1]})
    return windows


def find_window(name: str) -> Optional[Dict[str, str]]:
    for window in list_windows():
        if window["window_name"] == name:
            return window
    return None


def list_panes(target: Optional[str] = None) -> List[Dict[str, str]]:
    args = ["list-panes"]
    if target:
        args.extend(["-t", target])
    else:
        if not tmux_session_exists():
            return []
        args.extend(["-t", TMUX_SESSION])
    args.extend(
        [
            "-F",
            "#{pane_id}\t#{window_id}\t#{window_name}\t#{pane_dead}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}",
        ]
    )
    result = tmux(*args, check=False, capture=True)
    if result.returncode != 0:
        return []
    panes: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8:
            continue
        panes.append(
            {
                "pane_id": parts[0],
                "window_id": parts[1],
                "window_name": parts[2],
                "pane_dead": parts[3],
                "pane_pid": parts[4],
                "pane_current_command": parts[5],
                "pane_current_path": parts[6],
                "pane_title": parts[7],
            }
        )
    return panes


def get_pane_info(pane_id: str) -> Optional[Dict[str, str]]:
    if not pane_exists(pane_id):
        return None
    panes = list_panes(pane_id)
    return panes[0] if panes else None


def read_pane(pane_id: str, lines: int = DEFAULT_CAPTURE_LINES) -> str:
    start = f"-{max(lines, 1)}"
    result = tmux("capture-pane", "-p", "-J", "-t", pane_id, "-S", start, check=False, capture=True)
    if result.returncode != 0:
        return ""
    return "\n".join(result.stdout.splitlines()[-lines:])


def save_meta(session: str, meta: Dict[str, Any]) -> None:
    ensure_directories()
    meta_path(session).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_meta(session: str) -> Dict[str, Any]:
    path = meta_path(session)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def validate_discord_channel_id(value: str, *, option_name: str = "--channel-id") -> str:
    try:
        return common_validate_discord_channel_id(value)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("--discord-channel-id"):
            message = message.replace("--discord-channel-id", option_name, 1)
        raise OrcheError(message) from exc


def validate_notify_provider(value: str, *, option_name: str = "--notify-to") -> str:
    provider = str(value or "").strip()
    if not provider:
        raise OrcheError(f"{option_name} is required")
    if provider not in SUPPORTED_NOTIFY_PROVIDERS:
        supported = ", ".join(SUPPORTED_NOTIFY_PROVIDERS)
        raise OrcheError(f"{option_name} must be one of: {supported}")
    return provider


def _read_notify_binding(payload: Mapping[str, Any]) -> Dict[str, str]:
    binding = payload.get("notify_binding")
    if isinstance(binding, Mapping):
        provider = str(binding.get("provider") or "").strip()
        target = str(binding.get("target") or "").strip()
        if provider == "discord" and target.isdigit():
            return {
                "provider": "discord",
                "target": target,
                "session": str(binding.get("session") or derive_discord_session(target)).strip(),
            }
        if provider == "tmux-bridge" and target:
            return {
                "provider": "tmux-bridge",
                "target": target,
            }
    legacy_routes = payload.get("notify_routes")
    if isinstance(legacy_routes, Mapping):
        discord_route = legacy_routes.get("discord")
        if isinstance(discord_route, Mapping):
            target = str(discord_route.get("channel_id") or "").strip()
            if target.isdigit():
                return {
                    "provider": "discord",
                    "target": target,
                    "session": str(discord_route.get("session") or derive_discord_session(target)).strip(),
                }
        tmux_route = legacy_routes.get("tmux-bridge")
        if isinstance(tmux_route, Mapping):
            target = str(tmux_route.get("target_session") or tmux_route.get("target") or "").strip()
            if target:
                return {
                    "provider": "tmux-bridge",
                    "target": target,
                }
    discord_channel_id = str(payload.get("discord_channel_id") or "").strip()
    if discord_channel_id.isdigit():
        return {
            "provider": "discord",
            "target": discord_channel_id,
            "session": str(payload.get("discord_session") or derive_discord_session(discord_channel_id)).strip(),
        }
    return {}


def build_notify_binding(provider: str, target: str) -> Dict[str, str]:
    normalized_provider = validate_notify_provider(provider)
    normalized_target = str(target or "").strip()
    if normalized_provider == "discord":
        channel_id = validate_discord_channel_id(normalized_target, option_name="--notify-target")
        return {
            "provider": "discord",
            "target": channel_id,
            "session": derive_discord_session(channel_id),
        }
    if not normalized_target:
        raise OrcheError("--notify-target is required for --notify-to tmux-bridge")
    return {
        "provider": "tmux-bridge",
        "target": normalized_target,
    }


def remove_meta(session: str) -> None:
    path = meta_path(session)
    if path.exists():
        path.unlink()


def append_history_entry(session: str, entry: Dict[str, Any]) -> None:
    ensure_directories()
    path = history_path(session)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history_entries(session: str) -> List[Dict[str, Any]]:
    path = history_path(session)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def list_sessions() -> List[Dict[str, Any]]:
    ensure_directories()
    sessions: List[Dict[str, Any]] = []
    for path in sorted(meta_dir().glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        session = str(payload.get("session") or path.stem).strip()
        if not session:
            continue
        payload["session"] = session
        sessions.append(payload)
    sessions.sort(key=lambda item: str(item.get("session") or ""))
    return sessions


def _current_tmux_value(fmt: str) -> str:
    result = tmux("display-message", "-p", fmt, check=False, capture=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def load_config() -> Dict[str, Any]:
    ensure_directories()
    default = {
        "_comment": CONFIG_COMMENT,
        "codex_turn_complete_channel_id": "",
        "discord_bot_token": "",
        "discord_channel_id": "",
        "discord_webhook_url": "",
        "notify_enabled": True,
        "session": "",
        "discord_session": "",
        "runtime_home": "",
        "runtime_home_managed": False,
        "runtime_label": "",
        "codex_home": "",
        "codex_home_managed": False,
    }
    path = config_path()
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
    if not isinstance(data, dict):
        return default
    merged = dict(default)
    merged.update(data)
    return merged


def save_config(config: Dict[str, Any]) -> None:
    ensure_directories()
    payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    write_text_atomically(config_path(), payload)


def derive_discord_session(channel_id: str) -> str:
    return f"agent:main:discord:channel:{channel_id}"


def config_key_field(key: str) -> str:
    field = CONFIG_KEY_MAP.get(key)
    if field is None:
        supported = ", ".join(sorted(CONFIG_KEY_MAP))
        raise OrcheError(f"Unsupported config key: {key}. Supported keys: {supported}")
    return field


def get_config_value(key: str) -> str:
    config = load_config()
    field = config_key_field(key)
    value = config.get(field)
    if key == "notify.enabled":
        return "true" if bool(value) else "false"
    return "" if value is None else str(value)


def set_config_value(key: str, value: str) -> Dict[str, Any]:
    config = load_config()
    field = config_key_field(key)
    normalized = value
    if key == "notify.enabled":
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            normalized = True
        elif lowered in {"0", "false", "no", "off"}:
            normalized = False
        else:
            raise OrcheError("notify.enabled must be one of: true, false, 1, 0, yes, no, on, off")
    else:
        normalized = value.strip()
    config[field] = normalized
    save_config(config)
    return config


def list_config_values() -> Dict[str, str]:
    return {key: get_config_value(key) for key in sorted(CONFIG_KEY_MAP)}


def update_runtime_config(
    *,
    session: str,
    cwd: Path,
    agent: str,
    pane_id: str,
    runtime_home: Optional[str] = None,
    runtime_home_managed: Optional[bool] = None,
    runtime_label: str = "",
) -> Dict[str, Any]:
    config = load_config()
    config["_comment"] = CONFIG_COMMENT
    config.pop("orch_session", None)
    config.pop("parent_session_key", None)
    config["session"] = session
    config["cwd"] = str(cwd)
    config["agent"] = agent
    config["pane_id"] = pane_id
    normalized_runtime_home = normalize_runtime_home(runtime_home)
    config["runtime_home"] = normalized_runtime_home
    if runtime_home_managed is not None:
        config["runtime_home_managed"] = bool(runtime_home_managed)
    config["runtime_label"] = runtime_label
    if agent == "codex":
        config["codex_home"] = normalized_runtime_home
        config["codex_home_managed"] = bool(runtime_home_managed)
    else:
        config["codex_home"] = ""
        config["codex_home_managed"] = False
    config["updated_at"] = time.time()
    save_config(config)
    return config


@contextlib.contextmanager
def session_lock(session: str, *, timeout: float = 5.0):
    ensure_directories()
    path = lock_path(session)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise OrcheError(f"Timed out waiting for session lock: {session}")
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def target_session_io_lock(session: str, *, timeout: float = 5.0):
    ensure_directories()
    path = notify_target_lock_path(session)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise OrcheError(f"Timed out waiting for notify target lock: {session}")
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def bridge_name_pane(pane_id: str, session: str) -> None:
    tmux_bridge("name", pane_id, session, check=True, capture=True)


def bridge_resolve(session: str) -> Optional[str]:
    result = tmux_bridge("resolve", session, check=False, capture=True)
    if result.returncode != 0:
        return None
    pane_id = result.stdout.strip()
    return pane_id or None


def bridge_read(session: str, lines: int = DEFAULT_CAPTURE_LINES) -> str:
    result = tmux_bridge("read", session, str(lines), check=True, capture=True)
    return result.stdout.rstrip("\n")


def bridge_type(session: str, text: str) -> None:
    if not text:
        return
    tmux_bridge("read", session, "1", check=True, capture=True)
    tmux_bridge("type", session, text, check=True, capture=True)


def bridge_keys(session: str, keys: Union[Iterable[str], str]) -> None:
    values = [keys] if isinstance(keys, str) else list(keys)
    if not values:
        return
    tmux_bridge("read", session, "1", check=True, capture=True)
    tmux_bridge("keys", session, *values, check=True, capture=True)


def attach_session(session: str, *, pane_id: str = "") -> str:
    resolved_pane_id = pane_id or bridge_resolve(session) or str(load_meta(session).get("pane_id") or "")
    if not resolved_pane_id:
        raise OrcheError(f"Unknown session: {session}")
    info = get_pane_info(resolved_pane_id)
    if info is None:
        raise OrcheError(f"Pane disappeared before attach: {resolved_pane_id}")
    window_id = str(info.get("window_id") or "").strip()
    if not window_id:
        raise OrcheError(f"Window not found for session: {session}")
    tmux("select-window", "-t", window_id, check=True, capture=False)
    if os.environ.get("TMUX"):
        tmux("switch-client", "-t", TMUX_SESSION, check=True, capture=False)
    else:
        tmux("attach-session", "-t", TMUX_SESSION, check=True, capture=False)
    return window_id


def deliver_notify_to_session(session: str, prompt: str) -> str:
    target_session = session.strip()
    if not target_session:
        raise OrcheError("notify target session is required")
    if not prompt:
        raise OrcheError("notify prompt is required")
    with target_session_io_lock(target_session):
        pane_id = bridge_resolve(target_session)
        if not pane_id:
            raise OrcheError(f"notify target session not found: {target_session}")
        bridge_type(target_session, prompt)
        bridge_keys(target_session, ["Enter"])
        return pane_id


class _BridgeAdapter:
    def type(self, session: str, text: str) -> None:
        bridge_type(session, text)

    def keys(self, session: str, keys: Sequence[str]) -> None:
        bridge_keys(session, list(keys))


BRIDGE = _BridgeAdapter()


def supported_agent_names() -> Tuple[str, ...]:
    return supported_agents()


def get_agent(name: str) -> AgentPlugin:
    try:
        return get_agent_plugin(name)
    except ValueError as exc:
        raise OrcheError(str(exc)) from exc


def prepare_managed_runtime(
    plugin: AgentPlugin,
    session: str,
    *,
    cwd: Path,
    discord_channel_id: Optional[str],
) -> AgentRuntime:
    try:
        if plugin.name == "codex":
            codex_agent_module.DEFAULT_RUNTIME_HOME_ROOT = DEFAULT_CODEX_HOME_ROOT
            codex_agent_module.DEFAULT_CODEX_SOURCE_HOME = DEFAULT_CODEX_SOURCE_HOME
        elif plugin.name == "claude":
            claude_agent_module.DEFAULT_RUNTIME_HOME_ROOT = default_claude_home_path(session).parent
        return plugin.ensure_managed_runtime(
            session,
            cwd=cwd,
            discord_channel_id=discord_channel_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise OrcheError(str(exc)) from exc


def runtime_home_from_meta(meta: Dict[str, Any]) -> str:
    return normalize_runtime_home(meta.get("runtime_home") or meta.get("codex_home") or "")


def runtime_home_managed_from_meta(meta: Dict[str, Any]) -> bool:
    if "runtime_home_managed" in meta:
        return bool(meta.get("runtime_home_managed"))
    return bool(meta.get("codex_home_managed"))


def runtime_label_from_meta(meta: Dict[str, Any], plugin: AgentPlugin) -> str:
    return str(meta.get("runtime_label") or plugin.runtime_label)


def apply_runtime_to_meta(meta: Dict[str, Any], *, agent: str, runtime: AgentRuntime) -> None:
    meta["runtime_home"] = normalize_runtime_home(runtime.home)
    meta["runtime_home_managed"] = bool(runtime.managed)
    meta["runtime_label"] = runtime.label
    if agent == "codex":
        meta["codex_home"] = meta["runtime_home"]
        meta["codex_home_managed"] = meta["runtime_home_managed"]
    else:
        meta["codex_home"] = ""
        meta["codex_home_managed"] = False


def ensure_window(name: str, cwd: Path) -> Dict[str, str]:
    window = find_window(name)
    if window is not None:
        return window
    if tmux_session_exists():
        tmux("new-window", "-d", "-t", TMUX_SESSION, "-n", name, "-c", str(cwd), check=True, capture=True)
    else:
        tmux("new-session", "-d", "-s", TMUX_SESSION, "-n", name, "-c", str(cwd), check=True, capture=True)
    created = find_window(name)
    if created is None:
        raise OrcheError(f"Failed to create tmux window for {name}")
    return created


def normalize_pane(session: str, cwd: Path, pane: Dict[str, str]) -> str:
    pane_id = pane["pane_id"]
    if pane.get("pane_dead") == "1":
        tmux("respawn-pane", "-k", "-t", pane_id, "-c", str(cwd), check=True, capture=True)
    tmux("select-pane", "-t", pane_id, "-T", session, check=False, capture=True)
    bridge_name_pane(pane_id, session)
    return pane_id


def ensure_pane(session: str, cwd: Path, agent: str) -> str:
    cwd = cwd.resolve()
    with session_lock(session):
        meta = load_meta(session)
        pane_id = str(meta.get("pane_id") or "")
        if pane_id and pane_exists(pane_id):
            info = get_pane_info(pane_id)
            if info is not None:
                pane_id = normalize_pane(session, cwd, info)
                meta.update(
                    {
                        "backend": BACKEND,
                        "session": session,
                        "cwd": str(cwd),
                        "agent": agent,
                        "pane_id": pane_id,
                        "window_id": info["window_id"],
                        "window_name": info["window_name"],
                        "last_seen_at": time.time(),
                    }
                )
                save_meta(session, meta)
                return pane_id

        window = ensure_window(window_name(session), cwd)
        panes = list_panes(window["window_id"])
        if not panes:
            tmux("split-window", "-d", "-t", window["window_id"], "-c", str(cwd), check=True, capture=True)
            panes = list_panes(window["window_id"])
        if not panes:
            raise OrcheError(f"Failed to create tmux pane for {session}")
        pane = panes[0]
        pane_id = normalize_pane(session, cwd, pane)
        meta.update(
            {
                "backend": BACKEND,
                "session": session,
                "cwd": str(cwd),
                "agent": agent,
                "pane_id": pane_id,
                "window_id": pane["window_id"],
                "window_name": pane["window_name"],
                "last_seen_at": time.time(),
            }
        )
        save_meta(session, meta)
        return pane_id


def process_descendants(root_pid: int) -> List[str]:
    result = run(["ps", "-axo", "pid=,ppid=,command="], check=False, capture=True)
    if result.returncode != 0:
        return []
    children: Dict[int, List[Tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, parts[2]))
    commands: List[str] = []
    stack = [root_pid]
    seen: Set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for child_pid, command in children.get(current, []):
            commands.append(command)
            stack.append(child_pid)
    return commands


def is_agent_running(plugin: AgentPlugin, pane_id: str) -> bool:
    info = get_pane_info(pane_id)
    if info is None or info.get("pane_dead") == "1":
        return False
    command = (info.get("pane_current_command") or "").lower()
    try:
        pane_pid = int(info.get("pane_pid") or "0")
    except ValueError:
        return False
    return plugin.matches_process(command, process_descendants(pane_pid))


def wait_for_agent_ready(plugin: AgentPlugin, pane_id: str, cwd: Path, *, timeout: float = STARTUP_TIMEOUT) -> str:
    deadline = time.time() + timeout
    ready_streak = 0
    while time.time() <= deadline:
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
        if any(prompt in capture for prompt in plugin.login_prompts):
            raise OrcheError(f"{plugin.display_name} is not logged in inside the tmux pane")
        running = is_agent_running(plugin, pane_id)
        ready_candidate = running and plugin.capture_has_ready_surface(capture, cwd)
        ready_streak = ready_streak + 1 if ready_candidate else 0
        if ready_streak >= plugin.ready_streak_required:
            return pane_id
        time.sleep(1.0)
    raise OrcheError(f"Timed out waiting for {plugin.display_name} to become ready in {pane_id}")


def ensure_agent_running(
    plugin: AgentPlugin,
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    approve_all: bool = False,
    runtime: Optional[AgentRuntime] = None,
    discord_channel_id: Optional[str] = None,
) -> str:
    if is_agent_running(plugin, pane_id):
        return pane_id
    approve_all = True
    info = get_pane_info(pane_id)
    if info is None:
        raise OrcheError(f"Pane disappeared before {plugin.display_name} launch: {pane_id}")
    if info.get("pane_dead") == "1":
        tmux("respawn-pane", "-k", "-t", pane_id, "-c", str(cwd), check=True, capture=True)
    else:
        tmux("send-keys", "-t", pane_id, "C-c", check=False, capture=True)
        time.sleep(0.2)
    try:
        launch_command = plugin.build_launch_command(
            approve_all=approve_all,
            cwd=cwd,
            runtime=runtime or AgentRuntime(label=plugin.runtime_label),
            session=session,
            discord_channel_id=discord_channel_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise OrcheError(str(exc)) from exc
    tmux(
        "send-keys",
        "-t",
        pane_id,
        "-l",
        launch_command,
        check=True,
        capture=True,
    )
    tmux("send-keys", "-t", pane_id, "Enter", check=True, capture=True)
    pane_id = wait_for_agent_ready(plugin, pane_id, cwd)
    bridge_name_pane(pane_id, session)
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "pane_id": pane_id,
            "agent_started_at": time.time(),
            "agent_approve_all": approve_all,
            "last_seen_at": time.time(),
        }
    )
    apply_runtime_to_meta(meta, agent=plugin.name, runtime=runtime or AgentRuntime(label=plugin.runtime_label))
    save_meta(session, meta)
    return pane_id


def is_codex_running(pane_id: str) -> bool:
    return is_agent_running(get_agent("codex"), pane_id)


def ensure_codex_running(
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    approve_all: bool = False,
    codex_home: Optional[str] = None,
    discord_channel_id: Optional[str] = None,
) -> str:
    return ensure_agent_running(
        get_agent("codex"),
        session,
        cwd,
        pane_id,
        approve_all=approve_all,
        runtime=AgentRuntime(home=normalize_codex_home(codex_home), managed=False, label=get_agent("codex").runtime_label),
        discord_channel_id=discord_channel_id,
    )


def append_action_history(session: str, cwd: Path, agent: str, action: str, **fields: Any) -> None:
    append_history_entry(
        session,
        {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "action": action,
            **fields,
        },
    )


def ensure_managed_codex_home(session: str, *, cwd: Path, discord_channel_id: Optional[str]) -> Path:
    codex_agent_module.DEFAULT_RUNTIME_HOME_ROOT = DEFAULT_CODEX_HOME_ROOT
    codex_agent_module.DEFAULT_CODEX_SOURCE_HOME = DEFAULT_CODEX_SOURCE_HOME
    runtime = prepare_managed_runtime(get_agent("codex"), session, cwd=cwd, discord_channel_id=discord_channel_id)
    return Path(runtime.home)


def ensure_managed_claude_home(session: str, *, cwd: Path, discord_channel_id: Optional[str]) -> Path:
    runtime = prepare_managed_runtime(get_agent("claude"), session, cwd=cwd, discord_channel_id=discord_channel_id)
    return Path(runtime.home)


def remove_managed_codex_home(codex_home: str) -> None:
    if codex_home:
        remove_runtime_home(codex_home)


def session_launch_mode(meta: Mapping[str, Any]) -> str:
    mode = str(meta.get("launch_mode") or "").strip()
    return mode or "managed"


def native_cli_args_from_meta(meta: Mapping[str, Any]) -> List[str]:
    raw_args = meta.get("native_cli_args")
    if not isinstance(raw_args, list):
        return []
    values: List[str] = []
    for value in raw_args:
        text = str(value)
        if text:
            values.append(text)
    return values


def build_native_agent_launch_command(
    plugin: AgentPlugin,
    *,
    cwd: Path,
    cli_args: Sequence[str],
) -> str:
    command = [plugin.name, *[str(value) for value in cli_args]]
    prefix = [f"cd {shlex.quote(str(cwd))}"]
    prefix.append(f"exec {' '.join(shlex.quote(part) for part in command)}")
    return " && ".join(prefix)


def ensure_native_agent_running(
    plugin: AgentPlugin,
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    cli_args: Sequence[str],
) -> str:
    if is_agent_running(plugin, pane_id):
        return pane_id
    info = get_pane_info(pane_id)
    if info is None:
        raise OrcheError(f"Pane disappeared before {plugin.display_name} launch: {pane_id}")
    if info.get("pane_dead") == "1":
        tmux("respawn-pane", "-k", "-t", pane_id, "-c", str(cwd), check=True, capture=True)
    else:
        tmux("send-keys", "-t", pane_id, "C-c", check=False, capture=True)
        time.sleep(0.2)
    launch_command = build_native_agent_launch_command(
        plugin,
        cwd=cwd,
        cli_args=cli_args,
    )
    tmux(
        "send-keys",
        "-t",
        pane_id,
        "-l",
        launch_command,
        check=True,
        capture=True,
    )
    tmux("send-keys", "-t", pane_id, "Enter", check=True, capture=True)
    pane_id = wait_for_agent_ready(plugin, pane_id, cwd)
    bridge_name_pane(pane_id, session)
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "pane_id": pane_id,
            "agent_started_at": time.time(),
            "last_seen_at": time.time(),
        }
    )
    save_meta(session, meta)
    return pane_id


def ensure_native_session(
    session: str,
    cwd: Path,
    agent: str,
    *,
    cli_args: Sequence[str] = (),
) -> str:
    cwd = cwd.resolve()
    plugin = get_agent(agent)
    existing_meta = load_meta(session)
    existing_cwd = Path(str(existing_meta.get("cwd") or "")).resolve() if existing_meta.get("cwd") else None
    if existing_cwd is not None and existing_cwd != cwd:
        raise OrcheError(
            f"Session {session} is already bound to cwd={existing_cwd}. "
            "Use the same --cwd or close the session and create a new one."
        )
    existing_agent = str(existing_meta.get("agent") or "").strip()
    if existing_agent and existing_agent != plugin.name:
        raise OrcheError(
            f"Session {session} is already bound to agent={existing_agent}. "
            "Close the session and create a new one for a different agent."
        )
    if existing_meta and session_launch_mode(existing_meta) != "native":
        raise OrcheError(
            f"Session {session} is already managed by orche session-new. "
            "Use session-new commands for managed sessions or close the session and recreate it with the shortcut command."
        )
    provided_cli_args = [str(value) for value in cli_args]
    existing_cli_args = native_cli_args_from_meta(existing_meta)
    if existing_meta and provided_cli_args and provided_cli_args != existing_cli_args:
        raise OrcheError(
            f"Session {session} is already bound to native args={existing_cli_args!r}. "
            "Use the same shortcut args or close the session and create a new one."
        )
    resolved_cli_args = existing_cli_args or provided_cli_args
    pane_id = ensure_pane(session, cwd, agent)
    pane_id = ensure_native_agent_running(
        plugin,
        session,
        cwd,
        pane_id,
        cli_args=resolved_cli_args,
    )
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "pane_id": pane_id,
            "launch_mode": "native",
            "native_cli_args": list(resolved_cli_args),
            "last_seen_at": time.time(),
            "runtime_home": "",
            "runtime_home_managed": False,
            "runtime_label": "",
            "codex_home": "",
            "codex_home_managed": False,
        }
    )
    meta.pop("discord_channel_id", None)
    meta.pop("discord_session", None)
    meta.pop("notify_routes", None)
    meta.pop("notify_binding", None)
    save_meta(session, meta)
    update_runtime_config(
        session=session,
        cwd=cwd,
        agent=agent,
        pane_id=pane_id,
        runtime_home="",
        runtime_home_managed=False,
        runtime_label="",
    )
    return pane_id


def ensure_session(
    session: str,
    cwd: Path,
    agent: str,
    *,
    approve_all: bool = False,
    runtime_home: Optional[str] = None,
    codex_home: Optional[str] = None,
    notify_to: Optional[str] = None,
    notify_target: Optional[str] = None,
) -> str:
    cwd = cwd.resolve()
    plugin = get_agent(agent)
    existing_meta = load_meta(session)
    if existing_meta and session_launch_mode(existing_meta) != "managed":
        raise OrcheError(
            f"Session {session} is already bound to native shortcut mode. "
            "Use the shortcut command again or close the session and recreate it with session-new."
        )
    existing_cwd = Path(str(existing_meta.get("cwd") or "")).resolve() if existing_meta.get("cwd") else None
    if existing_cwd is not None and existing_cwd != cwd:
        raise OrcheError(
            f"Session {session} is already bound to cwd={existing_cwd}. "
            "Use the same --cwd or close the session and create a new one."
        )
    existing_agent = str(existing_meta.get("agent") or "").strip()
    if existing_agent and existing_agent != plugin.name:
        raise OrcheError(
            f"Session {session} is already bound to agent={existing_agent}. "
            "Close the session and create a new one for a different agent."
        )
    existing_notify_binding = _read_notify_binding(existing_meta)
    provided_notify_to = str(notify_to or "").strip()
    provided_notify_target = str(notify_target or "").strip()
    if (not provided_notify_to or not provided_notify_target) and not existing_notify_binding:
        raise OrcheError("session-new requires both --notify-to and --notify-target")
    provided_notify_binding = (
        build_notify_binding(provided_notify_to, provided_notify_target)
        if provided_notify_to and provided_notify_target
        else existing_notify_binding
    )
    if existing_meta and provided_notify_binding != existing_notify_binding:
        if existing_notify_binding:
            raise OrcheError(
                f"Session {session} is already bound to notify_to={existing_notify_binding['provider']} "
                f"notify_target={existing_notify_binding['target']}. "
                "Use the same notify binding or close the session and create a new one."
            )
    resolved_notify_binding = existing_notify_binding or provided_notify_binding
    resolved_discord_channel_id = (
        resolved_notify_binding.get("target")
        if resolved_notify_binding.get("provider") == "discord"
        else ""
    )

    requested_runtime_home = runtime_home or codex_home
    managed_runtime_home = False
    if requested_runtime_home:
        resolved_runtime_home = normalize_runtime_home(requested_runtime_home)
        runtime = AgentRuntime(home=resolved_runtime_home, managed=False, label=plugin.runtime_label)
    elif runtime_home_from_meta(existing_meta):
        resolved_runtime_home = runtime_home_from_meta(existing_meta)
        managed_runtime_home = runtime_home_managed_from_meta(existing_meta)
        runtime = AgentRuntime(
            home=resolved_runtime_home,
            managed=managed_runtime_home,
            label=runtime_label_from_meta(existing_meta, plugin),
        )
    else:
        runtime = prepare_managed_runtime(
            plugin,
            session,
            cwd=cwd,
            discord_channel_id=resolved_discord_channel_id,
        )
        resolved_runtime_home = normalize_runtime_home(runtime.home)
        managed_runtime_home = True
    if managed_runtime_home:
        runtime = prepare_managed_runtime(
            plugin,
            session,
            cwd=cwd,
            discord_channel_id=resolved_discord_channel_id,
        )
        resolved_runtime_home = normalize_runtime_home(runtime.home)
    existing_runtime_home = runtime_home_from_meta(existing_meta)
    if existing_runtime_home and resolved_runtime_home and existing_runtime_home != resolved_runtime_home:
        raise OrcheError(
            f"Session {session} is already bound to runtime_home={existing_runtime_home}. "
            f"Use the same {plugin.runtime_option_name} or close the session and create a new one."
        )
    pane_id = ensure_pane(session, cwd, agent)
    pane_id = ensure_agent_running(
        plugin,
        session,
        cwd,
        pane_id,
        approve_all=approve_all,
        runtime=runtime,
        discord_channel_id=resolved_discord_channel_id,
    )
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "pane_id": pane_id,
            "launch_mode": "managed",
            "last_seen_at": time.time(),
        }
    )
    apply_runtime_to_meta(meta, agent=agent, runtime=runtime)
    meta.pop("native_cli_args", None)
    meta.pop("discord_channel_id", None)
    meta.pop("discord_session", None)
    meta.pop("notify_routes", None)
    if resolved_notify_binding:
        meta["notify_binding"] = resolved_notify_binding
    else:
        meta.pop("notify_binding", None)
    save_meta(session, meta)
    update_runtime_config(
        session=session,
        cwd=cwd,
        agent=agent,
        pane_id=pane_id,
        runtime_home=resolved_runtime_home,
        runtime_home_managed=managed_runtime_home,
        runtime_label=runtime.label,
    )
    return pane_id


def send_prompt(
    session: str,
    cwd: Path,
    agent: str,
    prompt: str,
    *,
    approve_all: bool = False,
) -> str:
    plugin = get_agent(agent)
    meta = load_meta(session)
    if session_launch_mode(meta) == "native":
        pane_id = ensure_native_session(
            session,
            cwd,
            agent,
            cli_args=native_cli_args_from_meta(meta),
        )
    else:
        pane_id = ensure_session(
            session,
            cwd,
            agent,
            approve_all=approve_all,
        )
    before_capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
    meta = load_meta(session)
    meta["pending_turn"] = {
        "turn_id": uuid.uuid4().hex[:12],
        "prompt": prompt,
        "before_capture": before_capture,
        "submitted_at": time.time(),
        "pane_id": pane_id,
    }
    save_meta(session, meta)
    plugin.submit_prompt(session, prompt, bridge=BRIDGE)
    append_action_history(session, cwd, agent, "prompt", prompt=prompt, pane_id=pane_id)
    return pane_id


def latest_turn_summary(session: str) -> str:
    meta = load_meta(session)
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
    if pending_turn:
        pane_id = str((bridge_resolve(session) or pending_turn.get("pane_id") or meta.get("pane_id") or "")).strip()
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES) if pane_id else ""
        before_capture = str(pending_turn.get("before_capture") or "")
        delta = turn_delta(before_capture, capture) if capture else ""
        summary = extract_summary_candidate(delta, prompt=str(pending_turn.get("prompt") or ""))
        if summary:
            completed = dict(pending_turn)
            completed["summary"] = summary
            completed["completed_at"] = time.time()
            meta["last_completed_turn"] = completed
            meta.pop("pending_turn", None)
            save_meta(session, meta)
            return summary
        save_meta(session, meta)
        return ""
    last_completed = meta.get("last_completed_turn") if isinstance(meta.get("last_completed_turn"), dict) else None
    if last_completed:
        return str(last_completed.get("summary") or "")
    return ""


def build_status(session: str) -> Dict[str, Any]:
    meta = load_meta(session)
    pane_id = bridge_resolve(session) or str(meta.get("pane_id") or "")
    info = get_pane_info(pane_id) if pane_id else None
    cwd = str(meta.get("cwd") or (info or {}).get("pane_current_path") or "-")
    agent = str(meta.get("agent") or "codex")
    plugin = get_agent(agent)
    notify_binding = _read_notify_binding(meta) if meta else {}
    discord_session = notify_binding.get("session", "") if notify_binding.get("provider") == "discord" else ""
    runtime_home = runtime_home_from_meta(meta)
    runtime_home_managed = runtime_home_managed_from_meta(meta)
    agent_running = bool(pane_id and is_agent_running(plugin, pane_id))
    return {
        "backend": BACKEND,
        "session": session,
        "cwd": cwd,
        "agent": agent,
        "runtime_home": runtime_home,
        "runtime_home_managed": runtime_home_managed,
        "runtime_label": runtime_label_from_meta(meta, plugin),
        "codex_home": str(meta.get("codex_home") or runtime_home),
        "codex_home_managed": bool(meta.get("codex_home_managed") or runtime_home_managed),
        "pane_id": pane_id or "-",
        "window_name": (info or {}).get("window_name", meta.get("window_name", "-")),
        "agent_running": agent_running,
        "codex_running": agent_running,
        "pane_exists": bool(pane_id and pane_exists(pane_id)),
        "discord_session": discord_session,
        "notify_binding": notify_binding,
    }


def resolve_session_context(
    *,
    session: str,
    require_existing: bool = False,
    require_cwd_agent: bool = False,
) -> Tuple[Optional[Path], Optional[str], Dict[str, Any]]:
    meta = load_meta(session)
    cwd = Path(meta["cwd"]).resolve() if meta.get("cwd") else None
    agent = str(meta.get("agent")) if meta.get("agent") else None
    if require_existing and not meta:
        raise OrcheError(f"Unknown session: {session}")
    if require_cwd_agent and (cwd is None or agent is None):
        raise OrcheError(f"Session {session} is missing cwd/agent context; create it with session-new first")
    return cwd, agent, meta


def current_session_id() -> str:
    env_session = str(os.environ.get("ORCHE_SESSION") or "").strip()
    if env_session:
        return env_session

    current_pane_id = _current_tmux_value("#{pane_id}")
    if current_pane_id:
        for entry in list_sessions():
            if str(entry.get("pane_id") or "").strip() == current_pane_id:
                session = str(entry.get("session") or "").strip()
                if session:
                    return session

    pane_title = _current_tmux_value("#{pane_title}")
    if pane_title:
        meta = load_meta(pane_title)
        if meta:
            return str(meta.get("session") or pane_title).strip()

    window_name = _current_tmux_value("#{window_name}")
    if window_name:
        for entry in list_sessions():
            if str(entry.get("window_name") or "").strip() == window_name:
                session = str(entry.get("session") or "").strip()
                if session:
                    return session

    raise OrcheError("Unable to resolve current orche session id. Set ORCHE_SESSION or run inside an orche tmux pane.")


def cancel_session(session: str) -> str:
    _cwd, agent, _meta = resolve_session_context(session=session)
    plugin = get_agent(agent or "codex")
    plugin.interrupt(session, bridge=BRIDGE)
    return bridge_resolve(session) or "-"


def close_session(session: str) -> str:
    meta = load_meta(session)
    agent = str(meta.get("agent") or "codex")
    plugin = get_agent(agent)
    pane_id = bridge_resolve(session) or str(meta.get("pane_id") or "")
    if pane_id and pane_exists(pane_id):
        info = get_pane_info(pane_id)
        if info is not None:
            tmux("kill-window", "-t", info["window_id"], check=False, capture=True)
    runtime_home = runtime_home_from_meta(meta)
    if runtime_home and runtime_home_managed_from_meta(meta):
        plugin.cleanup_runtime(
            AgentRuntime(
                home=runtime_home,
                managed=True,
                label=runtime_label_from_meta(meta, plugin),
            )
        )
    config = load_config()
    if str(config.get("session") or "") == session:
        config["session"] = ""
        config["cwd"] = ""
        config["agent"] = ""
        config["pane_id"] = ""
        config["runtime_home"] = ""
        config["runtime_home_managed"] = False
        config["runtime_label"] = ""
        config["codex_home"] = ""
        config["codex_home_managed"] = False
        config["updated_at"] = time.time()
        save_config(config)
    remove_meta(session)
    return pane_id or "-"
