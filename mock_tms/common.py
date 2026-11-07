"""Chargement des données et de la configuration communes aux TMS simulés."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = PROJECT_ROOT / "data" / "synthetic"


class MockTmsSettings(BaseSettings):
    """Identifiants de démonstration des TMS simulés."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mock_alpha_api_key: str = "alpha-demo-key"
    mock_beta_client_id: str = "fleetmind"
    mock_beta_client_secret: str = "beta-demo-secret"
    mock_gamma_api_key: str = "gamma-demo-token"


@lru_cache
def get_mock_settings() -> MockTmsSettings:
    """Retourne une instance unique de la configuration."""
    return MockTmsSettings()


def load_dataset(name: str) -> dict[str, Any]:
    """Charge un jeu de données synthétique depuis data/synthetic/."""
    path = DATASETS_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))
