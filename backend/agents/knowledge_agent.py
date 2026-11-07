"""Agent documentaire : répond à partir des procédures, et cite toujours sa source."""

from collections.abc import Callable
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents.common import is_faithful, other_tms_names, to_source
from backend.agents.llm_client import LlmUnavailableError, OllamaClient
from backend.rag.embeddings import EmbeddingError
from backend.rag.retrieval import DocumentRetriever, RetrievalError, RetrievedChunk
from backend.schemas.chat import AgentResult, SummarySource
from backend.schemas.tms_config import TmsConfig

NOT_FOUND_MESSAGE = (
    "Je n'ai trouvé aucune procédure sur ce sujet pour ce périmètre. Si la procédure est propre "
    "à un TMS, précisez-le dans la question ; sinon, adressez-vous au responsable d'exploitation."
)
UNAVAILABLE_MESSAGE = "La base documentaire est indisponible : aucune procédure n'a pu être consultée."
WRITER_PROMPT = """Tu réponds à un exploitant de transport, en français et en trois phrases courtes au maximum, uniquement à partir des extraits de procédure fournis. N'ajoute aucune étape, aucun chiffre ni conseil absents des extraits. Indique pertinent à false si les extraits ne traitent pas du sujet de la question.

Exemple d'extrait : Prévenir le client dès que possible.
Exemple de question : Que faire en cas de retard ?
Exemple de réponse : {"pertinent": true, "reponse": "La procédure demande de prévenir le client dès que possible."}
Exemple de question : Comment poser des congés ?
Exemple de réponse : {"pertinent": false, "reponse": ""}"""
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"pertinent": {"type": "boolean"}, "reponse": {"type": "string"}},
    "required": ["pertinent", "reponse"],
}


def _citation(hit: RetrievedChunk) -> str:
    label = " Procédure de démonstration." if hit.chunk.origin == "demonstration" else ""
    return f"Source : {hit.chunk.title}, version {hit.chunk.version}.{label}"


class KnowledgeState(TypedDict, total=False):
    question: str
    tms_id: str | None
    hits: list[RetrievedChunk]
    unavailable: bool
    off_topic: bool
    summary: str
    source: SummarySource


class KnowledgeAgent:
    def __init__(
        self,
        configs: list[TmsConfig],
        retriever_factory: Callable[[], DocumentRetriever],
        now: Callable[[], datetime],
        llm: OllamaClient,
    ) -> None:
        self._configs = configs
        self._retriever_factory = retriever_factory
        self._now = now
        self._llm = llm
        graph = StateGraph(KnowledgeState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("write", self._write)
        graph.add_edge(START, "retrieve")
        graph.add_conditional_edges(
            "retrieve", lambda state: "write" if state.get("hits") else "stop",
            {"write": "write", "stop": END},
        )
        graph.add_edge("write", END)
        self._graph = graph.compile()

    def run(self, question: str, tms_id: str | None) -> AgentResult:
        state = self._graph.invoke({"question": question, "tms_id": tms_id})
        hits = state.get("hits", [])
        if not hits or state.get("off_topic"):
            message = UNAVAILABLE_MESSAGE if state.get("unavailable") else NOT_FOUND_MESSAGE
            return AgentResult(
                answered=False, summary=message,
                summary_source=SummarySource.DETERMINISTIC, reference_time=self._now(),
            )
        return AgentResult(
            summary=state["summary"], summary_source=state["source"],
            reference_time=self._now(), sources=[to_source(hit) for hit in hits],
        )

    def _retrieve(self, state: KnowledgeState) -> KnowledgeState:
        try:
            return {"hits": self._retriever_factory().search(state["question"], state.get("tms_id"))}
        except (EmbeddingError, RetrievalError):
            return {"unavailable": True}

    def _write(self, state: KnowledgeState) -> KnowledgeState:
        """Sans réponse fidèle du LLM, la procédure est citée telle quelle."""
        hits = state["hits"]
        extracts = [f"{hit.chunk.title} - {hit.chunk.section} : {hit.chunk.text}" for hit in hits]
        citation = _citation(hits[0])
        try:
            result = self._llm.complete_json(
                WRITER_PROMPT,
                f"Question : {state['question']}\nExtraits :\n" + "\n".join(extracts),
                ANSWER_SCHEMA,
                max_tokens=130,
            )
            if result.get("pertinent") is False:
                return {"off_topic": True}
            answer = str(result.get("reponse", "")).strip()
        except LlmUnavailableError:
            answer = ""
        tms_scope = {state["tms_id"]} if state.get("tms_id") else set()
        forbidden = other_tms_names(self._configs, tms_scope) if tms_scope else []
        if answer and is_faithful(answer, " ".join(extracts), forbidden_names=forbidden):
            return {"summary": f"{answer} {citation}", "source": SummarySource.LLM}
        full_text = " ".join(hit.chunk.text for hit in hits)
        return {"summary": f"{full_text} {citation}", "source": SummarySource.DETERMINISTIC}
