"""Consultation des missions, directement depuis les TMS et sous forme normalisée."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel

from backend.api.dependencies import CurrentUserDep, TmsRegistryDep, get_client_opener, select_tms
from backend.integrations.errors import TmsError
from backend.schemas.canonical import Mission

router = APIRouter(prefix="/missions", tags=["missions"])


class TmsMissions(BaseModel):
    """Missions d'un TMS, avec leur fraîcheur et ce qui n'a pas pu être lu."""

    source_tms: str
    available: bool
    retrieved_at: AwareDatetime
    missions: list[Mission] = []
    rejected_records: int = 0
    error: str | None = None


@router.get("", response_model=list[TmsMissions])
def list_missions(
    registry: TmsRegistryDep,
    user: CurrentUserDep,
    open_client: Annotated[object, Depends(get_client_opener)],
    tms_id: str | None = None,
) -> list[TmsMissions]:
    """Retourne les missions ; un TMS en panne est signalé, pas présenté comme vide."""
    results = []
    for config in select_tms(registry, user, tms_id):
        retrieved_at = datetime.now(UTC)
        with open_client(config) as client:
            try:
                fetched = client.get_missions()
            except (TmsError, LookupError) as error:
                results.append(
                    TmsMissions(
                        source_tms=config.id,
                        available=False,
                        retrieved_at=retrieved_at,
                        error=f"{type(error).__name__}: {error}",
                    )
                )
                continue
        results.append(
            TmsMissions(
                source_tms=config.id,
                available=True,
                retrieved_at=retrieved_at,
                missions=fetched.items,
                rejected_records=len(fetched.rejected),
            )
        )
    return results
