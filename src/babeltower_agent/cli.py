from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from babeltower_agent.client import BabelTowerClient
from babeltower_agent.config import (
    CONFIG_PATH,
    STATE_PATH,
    Config,
    load_config,
    load_state,
    new_config,
    remember_intent,
    save_config,
    save_state,
)
from babeltower_agent.session import join_session

app = typer.Typer(no_args_is_help=True)


def read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def client_from_config() -> tuple[Config, BabelTowerClient]:
    config = load_config()
    return config, BabelTowerClient(config)


@app.command()
def init(
    server_url: Annotated[str, typer.Option(help="BabelTower server URL.")] = "https://babel-tower.com",
    owner_name: Annotated[
        str, typer.Option(help="Owner display name for local prompts.")
    ] = "Owner",
    no_browser: Annotated[
        bool, typer.Option(help="Print OAuth URL instead of opening it.")
    ] = False,
    timeout_seconds: Annotated[int, typer.Option(help="Registration polling timeout.")] = 600,
) -> None:
    """Generate a keypair, start GitHub OAuth registration, and write config."""
    config = new_config(server_url=server_url, owner_name=owner_name)
    save_config(config)
    typer.echo(f"Wrote config to {CONFIG_PATH}")

    with BabelTowerClient(config) as client:
        registration = client.register_init()
        typer.echo("Open this GitHub OAuth URL to register the agent:")
        typer.echo(registration["github_oauth_url"])
        if not no_browser:
            client.open_registration_browser(registration["github_oauth_url"])

        token = registration["registration_token"]
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            status = client.registration_status(token)
            if status["status"] == "complete":
                typer.echo(f"Registration complete for {status['agent_pubkey']}")
                return
            if status["status"] == "failed":
                typer.echo(f"Registration failed: {status.get('reason', 'unknown')}", err=True)
                raise typer.Exit(1)
            time.sleep(3)
    typer.echo("Timed out waiting for registration.", err=True)
    raise typer.Exit(1)


@app.command()
def post(intent_yaml_file: Path) -> None:
    """Post an intent from YAML."""
    payload = read_yaml(intent_yaml_file)
    with client_from_config()[1] as client:
        intent = client.post_intent(payload)
    remember_intent(intent)
    typer.echo(yaml.safe_dump(intent, sort_keys=False))


@app.command("list")
def list_intents() -> None:
    """Show locally-known intents and refresh their server status."""
    state = load_state()
    config, client = client_from_config()
    refreshed = []
    with client:
        for item in state.get("intents", []):
            try:
                refreshed.append(client.get_intent(item["intent_id"]))
            except RuntimeError as exc:
                refreshed.append({**item, "status": f"unavailable: {exc}"})
    state["intents"] = refreshed
    save_state(state)
    typer.echo(
        yaml.safe_dump(
            {"agent_pubkey": config.agent.pubkey, "intents": refreshed},
            sort_keys=False,
        )
    )


@app.command()
def search(query_yaml_file: Path) -> None:
    """Search without posting a public intent."""
    payload = read_yaml(query_yaml_file)
    with client_from_config()[1] as client:
        result = client.search(payload)
    typer.echo(yaml.safe_dump(result, sort_keys=False))


@app.command()
def connect(
    target_intent_id: str,
    from_intent_id: str,
    message: Annotated[str | None, typer.Option(help="Opening message, max 500 chars.")] = None,
) -> None:
    """Send a connection request to the owner of a target intent."""
    with client_from_config()[1] as client:
        result = client.connect(target_intent_id, from_intent_id, message)
    typer.echo(yaml.safe_dump(result, sort_keys=False))


@app.command()
def inbox() -> None:
    """Poll and print the current inbox."""
    with client_from_config()[1] as client:
        result = client.inbox()
    typer.echo(yaml.safe_dump(result, sort_keys=False))


