from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from babeltower_agent.control import (
    ControllerUnavailable,
    SessionEventStore,
    SessionRegistry,
    WatchController,
    control_request,
)


def test_event_store_persists_events_and_handoffs(tmp_path) -> None:
    store = SessionEventStore(tmp_path)

    first = store.append("ses_1", "inbound", {"type": "ready", "session_id": "ses_1"})
    second = store.append(
        "ses_1",
        "inbound",
        {
            "type": "message",
            "body": {"kind": "contact_handoff", "handles": {"email": "a@example.test"}},
        },
    )

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert [item["seq"] for item in store.read("ses_1")] == [1, 2]
    assert store.read("ses_1", since_seq=1)[0]["event"]["body"]["kind"] == "contact_handoff"
    assert store.list_sessions() == [
        {
            "session_id": "ses_1",
            "last_seq": 2,
            "last_recorded_at": second["recorded_at"],
            "has_handoff": True,
        }
    ]
    assert store.list_handoffs()[0]["session_id"] == "ses_1"


@pytest.mark.asyncio
async def test_registry_enqueue_delivers_to_session_loop(tmp_path) -> None:
    registry = SessionRegistry(SessionEventStore(tmp_path))
    queue: asyncio.Queue[dict] = asyncio.Queue()
    registry.register("ses_1", asyncio.get_running_loop(), queue)

    registry.enqueue("ses_1", {"action": "send_message", "text": "hi"})

    assert await asyncio.wait_for(queue.get(), timeout=1) == {
        "action": "send_message",
        "text": "hi",
    }


@pytest.mark.asyncio
async def test_watch_controller_uses_unix_socket_and_reports_live_sessions() -> None:
    short_dir = Path(tempfile.mkdtemp(prefix="bt-", dir="/tmp"))
    registry = SessionRegistry(SessionEventStore(short_dir / "sessions"))
    queue: asyncio.Queue[dict] = asyncio.Queue()
    registry.register("ses_1", asyncio.get_running_loop(), queue)
    controller = WatchController(registry, short_dir / "control.sock")

    try:
        controller.start()
        response = control_request({"action": "list_sessions"}, short_dir / "control.sock")
        assert response["sessions"][0]["session_id"] == "ses_1"
        assert response["sessions"][0]["live"] is True

        sent = control_request(
            {"action": "send_message", "session_id": "ses_1", "text": "hello"},
            short_dir / "control.sock",
        )
        assert sent["queued"] == "send_message"
        assert (await asyncio.wait_for(queue.get(), timeout=1))["text"] == "hello"

        with pytest.raises(RuntimeError) as exc_info:
            control_request(
                {"action": "send_message", "session_id": "ses_missing", "text": "hello"},
                short_dir / "control.sock",
            )
        assert "not live" in str(exc_info.value)
    finally:
        controller.stop()
        shutil.rmtree(short_dir, ignore_errors=True)


def test_control_request_fails_clearly_when_watch_not_running(tmp_path) -> None:
    with pytest.raises(ControllerUnavailable) as exc_info:
        control_request({"action": "list_sessions"}, tmp_path / "missing.sock")
    assert "watch controller is not running" in str(exc_info.value)
