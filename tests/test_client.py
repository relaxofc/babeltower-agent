import json

import httpx

from babeltower_agent.client import BabelTowerClient
from babeltower_agent.config import new_config


def test_signed_request_adds_protocol_headers() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["pubkey"] = request.headers["X-Agent-Pubkey"]
        seen["timestamp"] = request.headers["X-Timestamp"]
        seen["signature"] = request.headers["X-Signature"]
        return httpx.Response(200, json={"pending_requests": []})

    config = new_config("http://testserver")
    client = BabelTowerClient(config, transport=httpx.MockTransport(handler))

    response = client.inbox()

    assert response == {"pending_requests": []}
    assert seen["pubkey"] == config.agent.pubkey
    assert seen["timestamp"].endswith("Z")
    assert len(seen["signature"]) > 40


def test_post_intent_sends_compact_json_body() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        payload = json.loads(request.content)
        return httpx.Response(201, json={**payload, "intent_id": "int_test", "status": "active"})

    config = new_config("http://testserver")
    client = BabelTowerClient(config, transport=httpx.MockTransport(handler))

    result = client.post_intent(
        {
            "match_type": "co-founder-technical",
            "seeking": "business co-founder",
            "offering": "technical build",
            "constraints": "",
            "filters": {},
            "ttl_days": 30,
        }
    )

    assert result["intent_id"] == "int_test"
    assert b": " not in bodies[0]
    assert b", " not in bodies[0]
