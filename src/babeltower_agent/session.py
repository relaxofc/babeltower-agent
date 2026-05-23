from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

import httpx
import websockets

from babeltower_agent.client import BabelTowerClient
from babeltower_agent.config import Config
from babeltower_agent.control import SessionEventStore, SessionRegistry
from babeltower_agent.crypto import (
    json_bytes,
    utc_timestamp,
    websocket_hello_signature,
    websocket_message_signature,
)
from babeltower_agent.llm import AgentBrain

EventRecorder = Callable[[str, dict[str, Any]], None]


def ws_url_for_server(server_url: str, session_id: str) -> str:
    base = server_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    return f"{base}/v1/session/{session_id}"


def message_envelope(config: Config, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    timestamp = utc_timestamp()
    return {
        "type": "message",
        "session_id": session_id,
        "from": config.agent.pubkey,
        "timestamp": timestamp,
        "body": body,
        "signature": websocket_message_signature(
            config.agent.private_key,
            session_id,
            timestamp,
            body,
        ),
    }


def handoff_body(config: Config) -> dict[str, Any]:
    handles = {}
    for name in config.owner.handle_disclosure.get("default", []):
        if name in config.owner.contact_handles:
            handles[name] = config.owner.contact_handles[name]
    return {
        "kind": "contact_handoff",
        "handles": handles,
        "note": f"{config.owner.name} has approved sharing these contact handles.",
    }


def signed_body_size(body: dict[str, Any]) -> int:
    return len(json_bytes(body))


def notify_owner(
    config: Config,
    payload: dict[str, Any],
    http_post=httpx.post,
) -> None:
    """Print a clear owner-facing summary to stdout, and POST to the
    configured webhook if one is set. Webhook failures are reported on
    stderr but never raised — the agent's protocol behavior must not
    depend on the owner's notification channel."""
    summary = json.dumps(payload, indent=2, sort_keys=True)
    print(f"[babeltower owner notification]\n{summary}", flush=True)
    webhook_url = config.policy.webhook_url
    if not webhook_url:
        return
    try:
        http_post(webhook_url, json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001 - notifications are best-effort
        print(f"[babeltower webhook failed] {exc}", file=sys.stderr, flush=True)


def should_accept_counterparty_match(config: Config) -> bool:
    """Default policy: only auto-accept a match the counterparty proposed
    if the owner has already opted into auto-approve. Otherwise the
    proposal is logged for the owner and the session is left to time
    out, which is conservative and reversible (the counterparty can
    propose again in a future session)."""
    return config.policy.auto_approve_match


async def send_json(
    websocket,
    payload: dict[str, Any],
    recorder: EventRecorder | None = None,
) -> None:
    await websocket.send(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    if recorder is not None:
        recorder("outbound", payload)


async def _handle_message_event(
    config: Config,
    client: BabelTowerClient,
    brain: AgentBrain,
    websocket,
    session_id: str,
    event: dict[str, Any],
    transcript: list[dict[str, str]],
    state: dict[str, Any],
    recorder: EventRecorder | None = None,
) -> None:
    body = event.get("body", {})

    # Phase 11.6: the counterparty's contact_handoff message is the whole
    # point of the match. Surface the handles to the owner immediately
    # instead of letting the LLM generate a conversational reply to it.
    # Without this, a confirmed match completes with the owner never
    # receiving the contact info — the value loop silently breaks.
    if body.get("kind") == "contact_handoff":
        notify_owner(
            config,
            {
                "event": "counterparty_handoff",
                "session_id": session_id,
                "counterparty_pubkey": event.get("from"),
                "handles": body.get("handles") or {},
                "note": body.get("note"),
            },
        )
        state["received_handoff"] = True
        return

    transcript.append({"from": event.get("from", ""), "body": json.dumps(body)})
    await asyncio.sleep(1)
    await send_json(
        websocket,
        message_envelope(
            config,
            session_id,
            {"kind": "conversation", "text": brain.reply(transcript)},
        ),
        recorder,
    )
    if (
        not state.get("proposed")
        and brain.should_propose_match(transcript)
    ):
        try:
            client.propose_match(session_id)
            state["proposed"] = True
        except Exception as exc:  # noqa: BLE001 - REST failure shouldn't kill the loop
            print(f"[babeltower propose_match failed] {exc}", file=sys.stderr, flush=True)


async def _handle_match_proposed(
    config: Config,
    client: BabelTowerClient,
    websocket,
    session_id: str,
    event: dict[str, Any],
    recorder: EventRecorder | None = None,
) -> None:
    proposed_by = event.get("body", {}).get("proposed_by")
    notify_owner(
        config,
        {
            "event": "match_proposed",
            "session_id": session_id,
            "proposed_by": proposed_by,
        },
    )
    if should_accept_counterparty_match(config):
        try:
            client.accept_match(session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[babeltower accept_match failed] {exc}", file=sys.stderr, flush=True)
    # If we don't auto-accept and the owner hasn't intervened by the time
    # the session's 30-min wall clock runs out, the server will close it
    # with reason `time_limit_reached` — a deliberate, reversible default.
    del websocket  # reserved for future "send a polite holding message" use
    del recorder


async def _handle_match_confirmed(
    config: Config,
    websocket,
    session_id: str,
    event: dict[str, Any],
    recorder: EventRecorder | None = None,
) -> None:
    await send_json(
        websocket,
        message_envelope(config, session_id, handoff_body(config)),
        recorder,
    )
    notify_owner(
        config,
        {
            "event": "match_confirmed",
            "session_id": session_id,
            "details": event.get("body", {}),
        },
    )


async def _handle_match_rejected(
    config: Config,
    session_id: str,
    event: dict[str, Any],
) -> None:
    notify_owner(
        config,
        {
            "event": "match_rejected",
            "session_id": session_id,
            "details": event.get("body", {}),
        },
    )


async def process_event(
    config: Config,
    client: BabelTowerClient,
    brain: AgentBrain,
    websocket,
    session_id: str,
    event: dict[str, Any],
    transcript: list[dict[str, str]],
    state: dict[str, Any],
    recorder: EventRecorder | None = None,
) -> bool:
    """Dispatch one server event. Returns False if the loop should exit."""
    event_type = event.get("type")
    if event_type == "message":
        await _handle_message_event(
            config, client, brain, websocket, session_id, event, transcript, state, recorder
        )
        return True
    if event_type == "match_proposed":
        await _handle_match_proposed(config, client, websocket, session_id, event, recorder)
        return True
    if event_type == "match_confirmed":
        await _handle_match_confirmed(config, websocket, session_id, event, recorder)
        return True
    if event_type == "match_rejected":
        await _handle_match_rejected(config, session_id, event)
        return True
    if event_type in {"session_ended", "error"}:
        notify_owner(
            config,
            {"event": event_type, "session_id": session_id, "details": event.get("body", {})},
        )
        return False
    return True


async def join_session(
    config: Config,
    session_id: str,
    client: BabelTowerClient | None = None,
    registry: SessionRegistry | None = None,
    event_store: SessionEventStore | None = None,
) -> None:
    owns_client = client is None
    if client is None:
        client = BabelTowerClient(config)
    brain = AgentBrain(config)
    transcript: list[dict[str, str]] = []
    state: dict[str, Any] = {"proposed": False}
    url = ws_url_for_server(config.server_url, session_id)
    store = event_store or (registry.store if registry is not None else None)

    def record(direction: str, event: dict[str, Any]) -> None:
        if store is not None:
            store.append(session_id, direction, event)
        if registry is not None:
            registry.touch(session_id)

    async def handle_control_command(websocket, command: dict[str, Any]) -> bool:
        action = command.get("action")
        if action == "send_message":
            body = {"kind": "conversation", "text": str(command.get("text", ""))}
            await send_json(
                websocket,
                message_envelope(config, session_id, body),
                record,
            )
            transcript.append({"from": config.agent.pubkey, "body": json.dumps(body)})
            return True
        if action == "send_handoff":
            handles = command.get("handles")
            body = (
                handoff_body(config)
                if handles is None
                else {
                    "kind": "contact_handoff",
                    "handles": handles,
                    "note": command.get("note")
                    or f"{config.owner.name} has approved sharing these contact handles.",
                }
            )
            await send_json(websocket, message_envelope(config, session_id, body), record)
            return True
        if action == "end_session":
            client.end_session(session_id)
            return False
        return True

    try:
        async with websockets.connect(url) as websocket:
            timestamp = utc_timestamp()
            await send_json(
                websocket,
                {
                    "type": "hello",
                    "agent_pubkey": config.agent.pubkey,
                    "session_id": session_id,
                    "timestamp": timestamp,
                    "signature": websocket_hello_signature(
                        config.agent.private_key,
                        session_id,
                        timestamp,
                    ),
                },
            )
            first = json.loads(await websocket.recv())
            if first.get("type") != "ready":
                raise RuntimeError(f"session {session_id} did not become ready: {first}")
            record("inbound", first)

            control_queue: asyncio.Queue[dict[str, Any]] | None = None
            if registry is not None:
                control_queue = asyncio.Queue()
                registry.register(session_id, asyncio.get_running_loop(), control_queue)

            await send_json(
                websocket,
                message_envelope(
                    config,
                    session_id,
                    {"kind": "conversation", "text": brain.opening_message()},
                ),
                record,
            )

            websocket_task = asyncio.create_task(websocket.recv())
            control_task = (
                asyncio.create_task(control_queue.get()) if control_queue is not None else None
            )
            while len(transcript) < config.policy.max_conversation_turns:
                pending = {websocket_task}
                if control_task is not None:
                    pending.add(control_task)
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if websocket_task in done:
                    raw = websocket_task.result()
                    websocket_task = asyncio.create_task(websocket.recv())
                    event = json.loads(raw)
                    record("inbound", event)
                    keep_going = await process_event(
                        config,
                        client,
                        brain,
                        websocket,
                        session_id,
                        event,
                        transcript,
                        state,
                        record,
                    )
                    if not keep_going:
                        return
                if control_task is not None and control_task in done:
                    command = control_task.result()
                    control_task = asyncio.create_task(control_queue.get())
                    keep_going = await handle_control_command(websocket, command)
                    if not keep_going:
                        return
    finally:
        for task_name in ("websocket_task", "control_task"):
            task = locals().get(task_name)
            if task is not None:
                task.cancel()
        if registry is not None:
            registry.unregister(session_id)
        if owns_client:
            client.close()
