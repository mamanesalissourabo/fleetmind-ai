"""Format de la réponse de la route de santé."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """État de l'application."""

    status: str
    service: str
    version: str
    environment: str
