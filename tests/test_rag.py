"""Tests du RAG avec des embeddings simulés et un index Qdrant en mémoire."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from backend.api.dependencies import get_current_user
from tests.helpers import TEST_ADMIN
from backend.agents.knowledge_agent import NOT_FOUND_MESSAGE, UNAVAILABLE_MESSAGE
from backend.agents.llm_client import LlmUnavailableError
from backend.agents.orchestrator import build_llm_client
from backend.api.dependencies import get_client_opener, get_clock
from backend.api.main import create_app
from backend.api.routes.chatbot import get_retriever_factory
from backend.core.clock import Clock
from backend.core.database import get_session
from backend.rag.embeddings import EmbeddingError
from backend.rag.ingestion import DocumentError, load_documents, parse_document
from backend.rag.retrieval import DOCUMENTS_DIR, DocumentRetriever, RetrievalError
from tests.test_chatbot import T0, FakeLlm, factory, opener  # noqa: F401

KEYWORDS = ["panne", "frigorifique", "retard", "déviation", "nouvelles"]


class KeywordEmbeddings:
    """Un axe par mot-clé : suffisant pour vérifier la recherche sans modèle."""

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        return [float(word in lowered) for word in KEYWORDS] + [0.01]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class BrokenEmbeddings(KeywordEmbeddings):
    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingError("modèle absent")


@pytest.fixture
def retriever() -> DocumentRetriever:
    instance = DocumentRetriever(QdrantClient(":memory:"), KeywordEmbeddings(), min_score=0.3)
    instance.reindex()
    return instance


def test_all_procedures_are_marked_as_demonstration_and_split_in_sections() -> None:
    chunks = load_documents(DOCUMENTS_DIR)

    assert len({chunk.document for chunk in chunks}) == 5
    assert {chunk.origin for chunk in chunks} == {"demonstration"}
    assert all(chunk.section and chunk.text for chunk in chunks)


def test_document_without_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sans_entete.md"
    path.write_text("## Section\nTexte", encoding="utf-8")

    with pytest.raises(DocumentError):
        parse_document(path)


def documents_for(retriever: DocumentRetriever, tms_id: str | None) -> set[str]:
    return {hit.chunk.document for hit in retriever.search("panne frigorifique", tms_id)}


def test_tms_specific_procedure_is_not_visible_to_another_tms(retriever) -> None:
    assert "rupture_froid.md" in documents_for(retriever, "tms_beta")
    assert "rupture_froid.md" not in documents_for(retriever, "tms_alpha")
    assert "rupture_froid.md" not in documents_for(retriever, None)


def test_whole_procedure_is_returned_in_order(retriever) -> None:
    hits = retriever.search("panne", None)

    assert [hit.chunk.position for hit in hits] == list(range(len(hits)))
    assert hits[0].chunk.section == "Premières actions"


def test_unrelated_question_finds_nothing(retriever) -> None:
    assert retriever.search("quel temps fait-il", None) == []


def test_search_before_indexing_is_reported() -> None:
    empty = DocumentRetriever(QdrantClient(":memory:"), KeywordEmbeddings(), min_score=0.3)

    with pytest.raises(RetrievalError):
        empty.search("panne", None)


def procedure(tms_id: str | None = None) -> dict:
    return {"intent": "procedure_question", "tms_id": tms_id, "period": "none"}


def ask_rag(factory, retriever, llm, question="Que faire en cas de panne ?"):  # noqa: F811
    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_client_opener] = lambda: opener
    app.dependency_overrides[build_llm_client] = lambda: llm
    app.dependency_overrides[get_clock] = lambda: Clock(T0)
    app.dependency_overrides[get_retriever_factory] = lambda: (lambda: retriever)
    return TestClient(app).post("/chatbot/ask", json={"question": question}).json()


def test_answer_cites_its_source_and_marks_demonstration(factory, retriever) -> None:  # noqa: F811
    llm = FakeLlm(procedure(), {"reponse": "Contacter le chauffeur pour confirmer sa sécurité."})

    body = ask_rag(factory, retriever, llm)

    assert body["answered"] is True
    assert body["summary_source"] == "llm"
    assert "Source : " in body["summary"] and "démonstration" in body["summary"]
    assert body["sources"] and body["sources"][0]["excerpt"]


def test_invented_number_falls_back_to_the_excerpt(factory, retriever) -> None:  # noqa: F811
    llm = FakeLlm(procedure(), {"reponse": "Appeler le dépanneur sous 30 minutes."})

    body = ask_rag(factory, retriever, llm)

    assert body["summary_source"] == "deterministic"
    assert "30 minutes" not in body["summary"]
    assert body["sources"][0]["excerpt"] in body["summary"]


def test_llm_down_during_writing_still_gives_the_procedure(factory, retriever) -> None:  # noqa: F811
    body = ask_rag(factory, retriever, FakeLlm(procedure(), LlmUnavailableError("x")))

    assert body["answered"] is True
    assert body["sources"]


def test_nothing_found_does_not_call_the_llm_to_write(factory, retriever) -> None:  # noqa: F811
    llm = FakeLlm(procedure())

    body = ask_rag(factory, retriever, llm, "Quel temps fait-il ?")

    assert llm.calls == 1
    assert body["answered"] is False
    assert body["summary"] == NOT_FOUND_MESSAGE


def test_embeddings_down_is_reported_without_guessing(factory) -> None:  # noqa: F811
    broken = DocumentRetriever(QdrantClient(":memory:"), BrokenEmbeddings(), min_score=0.3)
    broken.reindex()

    body = ask_rag(factory, broken, FakeLlm(procedure()))

    assert body["answered"] is False
    assert body["summary"] == UNAVAILABLE_MESSAGE


def test_llm_saying_extracts_are_off_topic_gives_not_found(factory, retriever) -> None:  # noqa: F811
    llm = FakeLlm(procedure(), {"pertinent": False, "reponse": ""})

    body = ask_rag(factory, retriever, llm)

    assert body["answered"] is False
    assert body["summary"] == NOT_FOUND_MESSAGE
    assert body["sources"] == []
