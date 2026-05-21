from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from babeltower_agent.config import Config
from babeltower_agent.crypto import (
    json_bytes,
    utc_timestamp,
    websocket_hello_signature,
    websocket_message_signature,
)
from babeltower_agent.llm import AgentBrain


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


async def send_json(websocket: websockets.ClientConnection, payload: dict[str, Any]) -> None:
    await websocket.send(json.dumps(payload, separators=(",", ":"), sort_keys=True))


async def join_session(config: Config, session_id: str) -> None:
    brain = AgentBrain(config)
    transcript: list[dict[str, str]] = []
    url = ws_url_for_server(config.server_url, session_id)
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

        await send_json(
            websocket,
            message_envelope(
                config,
                session_id,
                {"kind": "conversation", "text": brain.opening_message()},
            ),
        )

        while len(transcript) < config.policy.max_conversation_turns:
            raw = await websocket.recv()
            event = json.loads(raw)
            event_type = event.get("type")
            if event_type == "message":
                body = event.get("body", {})
                transcript.append({"from": event.get("from", ""), "body": json.dumps(body)})
                await asyncio.sleep(1)
                await send_json(
                    websocket,
                    message_envelope(
                        config,
                        session_id,
                        {"kind": "conversation", "text": brain.reply(transcript)},
                    ),
                )
            elif event_type == "match_confirmed":
                await send_json(
                    websocket,
                    message_envelope(config, session_id, handoff_body(config)),
                )
            elif event_type in {"session_ended", "error"}:
                return


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
