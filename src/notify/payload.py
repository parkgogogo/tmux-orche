from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from json_utils import JSONInputTooLargeError, MAX_JSON_INPUT_BYTES, loads_json
from .config import NotifyConfig
from .models import NotifyEvent

EVENT_ALIASES = {
    "": "completed",
    "agent-turn-complete": "completed",
    "turn-complete": "completed",
    "turn_complete": "completed",
    "task_complete": "completed",
    "task-complete": "completed",
    "stop": "completed",
    "subagentstop": "completed",
    "completed": "completed",
    "complete": "completed",
    "sessionstart": "session-start",
    "userpromptsubmit": "prompt-accepted",
    "notification": "notification",
    "permissionrequest": "permission-request",
    "startup-blocked": "startup-blocked",
    "startup_blocked": "startup-blocked",
    "stalled": "stalled",
    "needs-input": "needs-input",
    "needs_input": "needs-input",
    "failed": "failed",
}
SUPPORTED_EVENTS = set(EVENT_ALIASES) | set(EVENT_ALIASES.values())


def _first_string(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _payload_value(payload: Mapping[str, Any], *path_options: tuple[str, ...]) -> str:
    for path in path_options:
        current: Any = payload
        found = True
        for part in path:
            if not isinstance(current, Mapping) or part not in current:
                found = False
                break
            current = current[part]
        if found:
            text = _first_string(current)
            if text:
                return text
    return ""


def parse_payload(payload_text: str) -> Optional[Mapping[str, Any]]:
    raw = payload_text.strip()
    if not raw:
        return None
    try:
        payload = loads_json(raw, source="notify payload")
    except (json.JSONDecodeError, JSONInputTooLargeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_list_like(line: str) -> bool:
    return bool(re.match(r"^([-*+]\s+|\d+\.\s+|>\s+)", line))


def _truncate_discord_text(text: str, max_chars: int) -> str:
    value = text.strip()
    if len(value) <= max_chars:
        return value
    ellipsis = "…"
    if max_chars <= len(ellipsis):
        return ellipsis[:max_chars]
    truncated = value[: max_chars - len(ellipsis)].rstrip()
    if truncated.count("```") % 2 == 1:
        closing = "\n```"
        if max_chars <= len(ellipsis) + len(closing):
            return value[:max_chars].rstrip()
        truncated = value[: max_chars - len(ellipsis) - len(closing)].rstrip()
        if "\n" in truncated:
            truncated = truncated.rsplit("\n", 1)[0].rstrip() or truncated
        return f"{truncated}{ellipsis}{closing}"
    return f"{truncated}{ellipsis}"


def summarize_assistant_message(text: str, *, max_chars: int) -> str:
    blocks = []
    paragraph_lines = []
    list_lines = []
    in_code_block = False
    code_language = ""
    code_lines = []
    code_truncated = False

    def flush_paragraph() -> None:
        if paragraph_lines:
            blocks.append(" ".join(paragraph_lines).strip())
            paragraph_lines.clear()

    def flush_list() -> None:
        if list_lines:
            blocks.append("\n".join(list_lines).strip())
            list_lines.clear()

    def flush_code_block() -> None:
        nonlocal code_language, code_truncated
        if not code_lines and not code_truncated:
            code_language = ""
            return
        content_lines = list(code_lines)
        if code_truncated:
            content_lines.append("...")
        blocks.append(f"```{code_language}\n" + "\n".join(content_lines) + "\n```")
        code_lines.clear()
        code_language = ""
        code_truncated = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code_block:
                flush_code_block()
            else:
                code_language = stripped[3:].strip()
                code_lines.clear()
                code_truncated = False
            in_code_block = not in_code_block
            continue
        if in_code_block:
            if len(code_lines) < 5:
                code_lines.append(line)
            else:
                code_truncated = True
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if re.match(r"^#{1,6}\s+", stripped):
            flush_paragraph()
            flush_list()
            title = _compact_text(re.sub(r"^#{1,6}\s+", "", stripped))
            blocks.append(f"**{title}**")
            continue
        normalized = _compact_text(stripped)
        if _is_list_like(normalized):
            flush_paragraph()
            list_lines.append(normalized)
            continue
        flush_list()
        paragraph_lines.append(normalized)
    if in_code_block:
        flush_code_block()
    flush_paragraph()
    flush_list()
    return _truncate_discord_text("\n\n".join(block for block in blocks if block.strip()), max_chars)


def _event_name(payload: Mapping[str, Any]) -> str:
    raw = _first_string(
        payload.get("event"),
        payload.get("type"),
        payload.get("kind"),
        payload.get("hook_event_name"),
        payload.get("notification_type"),
        payload.get("name"),
        _payload_value(payload, ("notification", "event")),
        _payload_value(payload, ("payload", "event")),
        _payload_value(payload, ("payload", "hook_event_name")),
    ).lower()
    return EVENT_ALIASES.get(raw, raw)


def _is_stop_hook_payload(payload: Mapping[str, Any]) -> bool:
    raw = _first_string(
        payload.get("hook_event_name"),
        _payload_value(payload, ("payload", "hook_event_name")),
    ).lower()
    return EVENT_ALIASES.get(raw, raw) == "completed" and raw in {"stop", "subagentstop"}


def _assistant_message(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("last_agent_message"),
        payload.get("lastAgentMessage"),
        payload.get("last-assistant-message"),
        payload.get("last_assistant_message"),
        payload.get("lastAssistantMessage"),
        payload.get("summary"),
        _payload_value(payload, ("payload", "last_agent_message")),
        _payload_value(payload, ("payload", "lastAgentMessage")),
        _payload_value(payload, ("payload", "last-assistant-message")),
        _payload_value(payload, ("payload", "last_assistant_message")),
        _payload_value(payload, ("payload", "lastAssistantMessage")),
        _payload_value(payload, ("payload", "summary")),
        payload.get("content"),
        payload.get("body"),
        _payload_value(payload, ("payload", "content")),
        _payload_value(payload, ("payload", "body")),
        payload.get("message"),
        _payload_value(payload, ("payload", "message")),
    )


def _payload_hook_event_name(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("hook_event_name"),
        _payload_value(payload, ("payload", "hook_event_name")),
    )


def _payload_notification_type(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("notification_type"),
        _payload_value(payload, ("payload", "notification_type")),
    )


def _payload_title(payload: Mapping[str, Any]) -> str:
    return _first_string(payload.get("title"), _payload_value(payload, ("payload", "title")))


def _target_provider(
    *,
    runtime_config: Mapping[str, Any],
    notify_config: NotifyConfig,
    explicit_channel_id: str = "",
) -> str:
    if str(explicit_channel_id or "").strip():
        return "discord"
    binding = runtime_config.get("notify_binding")
    if isinstance(binding, Mapping):
        provider = str(binding.get("provider") or "").strip()
        target = str(binding.get("target") or "").strip()
        if provider and target:
            return provider
    return str(notify_config.provider or "").strip()


def _payload_transcript_path(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("transcript_path"),
        payload.get("transcriptPath"),
        _payload_value(payload, ("payload", "transcript_path")),
        _payload_value(payload, ("payload", "transcriptPath")),
    )


def _assistant_message_from_transcript(payload: Mapping[str, Any], *, wait_seconds: float = 0.0) -> str:
    transcript_path = _payload_transcript_path(payload)
    if not transcript_path:
        return ""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return ""
    if path.stat().st_size > MAX_JSON_INPUT_BYTES:
        return ""
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            raw = line.strip()
            if not raw:
                continue
            try:
                entry = loads_json(raw, source=str(path))
            except (json.JSONDecodeError, JSONInputTooLargeError):
                continue
            if not isinstance(entry, Mapping) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in reversed(content):
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("type") or "") != "text":
                    continue
                text = _first_string(item.get("text"))
                if text:
                    return text
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.25)


