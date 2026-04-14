from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

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
_FIELD_SPECS: dict[str, list[tuple[str, ...]]] = {
    "event": [("event",), ("type",), ("kind",), ("hook_event_name",), ("notification_type",), ("name",), ("notification", "event"), ("payload", "event"), ("payload", "hook_event_name")],
    "assistant_message": [("last_agent_message",), ("lastAgentMessage",), ("last-assistant-message",), ("last_assistant_message",), ("lastAssistantMessage",), ("summary",), ("payload", "last_agent_message"), ("payload", "lastAgentMessage"), ("payload", "last-assistant-message"), ("payload", "last_assistant_message"), ("payload", "lastAssistantMessage"), ("payload", "summary"), ("content",), ("body",), ("payload", "content"), ("payload", "body"), ("message",), ("payload", "message")],
    "hook_event_name": [("hook_event_name",), ("payload", "hook_event_name")],
    "notification_type": [("notification_type",), ("payload", "notification_type")],
    "title": [("title",), ("payload", "title")],
    "transcript_path": [("transcript_path",), ("transcriptPath",), ("payload", "transcript_path"), ("payload", "transcriptPath")],
    "session": [("session",), ("session_id",), ("sessionId",), ("thread_id",), ("thread-id",), ("threadId",), ("payload", "session"), ("payload", "session_id"), ("payload", "sessionId"), ("payload", "thread_id"), ("payload", "thread-id"), ("payload", "threadId")],
    "cwd": [("cwd",), ("payload", "cwd")],
    "turn_id": [("turn_id",), ("turn-id",), ("turnId",), ("metadata", "turn_id"), ("metadata", "turn-id"), ("metadata", "turnId"), ("payload", "turn_id"), ("payload", "turn-id"), ("payload", "turnId")],
    "source": [("source",), ("metadata", "source"), ("payload", "source")],
    "tail_text": [("tail_text",), ("tail",), ("metadata", "tail_text"), ("metadata", "tail"), ("payload", "tail_text"), ("payload", "tail")],
    "tail_lines": [("tail_lines",), ("metadata", "tail_lines"), ("payload", "tail_lines")],
}
_LIST_FIELD_SPECS: dict[str, list[tuple[str, ...]]] = {
    "input_message": [("input_messages",), ("input-messages",), ("inputMessages",), ("messages",), ("payload", "input_messages"), ("payload", "input-messages"), ("payload", "inputMessages")]
}
_DEFAULT_SUMMARIES = {
    "failed": "Agent turn failed",
    "startup-blocked": "Agent startup blocked",
    "session-start": "Claude session started",
    "prompt-accepted": "Claude accepted the prompt",
    "notification": "Claude sent a notification",
    "permission-request": "Claude requested permission",
    "needs-input": "Agent likely needs input",
    "stalled": "Agent turn stalled",
}
_SUMMARY_LOADER_EVENTS = {"session-start", "prompt-accepted", "notification", "permission-request", "startup-blocked"}
def _first_string(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
def _lookup(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current
def _extract(payload: Mapping[str, Any], key: str) -> str:
    return _first_string(*(_lookup(payload, path) for path in _FIELD_SPECS[key]))
def _extract_last_list_text(payload: Mapping[str, Any], key: str) -> str:
    for path in _LIST_FIELD_SPECS[key]:
        candidate = _lookup(payload, path)
        if not isinstance(candidate, list):
            continue
        for item in reversed(candidate):
            text = _first_string(item)
            if text:
                return text
    return ""
def parse_payload(payload_text: str) -> Mapping[str, Any] | None:
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
    blocks: list[str] = []
    paragraph_lines: list[str] = []
    list_lines: list[str] = []
    code_lines: list[str] = []
    in_code_block = False
    code_language = ""
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
        if code_lines or code_truncated:
            content_lines = list(code_lines)
            if code_truncated:
                content_lines.append("...")
            blocks.append(f"```{code_language}\n" + "\n".join(content_lines) + "\n```")
        code_lines.clear()
        code_language = ""
        code_truncated = False

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
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
                code_lines.append(raw_line.rstrip())
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
            blocks.append(f"**{_compact_text(re.sub(r'^#{1,6}\\s+', '', stripped))}**")
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
    return EVENT_ALIASES.get(_extract(payload, "event").lower(), _extract(payload, "event").lower())
def _is_stop_hook_payload(payload: Mapping[str, Any]) -> bool:
    raw = _extract(payload, "hook_event_name").lower()
    return EVENT_ALIASES.get(raw, raw) == "completed" and raw in {"stop", "subagentstop"}
def _assistant_message_from_transcript(payload: Mapping[str, Any], *, wait_seconds: float = 0.0) -> str:
    transcript_path = _extract(payload, "transcript_path")
    if not transcript_path:
        return ""
    path = Path(transcript_path).expanduser()
    if not path.exists() or path.stat().st_size > MAX_JSON_INPUT_BYTES:
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
            message = entry.get("message") if isinstance(entry, Mapping) and entry.get("type") == "assistant" else None
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, list):
                continue
            for item in reversed(content):
                if isinstance(item, Mapping) and str(item.get("type") or "") == "text":
                    text = _first_string(item.get("text"))
                    if text:
                        return text
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.25)
def _target_provider(*, runtime_config: Mapping[str, Any], notify_config: NotifyConfig, explicit_channel_id: str = "") -> str:
    if str(explicit_channel_id or "").strip():
        return "discord"
    binding = runtime_config.get("notify_binding")
    if isinstance(binding, Mapping):
        provider = str(binding.get("provider") or "").strip()
        target = str(binding.get("target") or "").strip()
        if provider and target:
            return provider
    return str(notify_config.provider or "").strip()
