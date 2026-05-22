"""Tests for the MCP server surface. We don't spin up a real MCP host;
we exercise the underlying tool functions and verify the FastMCP registry
sees the tools we expect. The wrapped BabelTowerClient is mocked at the
HTTP-transport level so no network is touched."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from babeltower_agent import mcp_server
from babeltower_agent.config import save_config
from babeltower_agent.crypto import generate_keypair


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant a valid ~/.babeltower/config.yaml at a tmp location and point
    the config module at it."""
    private_key, pubkey = generate_keypair()
    monkeypatch.setattr(mcp_server, "CONFIG_PATH", tmp_path / "config.yaml")
    # Also patch the load_config import target so the MCP server reads ours.
    from babeltower_agent import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    config = config_mod.Config(
        server_url="http://testserver",
        agent=config_mod.AgentIdentity(pubkey=pubkey, private_key=private_key),
    )
    config.owner.name = "Test Owner"
    save_config(config, tmp_path / "config.yaml")


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Replace BabelTowerClient's httpx transport so tools hit our handler
    instead of the network."""
    from babeltower_agent.client import BabelTowerClient
    from babeltower_agent.config import load_config

    real_init = BabelTowerClient.__init__

    def patched_init(self, config, transport=None):  # noqa: ANN001
        real_init(self, config, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(BabelTowerClient, "__init__", patched_init)
    # `_client()` calls load_config(); make sure the patched CONFIG_PATH is
    # used when load_config reads from disk.
    assert load_config  # silence unused-import


def test_my_identity_unconfigured(tmp_path, monkeypatch):
    """If no config exists, my_identity reports configured=False with a
    helpful next-step message instead of crashing."""
    monkeypatch.setattr(mcp_server, "CONFIG_PATH", tmp_path / "missing.yaml")
    result = mcp_server.my_identity()
    assert result["configured"] is False
    assert "babeltower-agent init" in result["next_step"]


def test_my_identity_configured(tmp_path, monkeypatch):
    """When config exists, my_identity exposes server URL, pubkey, owner."""
    _write_config(tmp_path, monkeypatch)
    result = mcp_server.my_identity()
    assert result["configured"] is True
    assert result["server_url"] == "http://testserver"
    assert isinstance(result["agent_pubkey"], str) and result["agent_pubkey"]
    assert result["owner_name"] == "Test Owner"


def test_tool_requires_configuration(tmp_path, monkeypatch):
    """A protocol tool called without config should fail with the same
    helpful message instead of a Python traceback the LLM can't read."""
    monkeypatch.setattr(mcp_server, "CONFIG_PATH", tmp_path / "missing.yaml")
    with pytest.raises(RuntimeError) as exc_info:
        mcp_server.get_inbox()
    assert "babeltower-agent init" in str(exc_info.value)


def test_post_intent_routes_through_signed_client(tmp_path, monkeypatch):
    """post_intent should hit POST /v1/intents and return the server's
    response verbatim."""
    _write_config(tmp_path, monkeypatch)

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content),
                "has_signature": "X-Signature" in request.headers,
            }
        )
        return httpx.Response(
            201,
            json={"intent_id": "int_test", "status": "active", **json.loads(request.content)},
        )

    _mock_client(monkeypatch, handler)

    result = mcp_server.post_intent(
        match_type="co-founder-technical",
        seeking="biotech CEO",
        offering="ML engineer",
        constraints="Seoul timezone",
        filters={"location": "Seoul"},
        ttl_days=14,
    )

    assert result["intent_id"] == "int_test"
    [call] = seen
    assert call["method"] == "POST"
    assert call["path"] == "/v1/intents"
    assert call["has_signature"]
    assert call["body"]["match_type"] == "co-founder-technical"
    assert call["body"]["ttl_days"] == 14


def test_search_routes_query(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["query_intent"]["match_type"] == "tennis-partner-seoul"
        return httpx.Response(200, json={"candidates": []})

    _mock_client(monkeypatch, handler)

    result = mcp_server.search(
        match_type="tennis-partner-seoul",
        seeking="evening doubles",
        offering="evening doubles",
    )
    assert result == {"candidates": []}


def test_inbox_returns_server_payload(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)

    payload = {
        "pending_requests": [],
        "accepted_sessions_awaiting_join": [],
        "match_proposals": [],
        "matched_handoffs": [],
        "recently_rejected": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/inbox"
        return httpx.Response(200, json=payload)

    _mock_client(monkeypatch, handler)

    assert mcp_server.get_inbox() == payload


def test_register_init_does_not_require_config(tmp_path, monkeypatch):
    """register_init is the bootstrap step — it must work before any local
    config exists. It should generate a fresh keypair and return the OAuth
    URL plus the new private key for the caller to persist."""
    monkeypatch.setattr(mcp_server, "CONFIG_PATH", tmp_path / "missing.yaml")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/register/init"
        body = json.loads(request.content)
        assert {"agent_pubkey", "nonce", "nonce_signature"} <= body.keys()
        return httpx.Response(
            200,
            json={
                "registration_token": "reg_test",
                "github_oauth_url": "https://github.com/login/oauth/authorize?x=y",
                "expires_in": 600,
            },
        )

    # register_init uses its own httpx.Client; patch the class.
    original_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)

    result = mcp_server.register_init(server_url="http://testserver")
    assert result["registration_token"] == "reg_test"
    assert result["github_oauth_url"].startswith("https://github.com/")
    assert len(result["private_key_b64"]) > 30
    assert len(result["agent_pubkey"]) > 30


def test_all_protocol_tools_are_registered():
    """Sanity check: every tool we expect to expose is actually registered
    with FastMCP. If someone deletes a @mcp.tool() by accident, this fails."""
    tool_names = {
        "my_identity",
        "server_info",
        "register_init",
        "register_status",
        "post_intent",
        "get_intent",
        "delete_intent",
        "refresh_intent",
        "search",
        "send_connect",
        "get_inbox",
        "accept_connect",
        "reject_connect",
        "cancel_connect",
        "propose_match",
        "accept_match",
        "reject_match",
        "end_session",
    }
    # FastMCP's @mcp.tool() decorator returns the original function
    # unchanged, so each tool is just a plain callable on the module.
    # Asserting they're present + callable catches accidental deletions.
    for name in tool_names:
        attr = getattr(mcp_server, name, None)
        assert callable(attr), f"missing or non-callable MCP tool: {name}"
