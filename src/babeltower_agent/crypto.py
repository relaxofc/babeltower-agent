from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_bytes(payload: Any) -> bytes:
    if payload is None:
        return b""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def generate_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    private_key_bytes = private_key.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    public_key_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return (
        base64.b64encode(private_key_bytes).decode("ascii"),
        base64.b64encode(public_key_bytes).decode("ascii"),
    )


def sign(private_key_b64: str, message: bytes) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(private_key.sign(message)).decode("ascii")


def canonical_request_string(
    method: str,
    path_with_query: str,
    timestamp: str,
    body: bytes,
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path_with_query}\n{timestamp}\n{body_hash}".encode()


def request_signature(
    private_key_b64: str,
    method: str,
    path_with_query: str,
    timestamp: str,
    body: bytes,
) -> str:
    canonical = canonical_request_string(method, path_with_query, timestamp, body)
    return sign(private_key_b64, canonical)


def websocket_hello_signature(private_key_b64: str, session_id: str, timestamp: str) -> str:
    return sign(private_key_b64, f"hello\n{session_id}\n{timestamp}".encode())


def websocket_message_signature(
    private_key_b64: str,
    session_id: str,
    timestamp: str,
    body: dict[str, Any],
) -> str:
    body_hash = hashlib.sha256(json_bytes(body)).hexdigest()
    return sign(private_key_b64, f"{session_id}\n{timestamp}\n{body_hash}".encode())
