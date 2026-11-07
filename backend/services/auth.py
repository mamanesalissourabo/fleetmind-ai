"""Création de comptes, connexion et vérification des sessions."""

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.core.security import (
    DUMMY_HASH,
    hash_password,
    hash_token,
    new_token,
    verify_password,
)
from backend.models.user import AuthSession, User
from backend.schemas.auth import CurrentUser, Role

SESSION_DURATION = timedelta(hours=8)
MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 10


class AuthError(Exception):
    """Connexion refusée, sans préciser si le compte existe."""


def _to_current_user(user: User) -> CurrentUser:
    return CurrentUser(
        username=user.username, role=Role(user.role), allowed_tms=tuple(user.allowed_tms)
    )


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caractères")


def create_user(
    session: Session, username: str, password: str, role: Role, allowed_tms: list[str], now: datetime
) -> User:
    check_password_rules(password)
    if not allowed_tms:
        raise ValueError("Un compte doit avoir au moins un TMS autorisé")
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role.value,
        allowed_tms=allowed_tms,
        is_active=True,
        failed_attempts=0,
        created_at=now,
    )
    session.add(user)
    session.flush()
    return user


def login(session: Session, username: str, password: str, now: datetime) -> tuple[str, CurrentUser]:
    """Retourne un jeton ; le message d'erreur est le même quelle que soit la cause."""
    refused = AuthError("Identifiant ou mot de passe incorrect, ou compte temporairement bloqué")
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or (user.locked_until and user.locked_until > now):
        verify_password(password, DUMMY_HASH)
        raise refused
    if not verify_password(password, user.password_hash):
        user.failed_attempts += 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = now + LOCK_DURATION
            user.failed_attempts = 0
        session.commit()
        raise refused

    user.failed_attempts = 0
    user.locked_until = None
    session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    token = new_token()
    session.add(
        AuthSession(
            token_hash=hash_token(token), user_id=user.id,
            expires_at=now + SESSION_DURATION, created_at=now,
        )
    )
    session.commit()
    return token, _to_current_user(user)


def resolve_token(session: Session, token: str, now: datetime) -> CurrentUser | None:
    """Un compte désactivé perd l'accès immédiatement, même avec une session en cours."""
    stored = session.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    if stored is None or stored.expires_at <= now or not stored.user.is_active:
        return None
    return _to_current_user(stored.user)


def logout(session: Session, token: str) -> None:
    session.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    session.commit()
