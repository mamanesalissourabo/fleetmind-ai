"""Mise à jour de l'index documentaire."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.api.dependencies import AdminDep
from backend.rag.embeddings import EmbeddingError
from backend.rag.ingestion import DocumentError
from backend.rag.retrieval import DocumentRetriever, RetrievalError, get_retriever

router = APIRouter(prefix="/documents", tags=["documents"])


class ReindexResult(BaseModel):
    sections_indexed: int


def retriever_dependency() -> DocumentRetriever:
    try:
        return get_retriever()
    except RetrievalError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error


@router.post("/reindex", response_model=ReindexResult)
def reindex(
    _: AdminDep, retriever: Annotated[DocumentRetriever, Depends(retriever_dependency)]
) -> ReindexResult:
    """Relit les procédures de data/documentation/procedures et reconstruit l'index."""
    try:
        return ReindexResult(sections_indexed=retriever.reindex())
    except EmbeddingError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    except DocumentError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
