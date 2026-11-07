"""Dépendances partagées par les routes de l'API."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.core.clock import Clock
from backend.core.config import get_settings
from backend.core.database import DatabaseNotConfiguredError, get_session, open_session
from backend.integrations.errors import TmsConfigError
from backend.integrations.generic_client import GenericTmsClient, create_http_client
from backend.integrations.tms_registry import TmsRegistry
from backend.schemas.auth import CurrentUser
from backend.schemas.tms_config import TmsConfig
from backend.services.alerting import AlertingService
from backend.services.auth import resolve_token
from backend.services.cycle_runner import CycleRunner, last_scheduled_reference
from backend.services.monitoring import MonitoringService
from backend.services.notification import NotificationService
from backend.services.rules_registry import RulesRegistry


@lru_cache
def get_tms_registry() -> TmsRegistry:
    return TmsRegistry.from_directory()


@lru_cache
def get_rules_registry() -> RulesRegistry:
    return RulesRegistry.from_directory()


@contextmanager
def open_tms_client(config: TmsConfig) -> Iterator[GenericTmsClient]:
    with create_http_client(config) as http_client:
        yield GenericTmsClient(config, http_client)


def get_client_opener():
    """Permet de remplacer l'accès réel aux TMS, notamment dans les tests."""
    return open_tms_client


def get_alerting_service(
    rules: Annotated[RulesRegistry, Depends(get_rules_registry)],
) -> AlertingService:
    return AlertingService(rules, NotificationService())


def build_cycle_runner(rules: RulesRegistry, open_client) -> CycleRunner:
    return CycleRunner(
        open_client, MonitoringService(rules), AlertingService(rules, NotificationService())
    )


def resume_simulated_start(configured: datetime | None, session_factory=open_session) -> datetime | None:
    """Reprend l'horloge simulée au dernier cycle planifié, pour ne pas revenir en arrière."""
    if configured is None:
        return None
    try:
        with session_factory() as session:
            last = last_scheduled_reference(session)
    except (SQLAlchemyError, DatabaseNotConfiguredError):
        return configured
    return max(configured, last) if last else configured


@lru_cache
def get_clock() -> Clock:
    settings = get_settings()
    start = resume_simulated_start(settings.monitoring_simulated_start)
    return Clock(start, settings.monitoring_time_speed)


def get_cycle_runner(
    rules: Annotated[RulesRegistry, Depends(get_rules_registry)],
    open_client: Annotated[object, Depends(get_client_opener)],
) -> CycleRunner:
    return build_cycle_runner(rules, open_client)


def visible_tms(registry: TmsRegistry, user: CurrentUser) -> list[TmsConfig]:
    return [config for config in registry.all() if user.can_access(config.id)]


def select_tms(registry: TmsRegistry, user: CurrentUser, tms_id: str | None) -> list[TmsConfig]:
    """Retourne le TMS demandé ; un TMS interdit répond comme un TMS inconnu."""
    if tms_id is None:
        return visible_tms(registry, user)
    try:
        config = registry.get(tms_id)
    except TmsConfigError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    if not user.can_access(config.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"TMS inconnu : {tms_id}")
    return [config]


SessionDep = Annotated[Session, Depends(get_session)]
bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: SessionDep,
) -> CurrentUser:
    """Toute route protégée passe ici : les droits sont relus en base à chaque requête."""
    user = resolve_token(session, credentials.credentials, datetime.now(UTC)) if credentials else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Connexion requise ou session expirée")
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_operator(user: CurrentUserDep) -> CurrentUser:
    if not user.can_act_on_alerts:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Votre rôle ne permet pas de traiter les alertes")
    return user


def require_admin(user: CurrentUserDep) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Action réservée aux administrateurs")
    return user


OperatorDep = Annotated[CurrentUser, Depends(require_operator)]
AdminDep = Annotated[CurrentUser, Depends(require_admin)]
TmsRegistryDep = Annotated[TmsRegistry, Depends(get_tms_registry)]
ClockDep = Annotated[Clock, Depends(get_clock)]
CycleRunnerDep = Annotated[CycleRunner, Depends(get_cycle_runner)]
AlertingDep = Annotated[AlertingService, Depends(get_alerting_service)]
