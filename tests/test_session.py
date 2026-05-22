from __future__ import annotations

from typing import Any

import pytest

from babeltower_agent.config import (
    AgentIdentity,
    Config,
    LlmConfig,
    OwnerConfig,
    PolicyConfig,
)
from babeltower_agent.crypto import generate_keypair
from babeltower_agent.llm import AgentBrain
from babeltower_agent.session import (
    handoff_body,
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


def test_should_accept_counterparty_match_tracks_policy() -> None:
    assert should_accept_counterparty_match(_config(auto_approve_match=False)) is False
    assert should_accept_counterparty_match(_config(auto_approve_match=True)) is True


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


@pytest.mark.asyncio
async def test_process_event_message_proposes_match_when_brain_says_so() -> None:
    config = _config()
    client = _FakeClient()
    socket = _FakeSocket()
    brain = _AlwaysProposeBrain(config)
    transcript: list[dict[str, str]] = []
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
async def test_process_event_match_proposed_auto_accepts_when_policy_set(capsys) -> None:
    config = _config(auto_approve_match=True)
    client = _FakeClient()
    socket = _FakeSocket()
    brain = AgentBrain(config)

    keep = await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "match_proposed", "body": {"proposed_by": "pub_b"}},
        [],
        {"proposed": False},
    )
    assert keep is True
    assert client.accepted == ["ses_1"]
    assert "match_proposed" in capsys.readouterr().out


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

    await process_event(
        config,
        client,
        brain,
        socket,
        "ses_1",
        {"type": "match_confirmed", "body": {"confirmed_at": "2026-05-21T00:00:00Z"}},
        [],
        {"proposed": True},
    )
    assert any("contact_handoff" in payload for payload in socket.sent)
    assert "match_confirmed" in capsys.readouterr().out


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
