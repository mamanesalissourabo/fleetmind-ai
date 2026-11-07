"""Configuration de l'application, lue depuis l'environnement et le fichier .env."""

import os
from ipaddress import ip_address
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import AwareDatetime, PositiveFloat, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
TMS_CONFIG_DIR = PROJECT_ROOT / "config" / "tms"
RULES_DIR = PROJECT_ROOT / "config" / "rules"


def _is_internal_host(host: str) -> bool:
    """Accepte localhost, une adresse privée ou un nom de conteneur sans domaine."""
    if host == "localhost" or (host and "." not in host and ":" not in host):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class Settings(BaseSettings):
    """Paramètres de l'application."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FleetMind AI"
    app_env: str = "development"
    database_url: str | None = None
    monitoring_enabled: bool = False
    monitoring_simulated_start: AwareDatetime | None = None
    monitoring_time_speed: PositiveFloat = 1.0
    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:4b"
    llm_timeout_seconds: PositiveFloat = 120
    business_timezone: str = "Africa/Casablanca"
    embedding_model: str = "nomic-embed-text"
    embedding_query_prefix: str = "search_query: "
    embedding_document_prefix: str = "search_document: "
    qdrant_path: str = "data/qdrant"
    rag_min_score: float = 0.65

    @field_validator("ollama_base_url")
    @classmethod
    def refuse_external_inference(cls, url: str) -> str:
        """Le LLM doit rester sur une infrastructure contrôlée par l'entreprise."""
        host = urlparse(url).hostname or ""
        if not _is_internal_host(host):
            raise ValueError("OLLAMA_BASE_URL doit désigner un hôte local ou privé")
        return url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    """Retourne une instance unique des paramètres."""
    return Settings()


def get_secret(name: str) -> str:
    """Lit un secret dans l'environnement, puis dans le fichier .env."""
    value = os.environ.get(name) or dotenv_values(ENV_FILE).get(name)
    if not value:
        raise LookupError(f"Secret non défini : {name}")
    return value
