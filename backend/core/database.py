"""Connexion à la base de données, créée uniquement au premier besoin."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    """La variable DATABASE_URL n'est pas définie."""


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url
    if not url:
        raise DatabaseNotConfiguredError("DATABASE_URL n'est pas définie dans .env")
    return create_engine(url, pool_pre_ping=True)


def open_session() -> Session:
    """Ouvre une session ; rien n'est enregistré sans commit explicite."""
    return sessionmaker(get_engine(), expire_on_commit=False)()


def get_session() -> Iterator[Session]:
    """Fournit une session à une requête de l'API."""
    with open_session() as session:
        yield session
