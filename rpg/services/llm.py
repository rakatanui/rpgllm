"""LLM client abstraction.

The Turn Engine is synchronous in the MVP. Keep the provider boundary synchronous
too; background execution/streaming can move the whole layer to async later.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from django.conf import settings

logger = logging.getLogger("rpg.llm")


@dataclass
class LLMResponse:
    raw_text: str
    action_type: str = ""
    public: str = ""
    private_to_gm: str = ""


class LLMClient(Protocol):
    def generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
    ) -> LLMResponse:
        ...

    def close(self) -> None:
        ...


class MockLLMClient:
    """Deterministic mock. Never performs network I/O."""

    def generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
    ) -> LLMResponse:
        player_name = "Player"
        for line in system_prompt.splitlines():
            if line.startswith("[PLAYER:"):
                player_name = line[len("[PLAYER:"):].rstrip("]").strip()
                break

        last_user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user_text = str(message.get("content", ""))[:120]
                break

        out_of_turn = "You are NOT the active player this round." in system_prompt
        action = "PASS" if out_of_turn else "ACT"
        public = (
            f"[PASS] {player_name}"
            if action == "PASS"
            else f"[ACT] {player_name}: mock response to: {last_user_text}".strip()
        )
        private = f"[PRIVATE] {player_name}: mock private note to GM."
        return LLMResponse(
            raw_text=json.dumps(
                {
                    "action_type": action,
                    "public": public,
                    "private_to_gm": private,
                }
            ),
            action_type=action,
            public=public,
            private_to_gm=private,
        )

    def close(self) -> None:
        return None


class LiteLLMClient:
    """Synchronous HTTP client to the LiteLLM OpenAI-compatible proxy."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ):
        self.base_url = (base_url or settings.LITELLM_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.LITELLM_API_KEY
        self.timeout = timeout or settings.DEFAULT_LLM_TIMEOUT
        self._client = httpx.Client(timeout=self.timeout)

    def generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "LiteLLM HTTP error %s: %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            raise
        except httpx.RequestError as exc:
            logger.warning("LiteLLM request error: %s", exc)
            raise

        text = data["choices"][0]["message"]["content"]
        return parse_structured_response(text)

    def close(self) -> None:
        self._client.close()


def _response_from_mapping(text: str, obj: dict) -> LLMResponse:
    action = str(obj.get("action_type", "ACT")).upper()
    if action not in ("ACT", "PASS", "ACT_OUT_OF_TURN"):
        action = "ACT"
    return LLMResponse(
        raw_text=text,
        action_type=action,
        public=str(obj.get("public", "")).strip(),
        private_to_gm=str(obj.get("private_to_gm", "")).strip(),
    )


def _json_mapping(candidate: str) -> dict | None:
    """Decode strict or nearly-valid JSON into a mapping.

    Some providers honor response_format semantically but still emit literal
    newlines/control characters inside JSON strings. Python's strict=False mode
    accepts those without weakening the structure of the surrounding object.
    """
    for strict in (True, False):
        try:
            obj = json.loads(candidate, strict=strict)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        # A few gateways double-encode the object as one JSON string.
        if isinstance(obj, str):
            try:
                nested = json.loads(obj, strict=False)
            except (json.JSONDecodeError, TypeError, ValueError):
                nested = None
            if isinstance(nested, dict):
                return nested

        if isinstance(obj, dict):
            return obj
    return None


def _looks_like_structured_json(text: str) -> bool:
    lowered = text.lower()
    return (
        text.lstrip().startswith("{")
        and (
            '"action_type"' in lowered
            or '"public"' in lowered
            or '"private_to_gm"' in lowered
        )
    )


def parse_structured_response(text: str) -> LLMResponse:
    """Parse a model response into structured fields.

    Plain prose still gets the legacy fallback. If the model clearly attempted
    the required JSON envelope but produced irrecoverably malformed JSON, fail
    instead of dumping the raw envelope into the public scene.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    obj = _json_mapping(stripped)
    if obj is not None:
        return _response_from_mapping(text, obj)

    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match:
        obj = _json_mapping(match.group(0))
        if obj is not None:
            return _response_from_mapping(text, obj)

    if _looks_like_structured_json(stripped):
        raise ValueError(
            "Model returned malformed structured JSON; raw JSON was not published."
        )

    action = "ACT"
    words = text.lower().split()
    if text.strip().upper().startswith("[PASS]") or "pass" in words[:2]:
        action = "PASS"
    return LLMResponse(
        raw_text=text,
        action_type=action,
        public=text.strip(),
        private_to_gm="",
    )


_client_singleton: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client_singleton
    if _client_singleton is None:
        backend = settings.LLM_BACKEND.lower()
        if backend == "litellm":
            _client_singleton = LiteLLMClient()
        elif backend == "mock":
            _client_singleton = MockLLMClient()
        else:
            raise RuntimeError(f"Unknown LLM_BACKEND: {backend!r}")
    return _client_singleton


def reset_llm_client() -> None:
    global _client_singleton
    if _client_singleton is not None:
        try:
            _client_singleton.close()
        except Exception:
            logger.debug("Failed to close LLM client during reset", exc_info=True)
    _client_singleton = None
