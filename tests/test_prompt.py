from babeltower_agent.config import new_config
from babeltower_agent.prompt import conversation_system_prompt


def test_prompt_forbids_contact_before_match_confirmed() -> None:
    config = new_config("http://localhost:8000", owner_name="Marzhana")
    config.owner.contact_handles = {"calendly": "https://calendly.com/example"}
    config.owner.handle_disclosure = {"default": ["calendly"], "on_request": [], "never": ["email"]}

    prompt = conversation_system_prompt(config, {"intent_id": "int_1"})

    assert "AI agent representing Marzhana" in prompt
    assert "Do not share contact handles before match_confirmed" in prompt
    assert "calendly" in prompt
