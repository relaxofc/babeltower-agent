from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass

from babeltower_agent.config import Config
from babeltower_agent.prompt import conversation_system_prompt


@dataclass(frozen=True)
class MatchDecision:
    decision: str
    reason: str
    confidence: float = 0.0

    @property
    def should_match(self) -> bool:
        return self.decision == "match"


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
        if model_reply and model_reply.strip():
            return model_reply.strip()
        if not transcript:
            return self.opening_message()
        return (
            "I am unable to generate a reliable reply right now, so I should pause here "
            "rather than risk a misleading response."
        )

    def should_propose_match(self, transcript: list[dict[str, str]]) -> bool:
        return (
            self.config.policy.auto_approve_match
            and self.evaluate_match(transcript).should_match
        )

    def should_accept_match(self, transcript: list[dict[str, str]]) -> MatchDecision:
        if not self.config.policy.auto_approve_match:
            return MatchDecision("uncertain", "Owner policy does not allow auto-approval.")
        return self.evaluate_match(transcript)

    def evaluate_match(self, transcript: list[dict[str, str]]) -> MatchDecision:
        if len(transcript) < 4:
            return MatchDecision("uncertain", "Need more conversation before judging fit.")
        payload = self._model_match_decision(transcript)
        if payload is None:
            return MatchDecision("uncertain", "Could not obtain a reliable fit judgment.")

        decision = str(payload.get("decision", "")).strip().lower().replace("-", "_")
        if decision not in {"match", "do_not_match", "uncertain"}:
            decision = "uncertain"
        reason = str(payload.get("reason") or payload.get("rationale") or "").strip()
        confidence = payload.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        if decision == "match" and confidence < 0.65:
            return MatchDecision(
                "uncertain",
                reason or "Fit judgment confidence was too low to exchange contacts.",
                confidence,
            )
        return MatchDecision(decision, reason, confidence)

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
        except Exception as exc:
            print(f"[babeltower llm reply failed] {exc}", file=sys.stderr, flush=True)
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

    def _match_decision_prompt(self, transcript: list[dict[str, str]]) -> str:
        return (
            "Conversation transcript so far:\n"
            f"{json.dumps(transcript, indent=2)}\n\n"
            "Decide whether the owners should exchange contact handles now.\n"
            "Return JSON only, with this schema:\n"
            '{"decision":"match|do_not_match|uncertain","confidence":0.0,'
            '"reason":"short explanation"}\n\n'
            "Be conservative. Same topic, market, role, or keyword overlap is not enough.\n"
            "Choose do_not_match when goals, constraints, seniority, time commitment, "
            "budget, geography, mentorship needs, execution expectations, or available "
            "support conflict. Choose uncertain if the conversation has not resolved an "
            "important constraint. Choose match only when the transcript shows positive fit "
            "and no explicit incompatibility."
        )

    def _parse_json_object(self, text: str | None) -> dict | None:
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                return None
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return result if isinstance(result, dict) else None

    def _model_match_decision(self, transcript: list[dict[str, str]]) -> dict | None:
        if not self._has_api_key():
            return None

        provider = self.config.llm.provider.lower()
        try:
            if provider == "anthropic":
                return self._anthropic_match_decision(transcript)
            if provider == "openai":
                return self._openai_match_decision(transcript)
            if provider == "ollama":
                return self._ollama_match_decision(transcript)
        except Exception:
            return None
        return None

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

    def _anthropic_match_decision(self, transcript: list[dict[str, str]]) -> dict | None:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.config.llm.api_key)
        message = client.messages.create(
            model=self.config.llm.model,
            max_tokens=500,
            system=self.system_prompt,
            messages=[{"role": "user", "content": self._match_decision_prompt(transcript)}],
        )
        first = message.content[0]
        return self._parse_json_object(getattr(first, "text", None))

    def _openai_reply(self, transcript: list[dict[str, str]]) -> str | None:
        from openai import OpenAI

        # `base_url` lets the user point the OpenAI SDK at any OpenAI-API-
        # compatible endpoint (DeepSeek, Groq, Together, Fireworks,
        # OpenRouter, vLLM, LM Studio, ...). When None, the SDK uses
        # api.openai.com — the previous default.
        client = OpenAI(
            api_key=self.config.llm.api_key,
            base_url=self.config.llm.base_url or None,
        )
        response = client.chat.completions.create(
            model=self.config.llm.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self._transcript_prompt(transcript)},
            ],
        )
        return response.choices[0].message.content

    def _openai_match_decision(self, transcript: list[dict[str, str]]) -> dict | None:
        from openai import OpenAI

        client = OpenAI(
            api_key=self.config.llm.api_key,
            base_url=self.config.llm.base_url or None,
        )
        request = {
            "model": self.config.llm.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self._match_decision_prompt(transcript)},
            ],
        }
        try:
            response = client.chat.completions.create(
                **request,
                response_format={"type": "json_object"},
            )
        except Exception:
            response = client.chat.completions.create(**request)
        return self._parse_json_object(response.choices[0].message.content)

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

    def _ollama_match_decision(self, transcript: list[dict[str, str]]) -> dict | None:
        import httpx

        response = httpx.post(
            "http://localhost:11434/api/chat",
            json={
                "model": self.config.llm.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self._match_decision_prompt(transcript)},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        return self._parse_json_object(response.json()["message"]["content"])
