from __future__ import annotations

import asyncio
import json
import os
import socket
import socketserver
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from babeltower_agent.config import CONFIG_DIR

CONTROL_SOCKET_PATH = CONFIG_DIR / "control.sock"
SESSIONS_DIR = CONFIG_DIR / "sessions"


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ControllerUnavailable(RuntimeError):
    pass


class SessionEventStore:
    def __init__(self, base_dir: Path = SESSIONS_DIR) -> None:
        self.base_dir = base_dir
        self._lock = threading.Lock()

    def _path_for(self, session_id: str) -> Path:
        return self.base_dir / session_id / "events.jsonl"

    def append(self, session_id: str, direction: str, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            path = self._path_for(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            next_seq = 1
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.strip():
                        try:
                            next_seq = max(next_seq, int(json.loads(line).get("seq", 0)) + 1)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            continue
            record = {
                "seq": next_seq,
                "recorded_at": utc_timestamp(),
                "direction": direction,
                "event": event,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            path.chmod(0o600)
            is_handoff = (
                event.get("type") == "message"
                and event.get("body", {}).get("kind") == "contact_handoff"
            )
            if is_handoff:
                handoff_path = path.parent / "handoff.json"
                handoff_path.write_text(
                    json.dumps(record, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                handoff_path.chmod(0o600)
            return record

    def read(
        self,
        session_id: str,
        since_seq: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        path = self._path_for(session_id)
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if since_seq is not None and int(record.get("seq", 0)) <= since_seq:
                continue
            records.append(record)
        return records[-limit:]

    def list_sessions(self) -> list[dict[str, Any]]:
        if not self.base_dir.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for child in sorted(self.base_dir.iterdir()):
            if not child.is_dir():
                continue
            events_path = child / "events.jsonl"
            handoff_path = child / "handoff.json"
            last_seq = 0
            last_recorded_at = None
            if events_path.exists():
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    last_seq = max(last_seq, int(record.get("seq", 0)))
                    last_recorded_at = record.get("recorded_at") or last_recorded_at
            sessions.append(
                {
                    "session_id": child.name,
                    "last_seq": last_seq,
                    "last_recorded_at": last_recorded_at,
                    "has_handoff": handoff_path.exists(),
                }
            )
        return sessions

    def list_handoffs(self) -> list[dict[str, Any]]:
        if not self.base_dir.exists():
            return []
        handoffs: list[dict[str, Any]] = []
        for handoff_path in sorted(self.base_dir.glob("*/handoff.json")):
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            handoffs.append({"session_id": handoff_path.parent.name, **handoff})
        return handoffs


@dataclass
class LiveSession:
    session_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]
    status: str = "active"
    started_at: str = ""
    last_seen_at: str = ""


class SessionRegistry:
    def __init__(self, store: SessionEventStore | None = None) -> None:
        self.store = store or SessionEventStore()
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def register(
        self,
        session_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        now = utc_timestamp()
        with self._lock:
            self._sessions[session_id] = LiveSession(
                session_id=session_id,
                loop=loop,
                queue=queue,
                started_at=now,
                last_seen_at=now,
            )

    def unregister(self, session_id: str) -> None:
        with self._lock:
            live = self._sessions.get(session_id)
            if live is not None:
                live.status = "closed"
                live.last_seen_at = utc_timestamp()
                del self._sessions[session_id]

    def touch(self, session_id: str) -> None:
        with self._lock:
            live = self._sessions.get(session_id)
            if live is not None:
                live.last_seen_at = utc_timestamp()

    def list_live(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "session_id": live.session_id,
                    "status": live.status,
                    "started_at": live.started_at,
                    "last_seen_at": live.last_seen_at,
                }
                for live in sorted(self._sessions.values(), key=lambda item: item.session_id)
            ]

    def enqueue(self, session_id: str, command: dict[str, Any]) -> None:
        with self._lock:
            live = self._sessions.get(session_id)
        if live is None:
            raise KeyError(session_id)
        live.loop.call_soon_threadsafe(live.queue.put_nowait, command)


class _UnixControllerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _ControllerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(1024 * 1024)
        try:
            request = json.loads(raw.decode("utf-8"))
            response = self.server.dispatch(request)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - local control must return JSON errors
            response = {"ok": False, "error": str(exc)}
        self.wfile.write(json.dumps(response, sort_keys=True).encode("utf-8") + b"\n")


class WatchController:
    def __init__(
        self,
        registry: SessionRegistry,
        socket_path: Path = CONTROL_SOCKET_PATH,
    ) -> None:
        self.registry = registry
        self.socket_path = socket_path
        self._server: _UnixControllerServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.chmod(0o700)
        if self.socket_path.exists():
            try:
                control_request({"action": "ping"}, self.socket_path)
            except ControllerUnavailable:
                self.socket_path.unlink()
            else:
                raise RuntimeError(
                    f"BabelTower watch controller already running at {self.socket_path}"
                )
        self._server = _UnixControllerServer(str(self.socket_path), _ControllerHandler)
        self._server.dispatch = self.dispatch  # type: ignore[attr-defined]
        os.chmod(self.socket_path, 0o600)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="babeltower-watch-controller",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self.socket_path.exists():
            self.socket_path.unlink()

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "ping":
            return {"ok": True}
        if action == "list_sessions":
            live_by_id = {item["session_id"]: item for item in self.registry.list_live()}
            stored = {item["session_id"]: item for item in self.registry.store.list_sessions()}
            session_ids = sorted(live_by_id.keys() | stored.keys())
            return {
                "ok": True,
                "sessions": [
                    {
                        **stored.get(session_id, {"session_id": session_id}),
                        "live": session_id in live_by_id,
                        **({"runtime": live_by_id[session_id]} if session_id in live_by_id else {}),
                    }
                    for session_id in session_ids
                ],
            }
        if action == "read_messages":
            return {
                "ok": True,
                "events": self.registry.store.read(
                    str(request["session_id"]),
                    request.get("since_seq"),
                    int(request.get("limit", 100)),
                ),
            }
        if action == "list_handoffs":
            return {"ok": True, "handoffs": self.registry.store.list_handoffs()}
        if action in {"send_message", "send_handoff", "end_session"}:
            session_id = str(request["session_id"])
            try:
                self.registry.enqueue(session_id, request)
            except KeyError as exc:
                raise RuntimeError(
                    f"Session {session_id} is not live in `babeltower-agent watch`."
                ) from exc
            return {"ok": True, "session_id": session_id, "queued": action}
        raise ValueError(f"unknown controller action: {action}")


def control_request(
    request: dict[str, Any],
    socket_path: Path = CONTROL_SOCKET_PATH,
    timeout: float = 3,
) -> dict[str, Any]:
    if not socket_path.exists():
        raise ControllerUnavailable(
            f"BabelTower watch controller is not running at {socket_path}. "
            "Start `babeltower-agent watch` and try again."
        )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(json.dumps(request, sort_keys=True).encode("utf-8") + b"\n")
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except OSError as exc:
        raise ControllerUnavailable(
            f"BabelTower watch controller is unavailable at {socket_path}: {exc}. "
            "Start or restart `babeltower-agent watch`."
        ) from exc
    response = json.loads(b"".join(chunks).decode("utf-8"))
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "BabelTower watch controller request failed")
    return response
