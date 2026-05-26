from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from babeltower_agent import session as session_mod
from babeltower_agent.config import (
    AgentIdentity,
    Config,
    LlmConfig,
    OwnerConfig,
    PolicyConfig,
)
from babeltower_agent.control import SessionEventStore, SessionRegistry
from babeltower_agent.crypto import generate_keypair
from babeltower_agent.llm import AgentBrain, MatchDecision
from babeltower_agent.session import (
    handoff_body,
    join_session,
    notify_owner,
    process_event,
    should_accept_counterparty_match,
    ws_url_for_server,
)


def _config(
    *,
    auto_approve_match: bool = False,
    webhook_url: str | None = None,
    default_handles: list[str] | None = None,
) -> Config:
    private_key, pubkey = generate_keypair()
    return Config(
        server_url="http://server",
        agent=AgentIdentity(pubkey=pubkey, private_key=private_key),
        llm=LlmConfig(api_key="${ANTHROPIC_API_KEY}"),
        owner=OwnerConfig(
            name="Test Owner",
            contact_handles={"calendly": "https://calendly/test", "email": "no@no"},
            handle_disclosure={
                "default": default_handles or ["calendly"],
                "on_request": [],
                "never": ["email"],
            },
        ),
        policy=PolicyConfig(
            auto_accept_connection_requests=False,
            auto_approve_match=auto_approve_match,
            max_conversation_turns=20,
            webhook_url=webhook_url,
        ),
    )


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class _ScriptedSocket:
    def __init__(self, inbound: list[str]) -> None:
        self.inbound: asyncio.Queue[str] = asyncio.Queue()
        for item in inbound:
            self.inbound.put_nowait(item)
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        return await self.inbound.get()

    def push(self, payload: dict[str, Any]) -> None:
        self.inbound.put_nowait(json.dumps(payload))


class _FakeClient:
    def __init__(self) -> None:
        self.proposed: list[str] = []
        self.accepted: list[str] = []
        self.rejected: list[tuple[str, str | None]] = []

    def propose_match(self, session_id: str) -> dict[str, Any]:
        self.proposed.append(session_id)
        return {"session_id": session_id, "match_status": "proposed"}

    def accept_match(self, session_id: str) -> dict[str, Any]:
        self.accepted.append(session_id)
        return {"session_id": session_id, "match_status": "confirmed"}

    def reject_match(self, session_id: str, reason: str | None = None) -> dict[str, Any]:
        self.rejected.append((session_id, reason))
        return {"session_id": session_id, "match_status": "rejected"}

    def end_session(self, session_id: str) -> None:
        self.ended = session_id


def test_ws_url_handles_http_https_and_bare() -> None:
    assert (
        ws_url_for_server("http://localhost:8000", "ses_x")
        == "ws://localhost:8000/v1/session/ses_x"
    )
    assert (
        ws_url_for_server("https://babel-tower.com/", "ses_x")
        == "wss://babel-tower.com/v1/session/ses_x"
    )
    assert ws_url_for_server("ws://internal/", "ses_x") == "ws://internal/v1/session/ses_x"


def test_handoff_body_only_includes_default_handles() -> None:
    body = handoff_body(_config(default_handles=["calendly"]))
    assert body["kind"] == "contact_handoff"
    assert body["handles"] == {"calendly": "https://calendly/test"}
    assert "email" not in body["handles"]


def test_should_accept_counterparty_match_tracks_policy_and_fit() -> None:
    good = MatchDecision("match", "goals and constraints align", 0.9)
    bad = MatchDecision("do_not_match", "constraints conflict", 0.9)
    assert should_accept_counterparty_match(_config(auto_approve_match=False), good) is False
    assert should_accept_counterparty_match(_config(auto_approve_match=True), good) is True
    assert should_accept_counterparty_match(_config(auto_approve_match=True), bad) is False


def test_notify_owner_prints_summary_and_skips_post_when_no_webhook(capsys) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_post(url, payload, timeout):
        calls.append((url, payload))

    def post_kw(url, json, timeout):  # noqa: A002 - matches real httpx kwarg name
        fake_post(url, json, timeout)

    notify_owner(_config(), {"event": "match_confirmed", "session_id": "ses_1"}, http_post=post_kw)
    out = capsys.readouterr().out
    assert "match_confirmed" in out
    assert calls == []


