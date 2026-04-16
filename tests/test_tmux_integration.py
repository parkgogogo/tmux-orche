from __future__ import annotations

import shutil
import time
import uuid

import pytest

from text_utils import window_name
from tmux.client import tmux
from tmux.query import (
    ensure_tmux_session,
    find_window,
    get_pane_info,
    list_panes,
    list_tmux_sessions,
    next_window_index,
    pane_cursor_state,
    pane_exists,
    read_pane,
)

pytestmark = [pytest.mark.integration, pytest.mark.tmux]

if shutil.which("tmux") is None:
    pytest.skip("tmux is required for tmux integration tests", allow_module_level=True)


@pytest.fixture
def managed_tmux_session(tmp_path):
    session = f"tmux-query-{uuid.uuid4().hex[:8]}"
    session_name = ensure_tmux_session(session, tmp_path)
    try:
        yield session, session_name, tmp_path
    finally:
        tmux("kill-session", "-t", session_name, check=False, capture=True)


def _wait_for_pane_output(pane_id: str, needle: str, *, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    last_capture = ""
    while time.time() < deadline:
        last_capture = read_pane(pane_id, 80)
        if needle in last_capture:
            return last_capture
        time.sleep(0.1)
    raise AssertionError(
        f"timed out waiting for {needle!r} in pane output:\n{last_capture}"
    )


def test_tmux_query_reads_real_pane_state(managed_tmux_session):
    session, session_name, cwd = managed_tmux_session
    pane = list_panes(session_name)[0]
    pane_id = pane["pane_id"]
    marker = f"ORCHE_TMUX_{uuid.uuid4().hex[:8]}"

    tmux(
        "send-keys",
        "-t",
        pane_id,
        f"printf '{marker}\\n'",
        "Enter",
        check=True,
        capture=True,
    )
    capture = _wait_for_pane_output(pane_id, marker)
    info = get_pane_info(pane_id)
    cursor = pane_cursor_state(pane_id)

    assert session_name in list_tmux_sessions()
    assert pane_exists(pane_id) is True
    assert marker in capture
    assert info is not None
    assert info["session_name"] == session_name
    assert info["pane_current_path"] == str(cwd)
    assert set(cursor) == {"cursor_x", "cursor_y", "pane_in_mode", "pane_dead"}
    assert cursor["pane_dead"] == "0"
    assert pane["window_name"] == window_name(session)


def test_ensure_tmux_session_is_idempotent_and_discovers_window(managed_tmux_session):
    session, session_name, _cwd = managed_tmux_session

    assert ensure_tmux_session(session, _cwd) == session_name

    window = find_window(window_name(session), target=session_name)

    assert window is not None
    assert window["session_name"] == session_name
    assert next_window_index(session_name) >= 1
