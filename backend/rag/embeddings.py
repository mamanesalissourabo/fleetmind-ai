"""Calcul local des vecteurs de texte via Ollama."""

import httpx


class EmbeddingError(Exception):
    """Le modèle d'embeddings local ne répond pas ou n'est pas installé."""


class OllamaEmbeddings:
    def __init__(
        self,
        base_url: str,
        model: str,
        query_prefix: str = "",
        document_prefix: str = "",
        timeout_seconds: float = 120,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._timeout = timeout_seconds

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self._document_prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_prefix + text])[0]

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        try:
            response = httpx.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": inputs},
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise EmbeddingError(
                f"Embeddings indisponibles ({type(error).__name__}), modèle : {self._model}"
            ) from error