def test_notify_owner_posts_to_webhook_when_set() -> None:
    calls: list[tuple[str, dict]] = []

    def post_kw(url, json, timeout):  # noqa: A002
        calls.append((url, json))

    config = _config(webhook_url="http://hooks/test")
    notify_owner(config, {"event": "match_rejected", "session_id": "ses_1"}, http_post=post_kw)
    assert calls == [("http://hooks/test", {"event": "match_rejected", "session_id": "ses_1"})]


def test_notify_owner_swallows_webhook_failures(capsys) -> None:
    def post_kw(url, json, timeout):  # noqa: A002
        raise RuntimeError("network down")

    config = _config(webhook_url="http://hooks/test")
    # Must not raise.
    notify_owner(config, {"event": "x"}, http_post=post_kw)
    err = capsys.readouterr().err
    assert "webhook failed" in err


class _AlwaysProposeBrain(AgentBrain):
    def reply(self, transcript):
        return "canned reply"

    def should_propose_match(self, transcript):
        return True

    def evaluate_match(self, transcript):
        return MatchDecision("match", "test says yes", 0.9)


class _TranscriptEchoBrain(AgentBrain):
    def opening_message(self) -> str:
        return "opening"

    def reply(self, transcript):
        return f"seen: {transcript[-2]['body']}"

    def should_propose_match(self, transcript):
        return False

    def evaluate_match(self, transcript):
        return MatchDecision("uncertain", "test says no", 0.2)


class _NoFitBrain(AgentBrain):
    def reply(self, transcript):
        return "canned reply"

    def evaluate_match(self, transcript):
        return MatchDecision(
            "do_not_match",
            "Junior needs regular mentorship; senior needs an independent co-lead.",
            0.94,
        )


@pytest.mark.asyncio
async def test_process_event_message_proposes_match_when_brain_says_so() -> None:
    config = _config(auto_approve_match=True)
    client = _FakeClient()
    socket = _FakeSocket()
    brain = _AlwaysProposeBrain(config)
    transcript: list[dict[str, str]] = [
        {"from": "a", "body": "turn 1"},
        {"from": "b", "body": "turn 2"},
        {"from": "a", "body": "turn 3"},
    ]
    state: dict[str, Any] = {"proposed": False}

    keep = await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "message", "from": "pub_b", "body": {"text": "hi"}},
        transcript,
        state,
    )
    assert keep is True
    assert state["proposed"] is True
    assert client.proposed == ["ses_1"]
    # The reply was sent over the wire.
    assert any("canned reply" in payload for payload in socket.sent)


@pytest.mark.asyncio
async def test_process_event_message_does_not_propose_when_fit_judgment_says_no(capsys) -> None:
    config = _config(auto_approve_match=True)
    client = _FakeClient()
    socket = _FakeSocket()
    brain = _NoFitBrain(config)
    transcript: list[dict[str, str]] = [
        {"from": "a", "body": "same topic"},
        {"from": "b", "body": "same topic"},
        {"from": "a", "body": "needs regular mentorship"},
    ]
    state: dict[str, Any] = {"proposed": False}

    keep = await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {
            "type": "message",
            "from": "pub_b",
            "body": {"kind": "conversation", "text": "needs independent co-lead"},
        },
        transcript,
        state,
    )

    assert keep is True
    assert state["proposed"] is False
    assert client.proposed == []
    assert "match_not_proposed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_join_session_adds_control_injected_messages_to_brain_transcript(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config()
    config.policy.max_conversation_turns = 2
    client = _FakeClient()
    registry = SessionRegistry(SessionEventStore(tmp_path))
    socket = _ScriptedSocket([json.dumps({"type": "ready", "session_id": "ses_1"})])

    def fake_connect(url):
        assert url == "ws://server/v1/session/ses_1"
        return socket

    monkeypatch.setattr(session_mod.websockets, "connect", fake_connect)
    monkeypatch.setattr(session_mod, "AgentBrain", _TranscriptEchoBrain)

    task = asyncio.create_task(join_session(config, "ses_1", client=client, registry=registry))
    for _ in range(20):
        if registry.list_live():
            break
        await asyncio.sleep(0)

    registry.enqueue("ses_1", {"action": "send_message", "text": "human says invest now"})
    for _ in range(20):
        if any("human says invest now" in payload for payload in socket.sent):
            break
        await asyncio.sleep(0)
    socket.push(
        {
            "type": "message",
            "from": "counterparty",
            "body": {"kind": "conversation", "text": "replying to your injection"},
        }
    )
    # 0.2.2: the loop no longer exits on max_conversation_turns. Push an
    # explicit session_ended so this test can assert join_session returns.
    socket.push({"type": "session_ended", "body": {"reason": "test"}})
    await asyncio.wait_for(task, timeout=2)

    outbound = [json.loads(payload) for payload in socket.sent if "human says" in payload]
    assert any(payload["body"]["text"] == "human says invest now" for payload in outbound)
    assert any(
        payload["body"]["text"].startswith("seen:")
        and "human says invest now" in payload["body"]["text"]
        for payload in map(json.loads, socket.sent)
        if payload.get("body", {}).get("kind") == "conversation"
    )


