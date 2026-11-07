"""Contrôles et formulations partagés par les agents."""

import re
from collections.abc import Iterable

from backend.models.alert import Alert
from backend.rag.retrieval import RetrievedChunk
from backend.schemas.chat import DocumentSource
from backend.schemas.tms_config import TmsConfig

IDENTIFIER = re.compile(r"\b[A-Z]{2,}-\d+\b")
NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")


def _same_decimal(number: str) -> str:
    """Considère 132,3 et 132.3 comme un même nombre."""
    return number.replace(",", ".")


def is_faithful(
    text: str,
    allowed_text: str,
    required: Iterable[str] = (),
    forbidden_names: Iterable[str] = (),
) -> bool:
    """Refuse un identifiant ou un nombre absents des données, un oubli ou un autre TMS."""
    if any(identifier not in text for identifier in required):
        return False
    if any(name in text.lower() for name in forbidden_names):
        return False
    without_ids = IDENTIFIER.sub(" ", text)
    allowed_numbers = {_same_decimal(number) for number in NUMBER.findall(allowed_text)}
    return all(item in allowed_text for item in IDENTIFIER.findall(text)) and all(
        _same_decimal(number) in allowed_numbers for number in NUMBER.findall(without_ids)
    )


def other_tms_names(configs: Iterable[TmsConfig], scope_ids: set[str]) -> list[str]:
    """Noms des TMS hors périmètre : les citer mélangerait les contextes."""
    return [
        config.name.removeprefix("TMS ").lower() for config in configs if config.id not in scope_ids
    ]


def describe_alert(alert: Alert) -> str:
    return (
        f"Alerte {alert.id} ({alert.status}, gravité {alert.severity}), "
        f"mission {alert.mission_external_id} : {alert.message}"
    )


def to_source(hit: RetrievedChunk) -> DocumentSource:
    return DocumentSource(
        document=hit.chunk.document,
        title=hit.chunk.title,
        section=hit.chunk.section,
        version=hit.chunk.version,
        origin=hit.chunk.origin,
        score=round(hit.score, 3),
        excerpt=hit.chunk.text,
    )
