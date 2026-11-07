"""Chargement et validation des configurations de TMS."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from backend.core.config import TMS_CONFIG_DIR
from backend.integrations.errors import TmsConfigError
from backend.schemas.tms_config import TmsConfig


def load_tms_config(path: Path) -> TmsConfig:
    """Lit et valide le fichier de configuration d'un TMS."""
    try:
        return TmsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise TmsConfigError(f"Configuration invalide ({path.name}) : {error}") from error


class TmsRegistry:
    """Donne accès aux configurations de TMS par identifiant."""

    def __init__(self, configs: list[TmsConfig]) -> None:
        self._configs: dict[str, TmsConfig] = {}
        for config in configs:
            if config.id in self._configs:
                raise TmsConfigError(f"Identifiant de TMS en double : {config.id}")
            self._configs[config.id] = config

    @classmethod
    def from_directory(cls, directory: Path = TMS_CONFIG_DIR) -> "TmsRegistry":
        return cls([load_tms_config(path) for path in sorted(directory.glob("*.yaml"))])

    def get(self, tms_id: str) -> TmsConfig:
        try:
            return self._configs[tms_id]
        except KeyError:
            raise TmsConfigError(f"TMS inconnu : {tms_id}") from None

    def all(self) -> list[TmsConfig]:
        return list(self._configs.values())
