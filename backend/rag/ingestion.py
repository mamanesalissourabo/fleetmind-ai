"""Découpage des procédures en sections, avec leurs métadonnées."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, PositiveInt, ValidationError

FRONT_MATTER_SEPARATOR = "---"
SECTION_PREFIX = "## "
ALL_TMS = "all"


class DocumentError(ValueError):
    """Un document n'a pas le format attendu."""


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    version: PositiveInt
    origin: Literal["demonstration", "business"]
    tms: str


class DocumentChunk(BaseModel):
    document: str
    title: str
    section: str
    position: int
    version: int
    origin: str
    tms: str
    text: str


def parse_document(path: Path) -> list[DocumentChunk]:
    """Découpe un document par section ; une section sans texte est ignorée."""
    content = path.read_text(encoding="utf-8")
    parts = content.split(FRONT_MATTER_SEPARATOR, 2)
    if len(parts) != 3 or parts[0].strip():
        raise DocumentError(f"En-tête manquant : {path.name}")
    try:
        metadata = DocumentMetadata.model_validate(yaml.safe_load(parts[1]))
    except (yaml.YAMLError, ValidationError) as error:
        raise DocumentError(f"En-tête invalide : {path.name}") from error

    chunks = []
    for position, block in enumerate(parts[2].split("\n" + SECTION_PREFIX)[1:]):
        section, _, body = block.partition("\n")
        if body.strip():
            chunks.append(
                DocumentChunk(
                    document=path.name,
                    section=section.strip(),
                    position=position,
                    text=body.strip(),
                    **metadata.model_dump(),
                )
            )
    if not chunks:
        raise DocumentError(f"Aucune section : {path.name}")
    return chunks


def load_documents(directory: Path) -> list[DocumentChunk]:
    return [chunk for path in sorted(directory.glob("*.md")) for chunk in parse_document(path)]
