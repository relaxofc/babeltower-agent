from pathlib import Path

from babeltower_agent.config import load_config, new_config, save_config


def test_save_and_load_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    config = new_config("http://localhost:8000", owner_name="Marzhana")

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.server_url == "http://localhost:8000"
    assert loaded.owner.name == "Marzhana"
    assert loaded.owner.dossier_paths == []
    assert loaded.agent.pubkey == config.agent.pubkey
    assert loaded.agent.private_key == config.agent.private_key
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_save_and_load_config_preserves_dossier_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    config = new_config("http://localhost:8000", owner_name="Marzhana")
    config.owner.dossier_paths = ["/tmp/startup-dossier.txt", "relative-dossier.txt"]

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.owner.dossier_paths == ["/tmp/startup-dossier.txt", "relative-dossier.txt"]
