from __future__ import annotations

import json

from babeltower_agent.config import Config
from babeltower_agent.prompt import conversation_system_prompt


class AgentBrain:
    def __init__(
        self,
        config: Config,
        active_intent: dict | None = None,
        counterparty_intent: dict | None = None,
    ):
        self.config = config
        self.system_prompt = conversation_system_prompt(
            config,
            active_intent or {},
            counterparty_intent,
        )

    def opening_message(self) -> str:
        return (
            f"Hi, I am an AI agent representing {self.config.owner.name}. "
            "I would like to ask a few questions to evaluate whether our owners should meet."
        )

    def reply(self, transcript: list[dict[str, str]]) -> str:
        model_reply = self._model_reply(transcript)
        if model_reply:
            return model_reply
        if not transcript:
            return self.opening_message()
        return (
            "Thanks. I am checking fit on goals, constraints, timing, "
            "and what each owner can offer. "
            "Could you share the most important requirement your owner has for this match?"
        )

    def should_propose_match(self, transcript: list[dict[str, str]]) -> bool:
        return self.config.policy.auto_approve_match and len(transcript) >= 4

    def _model_reply(self, transcript: list[dict[str, str]]) -> str | None:
        if not self._has_api_key():
            return None

        provider = self.config.llm.provider.lower()
        try:
            if provider == "anthropic":
                return self._anthropic_reply(transcript)
            if provider == "openai":
                return self._openai_reply(transcript)
            if provider == "ollama":
                return self._ollama_reply(transcript)
        except Exception:
            return None
        return None

    def _has_api_key(self) -> bool:
        if self.config.llm.provider.lower() == "ollama":
            return True
        api_key = self.config.llm.api_key
        return bool(api_key and not api_key.startswith("${"))

    def _transcript_prompt(self, transcript: list[dict[str, str]]) -> str:
        return (
            "Conversation transcript so far:\n"
            f"{json.dumps(transcript, indent=2)}\n\n"
            "Choose the next best agent action. Return only the message text to send."
        )

    def _anthropic_reply(self, transcript: list[dict[str, str]]) -> str | None:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.config.llm.api_key)
        message = client.messages.create(
            model=self.config.llm.model,
            max_tokens=500,
            system=self.system_prompt,
            messages=[{"role": "user", "content": self._transcript_prompt(transcript)}],
        )
        first = message.content[0]
        return getattr(first, "text", None)

    def _openai_reply(self, transcript: list[dict[str, str]]) -> str | None:
        from openai import OpenAI

        client = OpenAI(api_key=self.config.llm.api_key)
        response = client.chat.completions.create(
            model=self.config.llm.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self._transcript_prompt(transcript)},
            ],
        )
        return response.choices[0].message.content

    def _ollama_reply(self, transcript: list[dict[str, str]]) -> str | None:
        import httpx

        response = httpx.post(
            "http://localhost:11434/api/chat",
            json={
                "model": self.config.llm.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self._transcript_prompt(transcript)},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]
