"""Accès au LLM local via Ollama : aucune inférence ne sort de l'infrastructure."""

import json
from typing import Any

import httpx

NO_THINK_PREFIX = "/no_think\n"


class LlmUnavailableError(Exception):
    """Le LLM local ne répond pas, ou sa réponse est inexploitable."""


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: float) -> None:
        self._base_url = base_url
        self._model = model
        self._timeout = timeout_seconds

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 200
    ) -> dict[str, Any]:
        """Retourne une réponse conforme à `schema`, déterministe et sans raisonnement affiché."""
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": NO_THINK_PREFIX + system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": max_tokens},
        }
        try:
            response = httpx.post(f"{self._base_url}/api/chat", json=body, timeout=self._timeout)
            response.raise_for_status()
            result = json.loads(response.json()["message"]["content"])
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise LlmUnavailableError(f"LLM local indisponible ({type(error).__name__})") from error
        if not isinstance(result, dict):
            raise LlmUnavailableError("Réponse du LLM local inexploitable")
        return result
