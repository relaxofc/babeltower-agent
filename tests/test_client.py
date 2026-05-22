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


def test_list_my_intents_uses_signed_owned_intents_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"intents": []})

    config = new_config("http://testserver")
    client = BabelTowerClient(config, transport=httpx.MockTransport(handler))

    assert client.list_my_intents() == {"intents": []}

    [request] = seen
    assert request.method == "GET"
    assert request.url.path == "/v1/intents/mine"
    assert "x-signature" in request.headers


def test_unsigned_register_init_sends_json_content_type() -> None:
    """Regression: the register_init flow uses signed=False. Before this
    test, the request() helper sent an empty headers dict on that path,
    which meant no Content-Type header — FastAPI/Pydantic then parsed the
    body as a string and returned 422 'Input should be a valid dictionary
    or object'. Every CLI registration attempt against the live server
    failed silently with this bug. The fix: include Content-Type even
    when signed=False."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "registration_token": "reg_test",
                "github_oauth_url": "https://github.com/login/oauth/authorize?x=y",
                "expires_in": 600,
            },
        )

    config = new_config("http://testserver")
    client = BabelTowerClient(config, transport=httpx.MockTransport(handler))

    client.register_init()

    [request] = seen
    assert request.method == "POST"
    assert request.url.path == "/v1/register/init"
    assert request.headers.get("content-type") == "application/json", (
        "unsigned POSTs must still send Content-Type: application/json so "
        "FastAPI parses the body as JSON instead of a raw string"
    )
    # And it must NOT carry the signature headers — those are reserved
    # for the signed path.
    assert "x-signature" not in request.headers
    assert "x-agent-pubkey" not in request.headers
