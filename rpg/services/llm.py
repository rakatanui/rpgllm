"""LLM client abstraction.

LLM provider code is hidden behind this interface. Views and Turn Engine
talk only to `get_llm_client()` / `LLMClient`.

Two implementations:
  - MockLLMClient     : deterministic, no network
  - LiteLLMClient     : HTTP to LiteLLM proxy
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from django.conf import settings

logger = logging.getLogger("rpg.llm")


@dataclass
class LLMResponse:
    """Normalized response from any LLM backend."""
    raw_text: str
    # parsed structured action (best-effort):
    action_type: str = ""          # "ACT" | "PASS" | "ACT_OUT_OF_TURN"
    public: str = ""
    private_to_gm: str = ""


class LLMClient(Protocol):
    async def generate(self, *, system_prompt: str, messages: list[dict], model: str,
                       temperature: float = 0.7) -> LLMResponse:
        ...

    async def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------
class MockLLMClient:
    """Deterministic mock. Never touches the network.

    Returns a structured response shaped after the spec contract:
      {"action_type": "ACT", "public": "...", "private_to_gm": "..."}
    The public text encodes the player name passed via system_prompt marker.
    """

    async def generate(self, *, system_prompt: str, messages: list[dict], model: str,
                       temperature: float = 0.7) -> LLMResponse:
        # Extract player name from a sentinel in the system prompt:
        # we set "[PLAYER: Name]" inside the system prompt by the context builder.
        player_name = "Player"
        for line in system_prompt.splitlines():
            if line.startswith("[PLAYER:"):
                player_name = line[len("[PLAYER:"):]
                player_name = player_name.rstrip("]").strip()
                break

        # Echo the last GM input as the public action.
        last_user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_text = str(m.get("content", ""))[:120]
                break

        action = "ACT"
        # If it's an out-of-turn situation hint, mark accordingly. (Not used in mock by default.)
        public = f"[ACT] {player_name}: mock response to: {last_user_text}".strip()
        private = f"[PRIVATE] {player_name}: mock private note to GM."
        return LLMResponse(
            raw_text=json.dumps({"action_type": action, "public": public, "private_to_gm": private}),
            action_type=action,
            public=public,
            private_to_gm=private,
        )

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# LiteLLM (HTTP)
# ---------------------------------------------------------------------------
class LiteLLMClient:
    """HTTP client to the LiteLLM proxy (OpenAI-compatible /chat/completions)."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: float | None = None):
        self.base_url = (base_url or settings.LITELLM_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.LITELLM_API_KEY
        self.timeout = timeout or settings.DEFAULT_LLM_TIMEOUT
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def generate(self, *, system_prompt: str, messages: list[dict], model: str,
                       temperature: float = 0.7) -> LLMResponse:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": temperature,
        }
        # Try structured output via JSON mode + a schema hint in system prompt.
        payload["response_format"] = {"type": "json_object"}

        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("LiteLLM HTTP error %s: %s", e.response.status_code, e.response.text[:200])
            raise
        except httpx.RequestError as e:
            logger.warning("LiteLLM request error: %s", e)
            raise

        text = data["choices"][0]["message"]["content"]
        return parse_structured_response(text)

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Response parsing (shared fallback parser)
# ---------------------------------------------------------------------------
def parse_structured_response(text: str) -> LLMResponse:
    """Parse a model response into structured fields, with fallback."""
    # Strip markdown code fences (```json ... ``` or ``` ... ```) that many
    # models wrap around JSON even when asked not to.
    stripped = text.strip()
    if stripped.startswith("```"):
        # remove opening fence (with optional language tag) and trailing fence
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    # Try JSON first.
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            action = str(obj.get("action_type", "ACT")).upper()
            if action not in ("ACT", "PASS", "ACT_OUT_OF_TURN"):
                action = "ACT"
            public = str(obj.get("public", "")).strip()
            private = str(obj.get("private_to_gm", "")).strip()
            return LLMResponse(raw_text=text, action_type=action, public=public,
                               private_to_gm=private)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try to find a JSON object embedded in the text (some models add prose).
    import re
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                action = str(obj.get("action_type", "ACT")).upper()
                if action not in ("ACT", "PASS", "ACT_OUT_OF_TURN"):
                    action = "ACT"
                public = str(obj.get("public", "")).strip()
                private = str(obj.get("private_to_gm", "")).strip()
                return LLMResponse(raw_text=text, action_type=action, public=public,
                                   private_to_gm=private)
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback heuristics.
    action = "ACT"
    if text.strip().upper().startswith("[PASS]") or "pass" in text.lower().split()[:2]:
        action = "PASS"
    return LLMResponse(raw_text=text, action_type=action, public=text.strip(),
                       private_to_gm="")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_client_singleton: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Return the configured LLM client based on settings.LLM_BACKEND."""
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
    """For tests / config changes."""
    global _client_singleton
    _client_singleton = None