@pytest.mark.asyncio
async def test_process_event_match_proposed_auto_accepts_when_policy_set(capsys) -> None:
    config = _config(auto_approve_match=True)
    client = _FakeClient()
    socket = _FakeSocket()
    brain = _AlwaysProposeBrain(config)
    state: dict[str, Any] = {"proposed": False}

    keep = await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "match_proposed", "body": {"proposed_by": "pub_b"}},
        [],
        state,
    )
    assert keep is True
    assert client.accepted == ["ses_1"]
    assert state["match_pending"] is True
    assert "match_proposed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_process_event_match_proposed_rejects_when_fit_judgment_says_no(capsys) -> None:
    config = _config(auto_approve_match=True)
    client = _FakeClient()
    socket = _FakeSocket()
    brain = _NoFitBrain(config)

    keep = await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "match_proposed", "body": {"proposed_by": "pub_b"}},
        [
            {"from": "a", "body": "same field"},
            {"from": "b", "body": "same field"},
            {"from": "a", "body": "needs regular guidance"},
            {"from": "b", "body": "can only do monthly async feedback"},
        ],
        {"proposed": False},
    )

    assert keep is True
    assert client.accepted == []
    assert client.rejected == [
        ("ses_1", "Junior needs regular mentorship; senior needs an independent co-lead.")
    ]
    out = capsys.readouterr().out
    assert "match_proposed" in out
    assert "match_not_accepted" in out


@pytest.mark.asyncio
async def test_process_event_match_proposed_does_not_accept_without_policy(capsys) -> None:
    config = _config(auto_approve_match=False)
    client = _FakeClient()
    socket = _FakeSocket()
    brain = AgentBrain(config)

    await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "match_proposed", "body": {"proposed_by": "pub_b"}},
        [],
        {"proposed": False},
    )
    assert client.accepted == []
    # Owner is still notified so they know a proposal is sitting open.
    assert "match_proposed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_process_event_match_confirmed_sends_handoff_and_notifies(capsys) -> None:
    config = _config()
    client = _FakeClient()
    socket = _FakeSocket()
    brain = AgentBrain(config)
    state: dict[str, Any] = {"proposed": True}

    await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "match_confirmed", "body": {"confirmed_at": "2026-05-21T00:00:00Z"}},
        [],
        state,
    )
    assert any("contact_handoff" in payload for payload in socket.sent)
    assert "match_confirmed" in capsys.readouterr().out
    # Regression for 0.2.2: the loop checks `state["sent_handoff"]` to know
    # when both halves of the handoff have been exchanged so it can close
    # the session politely. Without this, the loop could spin forever after
    # a confirmed match.
    assert state["sent_handoff"] is True
    assert state["match_confirmed"] is True
    assert state["match_pending"] is False
    assert state["proposed"] is True


@pytest.mark.asyncio
async def test_process_event_does_not_propose_after_counterparty_proposal() -> None:
    config = _config(auto_approve_match=True)
    client = _FakeClient()
    socket = _FakeSocket()
    brain = _AlwaysProposeBrain(config)
    transcript: list[dict[str, str]] = [
        {"from": "a", "body": "turn 1"},
        {"from": "b", "body": "turn 2"},
        {"from": "a", "body": "turn 3"},
        {"from": "b", "body": "turn 4"},
    ]
    state: dict[str, Any] = {"proposed": False}

    await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "match_proposed", "body": {"proposed_by": "pub_b"}},
        transcript,
        state,
    )
    await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "message", "from": "pub_b", "body": {"text": "one more thing"}},
        transcript,
        state,
    )

    assert client.accepted == ["ses_1"]
    assert client.proposed == []
    assert state["match_pending"] is True


