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


def test_prompt_includes_owner_dossier_files(tmp_path) -> None:
    dossier = tmp_path / "startup-dossier.txt"
    dossier.write_text(
        "One-liner: workflow software for independent dental clinics.\n"
        "MRR: $42k.\n"
        "Do not claim: HIPAA certified.",
        encoding="utf-8",
    )
    config = new_config("http://localhost:8000", owner_name="ClinicFlow")
    config.owner.dossier_paths = [str(dossier)]

    prompt = conversation_system_prompt(config, {"intent_id": "int_1"})

    assert "Owner dossier files:" in prompt
    assert "startup-dossier.txt" in prompt
    assert "MRR: $42k" in prompt
    assert "Do not claim: HIPAA certified." in prompt


def test_prompt_handles_missing_dossier_files() -> None:
    config = new_config("http://localhost:8000", owner_name="ClinicFlow")
    config.owner.dossier_paths = ["/tmp/does-not-exist-babeltower-dossier.txt"]

    prompt = conversation_system_prompt(config, {"intent_id": "int_1"})

    assert "Could not read dossier file" in prompt
