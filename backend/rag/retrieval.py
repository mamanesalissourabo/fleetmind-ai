"""Index vectoriel local : chaque TMS ne voit que les procédures qui le concernent."""

import threading
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel
from qdrant_client import QdrantClient, models

from backend.core.config import PROJECT_ROOT, get_settings
from backend.rag.embeddings import EmbeddingError, OllamaEmbeddings
from backend.rag.ingestion import ALL_TMS, DocumentChunk, load_documents

COLLECTION = "procedures"
MAX_SECTIONS = 50
DOCUMENTS_DIR = PROJECT_ROOT / "data" / "documentation" / "procedures"


class RetrievalError(Exception):
    """L'index documentaire est absent ou inaccessible."""


class RetrievedChunk(BaseModel):
    chunk: DocumentChunk
    score: float


class DocumentRetriever:
    """Le client Qdrant local n'admet pas d'accès simultanés : ils sont donc sérialisés."""

    def __init__(self, client: QdrantClient, embeddings: OllamaEmbeddings, min_score: float) -> None:
        self._client = client
        self._embeddings = embeddings
        self._min_score = min_score
        self._lock = threading.Lock()

    def reindex(self, directory: Path = DOCUMENTS_DIR) -> int:
        """Reconstruit l'index ; le précédent reste en place si les embeddings échouent."""
        chunks = load_documents(directory)
        vectors = self._embeddings.embed_documents(
            [f"{chunk.title} - {chunk.section}\n{chunk.text}" for chunk in chunks]
        )
        points = [
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, f"{chunk.document}#{chunk.section}")),
                vector=vector,
                payload=chunk.model_dump(),
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        with self._lock:
            if self._client.collection_exists(COLLECTION):
                self._client.delete_collection(COLLECTION)
            self._client.create_collection(
                COLLECTION,
                vectors_config=models.VectorParams(
                    size=len(vectors[0]), distance=models.Distance.COSINE
                ),
            )
            self._client.upsert(COLLECTION, points)
        return len(chunks)

    def search(self, question: str, tms_id: str | None) -> list[RetrievedChunk]:
        """Retourne la procédure la plus proche, entière et dans l'ordre de ses sections.

        Sans TMS précisé, seules les procédures communes à tous les TMS sont consultées.
        """
        vector = self._embeddings.embed_query(question)
        allowed = [ALL_TMS] if tms_id is None else [ALL_TMS, tms_id]
        with self._lock:
            if not self._client.collection_exists(COLLECTION):
                raise RetrievalError("Index documentaire absent : lancer POST /documents/reindex")
            best = self._client.query_points(
                COLLECTION,
                query=vector,
                limit=1,
                score_threshold=self._min_score,
                query_filter=_match("tms", allowed),
            ).points
            if not best:
                return []
            sections, _ = self._client.scroll(
                COLLECTION,
                scroll_filter=_match("document", [best[0].payload["document"]]),
                limit=MAX_SECTIONS,
            )
        chunks = sorted(
            (DocumentChunk(**point.payload) for point in sections), key=lambda c: c.position
        )
        return [RetrievedChunk(chunk=chunk, score=best[0].score) for chunk in chunks]


def _match(key: str, values: list[str]) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key=key, match=models.MatchAny(any=values))])


@lru_cache
def get_retriever() -> DocumentRetriever:
    """Ouvre l'index une seule fois : le mode local verrouille son dossier."""
    settings = get_settings()
    embeddings = OllamaEmbeddings(
        settings.ollama_base_url,
        settings.embedding_model,
        settings.embedding_query_prefix,
        settings.embedding_document_prefix,
        settings.llm_timeout_seconds,
    )
    try:
        client = QdrantClient(path=str(PROJECT_ROOT / settings.qdrant_path))
    except RuntimeError as error:
        raise RetrievalError(f"Index documentaire verrouillé ({type(error).__name__})") from error
    return DocumentRetriever(client, embeddings, settings.rag_min_score)


__all__ = ["DocumentRetriever", "EmbeddingError", "RetrievalError", "get_retriever"]
