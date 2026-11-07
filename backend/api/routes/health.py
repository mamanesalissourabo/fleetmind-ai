"""Route de vérification de santé de l'application."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend import __version__
from backend.core.config import Settings, get_settings
from backend.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def get_health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Indique que l'application répond, sans contrôler de dépendance externe."""
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
    )