def _payload_session(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("session"),
        payload.get("session_id"),
        payload.get("sessionId"),
        payload.get("thread_id"),
        payload.get("thread-id"),
        payload.get("threadId"),
        _payload_value(payload, ("payload", "session")),
        _payload_value(payload, ("payload", "session_id")),
        _payload_value(payload, ("payload", "sessionId")),
        _payload_value(payload, ("payload", "thread_id")),
        _payload_value(payload, ("payload", "thread-id")),
        _payload_value(payload, ("payload", "threadId")),
    )


def _payload_cwd(payload: Mapping[str, Any]) -> str:
    return _first_string(payload.get("cwd"), _payload_value(payload, ("payload", "cwd")))


def _payload_turn_id(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("turn_id"),
        payload.get("turn-id"),
        payload.get("turnId"),
        _payload_value(payload, ("metadata", "turn_id")),
        _payload_value(payload, ("metadata", "turn-id")),
        _payload_value(payload, ("metadata", "turnId")),
        _payload_value(payload, ("payload", "turn_id")),
        _payload_value(payload, ("payload", "turn-id")),
        _payload_value(payload, ("payload", "turnId")),
    )


def _payload_input_message(payload: Mapping[str, Any]) -> str:
    candidates = (
        payload.get("input_messages"),
        payload.get("input-messages"),
        payload.get("inputMessages"),
        payload.get("messages"),
        _payload_value(payload, ("payload", "input_messages")),
        _payload_value(payload, ("payload", "input-messages")),
        _payload_value(payload, ("payload", "inputMessages")),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            for item in reversed(candidate):
                text = _first_string(item)
                if text:
                    return text
    return ""


def _payload_source(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("source"),
        _payload_value(payload, ("metadata", "source")),
        _payload_value(payload, ("payload", "source")),
    )


def _payload_tail_text(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("tail_text"),
        payload.get("tail"),
        _payload_value(payload, ("metadata", "tail_text")),
        _payload_value(payload, ("metadata", "tail")),
        _payload_value(payload, ("payload", "tail_text")),
        _payload_value(payload, ("payload", "tail")),
    )


def _payload_tail_lines(payload: Mapping[str, Any]) -> str:
    return _first_string(
        payload.get("tail_lines"),
        _payload_value(payload, ("metadata", "tail_lines")),
        _payload_value(payload, ("payload", "tail_lines")),
    )


def _default_summary_for_event(event_name: str, notify_config: NotifyConfig) -> str:
    if event_name == "failed":
        return "Agent turn failed"
    if event_name == "startup-blocked":
        return "Agent startup blocked"
    if event_name == "session-start":
        return "Claude session started"
    if event_name == "prompt-accepted":
        return "Claude accepted the prompt"
    if event_name == "notification":
        return "Claude sent a notification"
    if event_name == "permission-request":
        return "Claude requested permission"
    if event_name == "needs-input":
        return "Agent likely needs input"
    if event_name == "stalled":
        return "Agent turn stalled"
    return notify_config.default_message_prefix


def _normalize_event_status(event_name: str, status: str) -> str:
    normalized = status.strip().lower() or "success"
    if normalized != "warning":
        return normalized
    if event_name in {"stalled", "needs-input", "startup-blocked"}:
        return event_name
    return normalized


def build_message_from_payload(
    payload_text: str,
    *,
    notify_config: NotifyConfig,
    runtime_config: Mapping[str, Any],
    summary_loader: Callable[[str], str],
    explicit_session: str = "",
    explicit_channel_id: str = "",
    status: str = "success",
) -> Optional[NotifyEvent]:
    payload = parse_payload(payload_text)
    if payload is None:
        return None
    event_name = _event_name(payload)
    if event_name == "session-start" and _payload_source(payload).strip().lower() != "startup":
        return None
    if event_name not in SUPPORTED_EVENTS:
        return None
    session = _first_string(explicit_session, _payload_session(payload), runtime_config.get("session"))
    cwd = _first_string(_payload_cwd(payload), runtime_config.get("cwd"))
    assistant_message = _assistant_message(payload)
    loaded_summary = ""
    transcript_summary = (
        _assistant_message_from_transcript(payload, wait_seconds=3.0) if event_name == "completed" else ""
    )
    if event_name == "completed":
        prefer_loaded_summary = _is_stop_hook_payload(payload)
        if not transcript_summary and session and (prefer_loaded_summary or not assistant_message):
            loaded_summary = summary_loader(session)
        if prefer_loaded_summary:
            assistant_message = _first_string(transcript_summary, loaded_summary, assistant_message)
        else:
            assistant_message = _first_string(transcript_summary, assistant_message, loaded_summary)
    elif session and event_name not in {"session-start", "prompt-accepted", "notification", "permission-request", "startup-blocked"}:
        loaded_summary = summary_loader(session)
    if not assistant_message and loaded_summary:
        assistant_message = loaded_summary
    provider = _target_provider(
        runtime_config=runtime_config,
        notify_config=notify_config,
        explicit_channel_id=explicit_channel_id,
    )
    assistant_summary = (
        summarize_assistant_message(
            assistant_message,
            max_chars=notify_config.summary_max_chars,
        )
        if assistant_message and provider == "discord"
        else assistant_message.strip()
    )
    summary = assistant_summary or _default_summary_for_event(event_name, notify_config)
    normalized_status = _normalize_event_status(event_name, status)
    return NotifyEvent(
        event=event_name or "completed",
        summary=summary,
        session=session,
        cwd=cwd,
        status=normalized_status,
        metadata={
            "turn_id": _payload_turn_id(payload),
            "input_message": _payload_input_message(payload),
            "source": _payload_source(payload),
            "hook_event_name": _payload_hook_event_name(payload),
            "hook_source": _payload_source(payload),
            "notification_type": _payload_notification_type(payload),
            "title": _payload_title(payload),
            "tail_text": _payload_tail_text(payload),
            "tail_lines": _payload_tail_lines(payload),
        },
    )
