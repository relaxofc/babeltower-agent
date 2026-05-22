from __future__ import annotations

import base64
import os
import webbrowser
from typing import Any
from urllib.parse import urlencode

import httpx

from babeltower_agent.config import Config
from babeltower_agent.crypto import (
    json_bytes,
    request_signature,
    sign,
    utc_timestamp,
)


class BabelTowerClient:
    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.http = httpx.Client(
            base_url=config.server_url,
            timeout=30,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> BabelTowerClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _signed_headers(self, method: str, path_with_query: str, body: bytes) -> dict[str, str]:
        timestamp = utc_timestamp()
        signature = request_signature(
            self.config.agent.private_key,
            method,
            path_with_query,
            timestamp,
            body,
        )
        return {
            "X-Agent-Pubkey": self.config.agent.pubkey,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        signed: bool = True,
    ) -> dict[str, Any] | None:
        # Build the full URL once and use it for both signing and the HTTP call.
        # Passing path + params separately to httpx would let it re-encode the
        # query string, which would diverge from our signed canonical path and
        # cause the server's signature verification to fail.
        query = f"?{urlencode(params)}" if params else ""
        path_with_query = f"{path}{query}"
        body = json_bytes(json)
        # Even on unsigned endpoints (register_init / status) we still need
        # Content-Type: application/json. httpx's `content=` kwarg does NOT
        # set it automatically (unlike `json=`), and FastAPI/Pydantic v2
        # parses the body as a raw string instead of a JSON object when the
        # header is missing — which returned a 422 "Input should be a valid
        # dictionary or object" for every CLI registration attempt before
        # this fix.
        headers = (
            self._signed_headers(method, path_with_query, body)
            if signed
            else {"Content-Type": "application/json"}
        )
        response = self.http.request(method, path_with_query, content=body, headers=headers)
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {path_with_query} failed: {response.status_code} {response.text}"
            )
        return response.json()

    def register_init(self) -> dict[str, Any]:
        nonce = os.urandom(32)
        nonce_b64 = base64.b64encode(nonce).decode("ascii")
        payload = {
            "agent_pubkey": self.config.agent.pubkey,
            "nonce": nonce_b64,
            "nonce_signature": sign(self.config.agent.private_key, nonce),
        }
        result = self.request("POST", "/v1/register/init", json=payload, signed=False)
        assert result is not None
        return result

    def open_registration_browser(self, url: str) -> None:
        webbrowser.open(url)

    def registration_status(self, token: str) -> dict[str, Any]:
        result = self.request(
            "GET",
            "/v1/register/status",
            params={"token": token},
            signed=False,
        )
        assert result is not None
        return result

    def post_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", "/v1/intents", json=payload)
        assert result is not None
        return result

    def get_intent(self, intent_id: str) -> dict[str, Any]:
        result = self.request("GET", f"/v1/intents/{intent_id}")
        assert result is not None
        return result

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", "/v1/search", json=payload)
        assert result is not None
        return result

    def connect(
        self,
        target_intent_id: str,
        from_intent_id: str,
        opening_message: str | None = None,
    ) -> dict[str, Any]:
        result = self.request(
            "POST",
            "/v1/connect",
            json={
                "target_intent_id": target_intent_id,
                "from_intent_id": from_intent_id,
                "opening_message": opening_message,
            },
        )
        assert result is not None
        return result

    def inbox(self) -> dict[str, Any]:
        result = self.request("GET", "/v1/inbox")
        assert result is not None
        return result

    def accept_connection(self, request_id: str) -> dict[str, Any]:
        result = self.request("POST", f"/v1/connect/{request_id}/accept")
        assert result is not None
        return result

    def reject_connection(self, request_id: str, reason: str | None = None) -> None:
        self.request("POST", f"/v1/connect/{request_id}/reject", json={"reason": reason})

    def propose_match(self, session_id: str) -> dict[str, Any]:
        result = self.request("POST", "/v1/match/propose", json={"session_id": session_id})
        assert result is not None
        return result

    def accept_match(self, session_id: str) -> dict[str, Any]:
        result = self.request("POST", "/v1/match/accept", json={"session_id": session_id})
        assert result is not None
        return result

    def reject_match(self, session_id: str, reason: str | None = None) -> dict[str, Any]:
        result = self.request(
            "POST",
            "/v1/match/reject",
            json={"session_id": session_id, "reason": reason},
        )
        assert result is not None
        return result

    def end_session(self, session_id: str) -> None:
        self.request("POST", f"/v1/session/{session_id}/end")

    def server_info(self) -> dict[str, Any]:
        result = self.request("GET", "/v1/server/info", signed=False)
        assert result is not None
        return result