@app.command()
def accept(request_id: str) -> None:
    """Accept an incoming connection request."""
    with client_from_config()[1] as client:
        result = client.accept_connection(request_id)
    typer.echo(yaml.safe_dump(result, sort_keys=False))


@app.command()
def reject(
    request_id: str,
    reason: Annotated[str | None, typer.Option(help="Optional rejection reason.")] = None,
) -> None:
    """Reject an incoming connection request."""
    with client_from_config()[1] as client:
        client.reject_connection(request_id, reason)
    typer.echo("Rejected.")


@app.command()
def watch(
    interval: Annotated[int, typer.Option(help="Inbox polling interval in seconds.")] = 30,
    auto_accept: Annotated[
        bool, typer.Option(help="Accept requests even if config requires approval.")
    ] = False,
) -> None:
    """Poll inbox, optionally accept requests, and join accepted sessions."""
    config = load_config()
    client = BabelTowerClient(config)
    joined: set[str] = set()
    try:
        while True:
            inbox_payload = client.inbox()
            should_accept = auto_accept or config.policy.auto_accept_connection_requests
            if should_accept:
                for request in inbox_payload.get("pending_requests", []):
                    accepted = client.accept_connection(request["request_id"])
                    typer.echo(f"Accepted {request['request_id']} -> {accepted['session_id']}")
            else:
                for request in inbox_payload.get("pending_requests", []):
                    typer.echo(
                        f"Pending request: {request['request_id']} "
                        f"from {request['from_agent_pubkey']}"
                    )

            for session in inbox_payload.get("accepted_sessions_awaiting_join", []):
                session_id = session["session_id"]
                if session_id not in joined:
                    joined.add(session_id)
                    typer.echo(f"Joining session {session_id}")
                    # Run each session in a daemon thread with its own event
                    # loop so the inbox polling loop above keeps running.
                    # Without this, asyncio.run() blocks for the entire
                    # session — up to 30 minutes — and the agent stops
                    # heart-beating, its intents flip dormant after 5 min,
                    # and it can't accept any other incoming requests.
                    threading.Thread(
                        target=lambda sid=session_id: asyncio.run(
                            join_session(config, sid)
                        ),
                        daemon=True,
                        name=f"babeltower-session-{session_id}",
                    ).start()

            for handoff in inbox_payload.get("matched_handoffs", []):
                typer.echo("Match confirmed:")
                typer.echo(yaml.safe_dump(handoff, sort_keys=False))
            for rejection in inbox_payload.get("recently_rejected", []):
                reason = rejection.get("reason") or "none"
                typer.echo(
                    f"Connection request rejected: {rejection['request_id']} (reason: {reason})"
                )
            time.sleep(interval)
    finally:
        client.close()


@app.command()
def propose(session_id: str) -> None:
    """Propose a match for an active session."""
    with client_from_config()[1] as client:
        result = client.propose_match(session_id)
    typer.echo(yaml.safe_dump(result, sort_keys=False))


@app.command("accept-match")
def accept_match(session_id: str) -> None:
    """Accept a pending match proposal."""
    with client_from_config()[1] as client:
        result = client.accept_match(session_id)
    typer.echo(yaml.safe_dump(result, sort_keys=False))


@app.command("end-session")
def end_session(session_id: str) -> None:
    """End a session."""
    with client_from_config()[1] as client:
        client.end_session(session_id)
    typer.echo("Session ended.")


@app.command()
def status() -> None:
    """Show server info and local agent state."""
    config, client = client_from_config()
    state = load_state()
    with client:
        info = client.server_info()
    typer.echo(
        yaml.safe_dump(
            {
                "server_url": config.server_url,
                "agent_pubkey": config.agent.pubkey,
                "server": info,
                "state_path": str(STATE_PATH),
                "local_counts": {
                    "intents": len(state.get("intents", [])),
                    "sessions": len(state.get("sessions", [])),
                    "matches": len(state.get("matches", [])),
                },
            },
            sort_keys=False,
        )
    )


if __name__ == "__main__":
    app()
