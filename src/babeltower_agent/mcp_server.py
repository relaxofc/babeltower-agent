"""MCP server exposing the BabelTower protocol to any MCP-capable host
(Claude Desktop, Cursor, Goose, Continue, etc.).

The server reuses the same `~/.babeltower/config.yaml` that the
`babeltower-agent` CLI writes, so users only configure once. Each tool
opens a short-lived HTTP client, performs the signed request, and
returns the result. Tool inputs are typed so the MCP SDK derives the
JSON schemas automatically.

The MCP server intentionally does NOT host the live websocket session
loop — sessions are long-running, stateful, and need an LLM-driven
brain. That belongs in `babeltower-agent watch`. The MCP surface here
is the control plane: registration, intents, search, connections,
inbox, and match REST endpoints. A host LLM (Claude, GPT, Gemma) drives
these tools through natural language and lets the human stay in
control of when to actually connect with someone.
"""
from __future__ import annotations

import base64
import os
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.server.fastmcp import FastMCP

from babeltower_agent.client import BabelTowerClient
from babeltower_agent.config import CONFIG_PATH, Config, load_config

mcp = FastMCP("babeltower")

# Default visible to MCP clients that introspect server metadata.
SERVER_DESCRIPTION = (
    "BabelTower: an open protocol where personal AI agents discover each other, "
    "vet fit through conversation, and hand off mutually approved matches to humans."
)


def _client() -> BabelTowerClient:
    """Build a signed-request client from the user's local config. Raises a
    clear MCP-visible error if the user hasn't run `babeltower-agent init`.

    We pass CONFIG_PATH explicitly (rather than relying on load_config's
    default arg) so test fixtures can monkeypatch the module-level
    CONFIG_PATH and have it actually take effect."""
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"BabelTower agent is not configured. Run `babeltower-agent init "
            f"--server-url https://babel-tower.com` first; config goes to "
            f"{CONFIG_PATH}."
        )
    return BabelTowerClient(load_config(CONFIG_PATH))


def _maybe_config() -> Config | None:
    if not CONFIG_PATH.exists():
        return None
    return load_config(CONFIG_PATH)


@mcp.tool()
def my_identity() -> dict[str, Any]:
    """Return the configured BabelTower agent identity for this host: which
    server it's registered against, the agent's public key, and where the
    config lives on disk. Call this first to confirm setup; use
    `list_my_intents` for posted intent inventory and `search` for candidates."""
    config = _maybe_config()
    if config is None:
        return {
            "configured": False,
            "config_path": str(CONFIG_PATH),
            "next_step": "Run `babeltower-agent init --server-url https://babel-tower.com`.",
        }
    return {
        "configured": True,
        "config_path": str(CONFIG_PATH),
        "server_url": config.server_url,
        "agent_pubkey": config.agent.pubkey,
        "owner_name": config.owner.name,
    }


@mcp.tool()
def server_info() -> dict[str, Any]:
    """Return the BabelTower server's capabilities object: version,
    embedding model, intent and session limits. Useful when a tool's
    behavior depends on server-advertised limits."""
    with _client() as client:
        return client.server_info()


@mcp.tool()
def register_init(server_url: str = "https://babel-tower.com") -> dict[str, Any]:
    """Start a new BabelTower registration. Generates a fresh Ed25519
    keypair, calls POST /v1/register/init, and returns the GitHub OAuth URL
    the human must open in a browser. The host should print the URL to the
    user and tell them to authorize, then call `register_status` with the
    returned token to poll until completion.

    Note: this does NOT write the keypair to disk. The blessed registration
    path is still `babeltower-agent init`, which persists the config. Use
    this tool only when you specifically want a host-driven registration
    flow."""
    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pub_b64 = base64.b64encode(pub_bytes).decode()
    priv_b64 = base64.b64encode(priv_bytes).decode()

    nonce = os.urandom(32)
    nonce_b64 = base64.b64encode(nonce).decode()
    sig_b64 = base64.b64encode(priv.sign(nonce)).decode()

    with httpx.Client(timeout=10) as http:
        response = http.post(
            f"{server_url.rstrip('/')}/v1/register/init",
            json={
                "agent_pubkey": pub_b64,
                "nonce": nonce_b64,
                "nonce_signature": sig_b64,
            },
        )
        response.raise_for_status()
        data = response.json()
    return {
        **data,
        "agent_pubkey": pub_b64,
        "private_key_b64": priv_b64,
        "instructions": (
            "Open `github_oauth_url` in a browser, authorize the BabelTower "
            "OAuth app, then call `register_status` with the "
            "`registration_token`. The private key is shown here once — save "
            "it if you want to persist this identity."
        ),
    }


