from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from babeltower_agent.crypto import generate_keypair

CONFIG_DIR = Path.home() / ".babeltower"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
STATE_PATH = CONFIG_DIR / "state.yaml"


@dataclass
class AgentIdentity:
    pubkey: str
    private_key: str


@dataclass
class LlmConfig:
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-7"
    api_key: str = "${ANTHROPIC_API_KEY}"
    # Optional override for any OpenAI-compatible endpoint (DeepSeek,
    # Groq, Together, Fireworks, OpenRouter, vLLM, LM Studio, ...).
    # Only honored when provider == "openai". Leave None to use the
    # OpenAI SDK's default endpoint (api.openai.com).
    base_url: str | None = None


@dataclass
class OwnerConfig:
    name: str = "Owner"
    about: str = ""
    dossier_paths: list[str] = field(default_factory=list)
    contact_handles: dict[str, str] = field(default_factory=dict)
    handle_disclosure: dict[str, list[str]] = field(
        default_factory=lambda: {"default": [], "on_request": [], "never": []}
    )


@dataclass
class PolicyConfig:
    auto_accept_connection_requests: bool = False
    auto_approve_match: bool = False
    max_conversation_turns: int = 20
    webhook_url: str | None = None


@dataclass
class Config:
    server_url: str
    agent: AgentIdentity
    llm: LlmConfig = field(default_factory=LlmConfig)
    owner: OwnerConfig = field(default_factory=OwnerConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def config_to_dict(config: Config) -> dict[str, Any]:
    return {
        "server_url": config.server_url,
        "agent": {
            "pubkey": config.agent.pubkey,
            "private_key": config.agent.private_key,
        },
        "llm": {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "api_key": config.llm.api_key,
            "base_url": config.llm.base_url,
        },
        "owner": {
            "name": config.owner.name,
            "about": config.owner.about,
            "dossier_paths": config.owner.dossier_paths,
            "contact_handles": config.owner.contact_handles,
            "handle_disclosure": config.owner.handle_disclosure,
        },
        "policy": {
            "auto_accept_connection_requests": config.policy.auto_accept_connection_requests,
            "auto_approve_match": config.policy.auto_approve_match,
            "max_conversation_turns": config.policy.max_conversation_turns,
            "webhook_url": config.policy.webhook_url,
        },
    }


def config_from_dict(raw: dict[str, Any]) -> Config:
    expanded = expand_env(raw)
    agent_raw = expanded.get("agent", {})
    return Config(
        server_url=expanded.get("server_url", "https://babel-tower.com").rstrip("/"),
        agent=AgentIdentity(
            pubkey=agent_raw["pubkey"],
            private_key=agent_raw["private_key"],
        ),
        llm=LlmConfig(**expanded.get("llm", {})),
        owner=OwnerConfig(**expanded.get("owner", {})),
        policy=PolicyConfig(**expanded.get("policy", {})),
    )


def new_config(server_url: str, owner_name: str = "Owner") -> Config:
    private_key, pubkey = generate_keypair()
    return Config(
        server_url=server_url.rstrip("/"),
        agent=AgentIdentity(pubkey=pubkey, private_key=private_key),
        owner=OwnerConfig(name=owner_name),
    )


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}. Run babeltower-agent init first.")
    raw = yaml.safe_load(path.read_text()) or {}
    return config_from_dict(raw)


def save_config(config: Config, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config_to_dict(config), sort_keys=False))
    path.chmod(0o600)


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"intents": [], "sessions": [], "matches": []}
    return yaml.safe_load(path.read_text()) or {"intents": [], "sessions": [], "matches": []}


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(state, sort_keys=False))
    path.chmod(0o600)


def remember_intent(intent: dict[str, Any], path: Path = STATE_PATH) -> None:
    state = load_state(path)
    known = {item["intent_id"]: item for item in state.setdefault("intents", [])}
    known[intent["intent_id"]] = intent
    state["intents"] = list(known.values())
    save_state(state, path)