@pytest.mark.asyncio
async def test_process_event_suppresses_duplicate_replies(capsys) -> None:
    config = _config()
    client = _FakeClient()
    socket = _FakeSocket()
    brain = _AlwaysProposeBrain(config)
    transcript: list[dict[str, str]] = []
    state: dict[str, Any] = {"proposed": False}

    for i in range(2):
        await process_event(
            config,
            client,
            brain,
            socket,
            "ses_1",
            {"type": "message", "from": "pub_b", "body": {"text": f"msg {i}"}},
            transcript,
            state,
        )

    assert sum("canned reply" in payload for payload in socket.sent) == 1
    assert "duplicate_reply_suppressed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_process_event_message_does_not_retry_propose_after_failure() -> None:
    """Regression for 0.2.2: when propose_match raises (e.g. server returns
    409 session_not_active because the counterparty already proposed and the
    match was confirmed), the agent must still mark `proposed=True` so it
    doesn't re-attempt on every subsequent inbound conversation message and
    spam the API."""
    config = _config(auto_approve_match=True)

    class _FailingClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.propose_calls = 0

        def propose_match(self, session_id: str):
            self.propose_calls += 1
            raise RuntimeError("POST /v1/match/propose failed: 409 session_not_active")

    client = _FailingClient()
    socket = _FakeSocket()
    brain = _AlwaysProposeBrain(config)
    transcript: list[dict[str, str]] = [
        {"from": "a", "body": "turn 1"},
        {"from": "b", "body": "turn 2"},
        {"from": "a", "body": "turn 3"},
    ]
    state: dict[str, Any] = {"proposed": False}

    # Three inbound conversation messages — pre-0.2.2, each one would call
    # propose_match because state["proposed"] was only set on success.
    for i in range(3):
        await process_event(
            config,
            client,
            brain,
            socket,
            "ses_1",
            {"type": "message", "from": "pub_b", "body": {"text": f"msg {i}"}},
            transcript,
            state,
        )

    assert client.propose_calls == 1
    assert state["proposed"] is True


@pytest.mark.asyncio
async def test_process_event_surfaces_counterparty_contact_handoff(capsys) -> None:
    """Regression: when the counterparty sends a contact_handoff message,
    the agent must notify the owner with the handles instead of feeding the
    body into the LLM transcript and replying conversationally. Without
    this, a confirmed match silently ends with the owner never seeing the
    other person's calendly/linkedin/etc — the whole product loop breaks."""
    config = _config()
    client = _FakeClient()
    socket = _FakeSocket()
    brain = AgentBrain(config)
    transcript: list[dict[str, str]] = []
    state: dict[str, Any] = {"proposed": True}

    handoff_event = {
        "type": "message",
        "from": "counterparty_pub",
        "body": {
            "kind": "contact_handoff",
            "handles": {
                "calendly": "https://calendly.com/marcus/intro",
                "linkedin": "https://linkedin.com/in/marcus",
            },
            "note": "Looking forward to talking.",
        },
    }
    keep = await process_event(
        config, client, brain, socket, "ses_1", handoff_event, transcript, state
    )

    assert keep is True
    # The counterparty's handles must appear in the owner notification.
    out = capsys.readouterr().out
    assert "counterparty_handoff" in out
    assert "calendly.com/marcus" in out
    # The agent must NOT have appended the handoff to the LLM transcript or
    # sent a chatty reply — handoff is not a conversation turn.
    assert transcript == []
    assert socket.sent == []
    # The state should record receipt so the rest of the loop can react.
    assert state["received_handoff"] is True


@pytest.mark.asyncio
async def test_process_event_session_ended_breaks_loop(capsys) -> None:
    config = _config()
    client = _FakeClient()
    socket = _FakeSocket()
    brain = AgentBrain(config)

    keep = await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "session_ended", "body": {"reason": "time_limit_reached"}},
        [],
        {"proposed": False},
    )
    assert keep is False
    assert "session_ended" in capsys.readouterr().out