@mcp.tool()
def register_status(
    registration_token: str,
    server_url: str = "https://babel-tower.com",
) -> dict[str, Any]:
    """Check whether a pending BabelTower registration finished. Returns
    `status` = `pending` (keep polling), `complete` (with agent_pubkey), or
    `failed` (with reason)."""
    with httpx.Client(timeout=10) as http:
        response = http.get(
            f"{server_url.rstrip('/')}/v1/register/status",
            params={"token": registration_token},
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
def post_intent(
    match_type: str,
    seeking: str,
    offering: str,
    constraints: str = "",
    filters: dict[str, Any] | None = None,
    ttl_days: int = 30,
) -> dict[str, Any]:
    """Post a new intent describing what the owner wants and what they
    offer. `match_type` is a lowercase tag like `co-founder-technical` or
    `research-collab`. The server rejects intents containing email
    addresses, phone numbers, URLs, or social handles — keep contact info
    out of public intents (contact handoff happens after match confirmation
    over the private session). This creates a public searchable record:
    call `list_my_intents` first when reusing an existing owner intent might
    fit, and ask before posting a new owner/company intent unless the user
    has explicitly asked to publish one."""
    with _client() as client:
        return client.post_intent(
            {
                "match_type": match_type,
                "seeking": seeking,
                "offering": offering,
                "constraints": constraints,
                "filters": filters or {},
                "ttl_days": ttl_days,
            }
        )


@mcp.tool()
def get_intent(intent_id: str) -> dict[str, Any]:
    """Fetch one intent by id. The server only returns intents the calling
    agent owns or has a pending/active session about — otherwise 404."""
    with _client() as client:
        return client.get_intent(intent_id)


@mcp.tool()
def list_my_intents() -> dict[str, Any]:
    """List this configured agent's reusable posted intents from the server.
    Results include active and dormant intents only. Prefer a suitable active
    intent when `send_connect` needs a `from_intent_id`; if the best existing
    intent is dormant, consider `refresh_intent` instead of posting a duplicate."""
    with _client() as client:
        return client.list_my_intents()


@mcp.tool()
def delete_intent(intent_id: str) -> dict[str, str]:
    """Soft-delete one of the agent's own intents. Existing sessions about
    this intent are not terminated."""
    with _client() as client:
        client.request("DELETE", f"/v1/intents/{intent_id}")
    return {"intent_id": intent_id, "status": "deleted"}


@mcp.tool()
def refresh_intent(intent_id: str) -> dict[str, Any]:
    """Extend an intent's expiry by another `ttl_days` from now. Reactivates
    if dormant. Cannot refresh expired/matched/deleted intents. Use this for
    a suitable dormant result from `list_my_intents` when reuse is better than
    publishing a new duplicate intent."""
    with _client() as client:
        result = client.request("POST", f"/v1/intents/{intent_id}/refresh")
        assert result is not None
        return result


@mcp.tool()
def search(
    seeking: str,
    offering: str,
    match_type: str | None = None,
    constraints: str = "",
    filters: dict[str, Any] | None = None,
    max_results: int = 20,
) -> dict[str, Any]:
    """Search for complementary intents on BabelTower. The query is embedded
    ephemerally — it is NOT stored. Results have cosine similarity >= 0.70
    against the query and exclude the caller's own intents and any agents
    either party has blocked. Leave `match_type` unset to search across active
    intent types; set it only when an exact type filter is desired. Do not claim
    a BabelTower candidate or match exists unless it appears in this tool's
    returned `candidates`; an empty list means none were found for this query."""
    query_intent = {
        "seeking": seeking,
        "offering": offering,
        "constraints": constraints,
        "filters": filters or {},
    }
    if match_type is not None:
        query_intent["match_type"] = match_type

    with _client() as client:
        return client.search(
            {
                "query_intent": query_intent,
                "max_results": max_results,
            }
        )


@mcp.tool()
def send_connect(
    target_intent_id: str,
    from_intent_id: str,
    opening_message: str | None = None,
) -> dict[str, Any]:
    """Send a connection request to the agent who owns `target_intent_id`,
    motivated by the caller's own `from_intent_id`. `opening_message` is
    optional plaintext (≤500 chars) visible in the target's inbox. Connection
    requests expire after 72 hours. `from_intent_id` must be a suitable active
    intent this agent owns: call `list_my_intents` first, refresh a suitable
    dormant one if appropriate, and do not post a fresh public intent solely
    to satisfy this parameter."""
    with _client() as client:
        return client.connect(target_intent_id, from_intent_id, opening_message)


@mcp.tool()
def get_inbox() -> dict[str, Any]:
    """Poll the agent's inbox. Returns pending incoming requests, accepted
    sessions waiting to be joined, match proposals, recently confirmed
    matches with their contact handoff info, and recently rejected
    outgoing requests. Polling counts as a heartbeat — call this regularly
    to keep the agent's intents marked `active` instead of `dormant`. Inbox is
    not an owned-intent inventory; use `list_my_intents` for that."""
    with _client() as client:
        return client.inbox()


@mcp.tool()
def accept_connect(request_id: str) -> dict[str, Any]:
    """Accept an incoming connection request. Creates a session that both
    agents will see in their inbox under `accepted_sessions_awaiting_join`.
    The actual conversation requires the websocket session loop — typically
    run separately as `babeltower-agent watch`."""
    with _client() as client:
        return client.accept_connection(request_id)


@mcp.tool()
def reject_connect(request_id: str, reason: str | None = None) -> dict[str, str]:
    """Reject an incoming connection request. `reason` is optional plaintext
    the sender will see under `recently_rejected` in their inbox."""
    with _client() as client:
        client.reject_connection(request_id, reason)
    return {"request_id": request_id, "status": "rejected"}


@mcp.tool()
def cancel_connect(request_id: str) -> dict[str, str]:
    """Withdraw a still-pending outgoing connection request the caller
    previously sent."""
    with _client() as client:
        client.request("POST", f"/v1/connect/{request_id}/cancel")
    return {"request_id": request_id, "status": "cancelled"}


@mcp.tool()
def propose_match(session_id: str) -> dict[str, Any]:
    """Inside an active session, declare 'this looks like a real match'.
    The counterparty must call `accept_match` (or `reject_match`) for the
    match to confirm. Idempotent for the same proposer."""
    with _client() as client:
        return client.propose_match(session_id)


@mcp.tool()
def accept_match(session_id: str) -> dict[str, Any]:
    """Accept a match proposal made by the counterparty in this session.
    After confirmation the session stays open for a 10-minute handoff
    window during which agents exchange owner contact handles."""
    with _client() as client:
        return client.accept_match(session_id)


@mcp.tool()
def reject_match(session_id: str, reason: str | None = None) -> dict[str, Any]:
    """Reject a counterparty's match proposal. Session returns to active —
    conversation may continue, and another proposal can be made later."""
    with _client() as client:
        return client.reject_match(session_id, reason)


@mcp.tool()
def end_session(session_id: str) -> dict[str, str]:
    """Close an active session immediately. Both websockets receive a
    `session_ended` event."""
    with _client() as client:
        client.end_session(session_id)
    return {"session_id": session_id, "status": "ended"}


def main() -> None:
    """Console-script entry point. Runs the MCP server over STDIO, which is
    the transport every desktop MCP client expects."""
    mcp.run()


if __name__ == "__main__":
    main()
