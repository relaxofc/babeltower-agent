from __future__ import annotations

from pathlib import Path

from babeltower_agent.config import CONFIG_DIR, Config

MAX_DOSSIER_CHARS = 12_000
MAX_TOTAL_DOSSIER_CHARS = 40_000


def _resolve_dossier_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return CONFIG_DIR / path


def owner_dossier_context(config: Config) -> str:
    blocks: list[str] = []
    remaining = MAX_TOTAL_DOSSIER_CHARS
    for raw_path in config.owner.dossier_paths:
        if remaining <= 0:
            break
        path = _resolve_dossier_path(raw_path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            blocks.append(f"### {raw_path}\n[Could not read dossier file: {exc}]")
            continue

        text = text.strip()
        if len(text) > MAX_DOSSIER_CHARS:
            text = text[:MAX_DOSSIER_CHARS].rstrip() + "\n[truncated]"
        if len(text) > remaining:
            text = text[:remaining].rstrip() + "\n[truncated]"
        remaining -= len(text)
        blocks.append(f"### {raw_path}\n{text}")

    if not blocks:
        return "No local dossier files configured."
    return "\n\n".join(blocks)


def conversation_system_prompt(
    config: Config,
    active_intent: dict,
    counterparty_intent: dict | None = None,
) -> str:
    default_handles = config.owner.handle_disclosure.get("default", [])
    on_request = config.owner.handle_disclosure.get("on_request", [])
    never = config.owner.handle_disclosure.get("never", [])
    dossier_context = owner_dossier_context(config)
    return f"""You are an AI agent representing {config.owner.name}.

You must clearly identify yourself as an AI agent acting for your owner.

Owner profile:
{config.owner.about}

Owner dossier files:
{dossier_context}

Your active intent:
{active_intent}

Counterparty intent:
{counterparty_intent or "Unknown until shared by the counterparty."}

Conversation goal:
- Ask 2-5 clarifying questions.
- Evaluate whether there is a real fit for both owners.
- If there is a fit and owner policy allows it, use propose_match.
- If there is no fit, end the session politely.

Hard contact rule:
- Do not share contact handles before match_confirmed.
- After match_confirmed, you may share default handles: {default_handles}.
- Share on-request handles only if the counterparty asks: {on_request}.
- Never share these handles: {never}.

Available actions:
send_message, propose_match, accept_match, reject_match, end_session, share_handle(handle_name)

Response format (IMPORTANT):
Reply with ONLY the plain message text you want to send to the counterparty.
Do NOT wrap your reply in JSON, do NOT include keys like "kind", "text",
"body", or "from", do NOT use triple backticks, and do NOT include any
metadata. The wire envelope is added by the agent runtime around whatever
plain string you return.
"""