def _default_summary_for_event(event_name: str, notify_config: NotifyConfig) -> str:
    return _DEFAULT_SUMMARIES.get(event_name, notify_config.default_message_prefix)
def _normalize_event_status(event_name: str, status: str) -> str:
    normalized = status.strip().lower() or "success"
    return event_name if normalized == "warning" and event_name in {"stalled", "needs-input", "startup-blocked"} else normalized
def build_message_from_payload(
    payload_text: str,
    *,
    notify_config: NotifyConfig,
    runtime_config: Mapping[str, Any],
    summary_loader: Callable[[str], str],
    explicit_session: str = "",
    explicit_channel_id: str = "",
    status: str = "success",
) -> NotifyEvent | None:
    payload = parse_payload(payload_text)
    if payload is None:
        return None
    event_name = _event_name(payload)
    source = _extract(payload, "source")
    if event_name == "session-start" and source.lower() != "startup":
        return None
    if event_name not in SUPPORTED_EVENTS:
        return None
    session = _first_string(explicit_session, _extract(payload, "session"), runtime_config.get("session"))
    assistant_message = _extract(payload, "assistant_message")
    loaded_summary = ""
    transcript_summary = _assistant_message_from_transcript(payload, wait_seconds=3.0) if event_name == "completed" else ""
    if event_name == "completed":
        prefer_loaded_summary = _is_stop_hook_payload(payload)
        if not transcript_summary and session and (prefer_loaded_summary or not assistant_message):
            loaded_summary = summary_loader(session)
        assistant_message = _first_string(transcript_summary, loaded_summary, assistant_message) if prefer_loaded_summary else _first_string(transcript_summary, assistant_message, loaded_summary)
    elif session and event_name not in _SUMMARY_LOADER_EVENTS:
        loaded_summary = summary_loader(session)
    if not assistant_message and loaded_summary:
        assistant_message = loaded_summary
    provider = _target_provider(runtime_config=runtime_config, notify_config=notify_config, explicit_channel_id=explicit_channel_id)
    assistant_summary = summarize_assistant_message(assistant_message, max_chars=notify_config.summary_max_chars) if assistant_message and provider == "discord" else assistant_message.strip()
    return NotifyEvent(
        event=event_name or "completed",
        summary=assistant_summary or _default_summary_for_event(event_name, notify_config),
        session=session,
        cwd=_first_string(_extract(payload, "cwd"), runtime_config.get("cwd")),
        status=_normalize_event_status(event_name, status),
        metadata={
            "turn_id": _extract(payload, "turn_id"),
            "input_message": _extract_last_list_text(payload, "input_message"),
            "source": source,
            "hook_event_name": _extract(payload, "hook_event_name"),
            "hook_source": source,
            "notification_type": _extract(payload, "notification_type"),
            "title": _extract(payload, "title"),
            "tail_text": _extract(payload, "tail_text"),
            "tail_lines": _extract(payload, "tail_lines"),
        },
    )
