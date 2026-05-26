from __future__ import annotations

from babeltower_agent.config import AgentIdentity, Config, LlmConfig, OwnerConfig
from babeltower_agent.crypto import generate_keypair
from babeltower_agent.llm import AgentBrain


def _config() -> Config:
    private_key, pubkey = generate_keypair()
    return Config(
        server_url="http://server",
        agent=AgentIdentity(pubkey=pubkey, private_key=private_key),
        llm=LlmConfig(api_key="${ANTHROPIC_API_KEY}"),
        owner=OwnerConfig(name="Test Owner"),
    )


def test_reply_pauses_instead_of_generic_question_when_llm_unavailable() -> None:
    brain = AgentBrain(_config())

    reply = brain.reply([{"from": "counterparty", "body": "What is your MRR?"}])

    assert "unable to generate a reliable reply" in reply
    assert "most important requirement" not in reply
