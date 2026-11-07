"""Point d'entrée de l'application FastAPI."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from backend import __version__
from backend.api.dependencies import (
    build_cycle_runner,
    get_clock,
    get_rules_registry,
    get_tms_registry,
    open_tms_client,
)
from backend.api.routes import alerts, auth, chatbot, documents, health, missions, monitoring
from backend.core.config import get_settings
from backend.core.database import DatabaseNotConfiguredError, open_session
from backend.services.scheduler import MonitoringScheduler


def _database_unavailable(_: Request, error: Exception) -> JSONResponse:
    """Répond sans exposer la chaîne de connexion ni le mot de passe."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": f"Base de données indisponible ({type(error).__name__})"},
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Démarre la surveillance automatique si elle est activée, et l'arrête proprement."""
    scheduler = None
    if get_settings().monitoring_enabled:
        scheduler = MonitoringScheduler(
            get_tms_registry().all(),
            build_cycle_runner(get_rules_registry(), open_tms_client),
            open_session,
            get_clock(),
        )
        scheduler.start()
    application.state.scheduler = scheduler
    yield
    if scheduler is not None:
        scheduler.shutdown()


def create_app() -> FastAPI:
    """Crée l'application et enregistre ses routes."""
    application = FastAPI(title=get_settings().app_name, version=__version__, lifespan=lifespan)
    for router_module in (health, auth, missions, monitoring, alerts, chatbot, documents):
        application.include_router(router_module.router)
    application.add_exception_handler(OperationalError, _database_unavailable)
    application.add_exception_handler(DatabaseNotConfiguredError, _database_unavailable)
    return application


app = create_app()
