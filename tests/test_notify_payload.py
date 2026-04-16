from __future__ import annotations

import json

import pytest

from notify import payload as payload_module
from notify.config import NotifyConfig
from notify.payload import (
    build_message_from_payload,
    parse_payload,
    summarize_assistant_message,
)

pytestmark = pytest.mark.unit


def test_parse_payload_rejects_invalid_or_non_mapping_json():
    assert parse_payload("") is None
    assert parse_payload("not-json") is None
    assert parse_payload('["not-a-mapping"]') is None


def test_summarize_assistant_message_preserves_structure():
    summary = summarize_assistant_message(
        "First line\nSecond line\n\n## Result\n- item one\n\n```py\nline1\nline2\nline3\nline4\nline5\nline6\n```",
        max_chars=400,
    )

    assert (
        summary
        == "First line Second line\n\n**Result**\n\n- item one\n\n```py\nline1\nline2\nline3\nline4\nline5\n...\n```"
    )


def test_summarize_assistant_message_closes_truncated_code_block():
    summary = summarize_assistant_message(
        "```py\nline1\nline2\nline3\nline4\nline5\nline6\n```",
        max_chars=18,
    )

    assert "…" in summary
    assert summary.endswith("```")


def test_build_message_from_payload_prefers_summary_loader_for_stop_hooks():
    message = build_message_from_payload(
        '{"hook_event_name":"Stop","session":"worker-1","last_agent_message":"stale"}',
        notify_config=NotifyConfig(),
        runtime_config={"notify_binding": {"provider": "discord", "target": "123"}},
        summary_loader=lambda session: f"loaded:{session}",
    )

    assert message is not None
    assert message.event == "completed"
    assert message.session == "worker-1"
    assert message.summary == "loaded:worker-1"


def test_build_message_from_payload_only_accepts_startup_session_start():
    accepted = build_message_from_payload(
        '{"hook_event_name":"SessionStart","session_id":"claude-session","source":"startup"}',
        notify_config=NotifyConfig(),
        runtime_config={},
        summary_loader=lambda session: "",
    )
    ignored = build_message_from_payload(
        '{"hook_event_name":"SessionStart","session_id":"claude-session","source":"resume"}',
        notify_config=NotifyConfig(),
        runtime_config={},
        summary_loader=lambda session: "",
    )

    assert accepted is not None
    assert accepted.event == "session-start"
    assert accepted.metadata["hook_source"] == "startup"
    assert ignored is None


def test_assistant_message_from_transcript_returns_last_assistant_text(tmp_path):
    transcript_path = tmp_path / "claude.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "first"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "second"}]},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    result = payload_module._assistant_message_from_transcript(
        {"transcript_path": str(transcript_path)},
        wait_seconds=0.0,
    )

    assert result == "second"
