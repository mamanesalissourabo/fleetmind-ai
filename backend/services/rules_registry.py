"""Chargement des règles : chaque TMS n'accède qu'à son propre ensemble de règles."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from backend.core.config import RULES_DIR
from backend.integrations.errors import TmsConfigError
from backend.schemas.rules import RuleSet


def load_ruleset(path: Path) -> RuleSet:
    """Lit et valide un fichier de règles."""
    try:
        return RuleSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise TmsConfigError(f"Règles invalides ({path.name}) : {error}") from error


class RulesRegistry:
    """Donne accès aux règles par TMS, sans aucune règle de repli."""

    def __init__(self, rulesets: list[RuleSet]) -> None:
        self._rulesets: dict[str, RuleSet] = {}
        for ruleset in rulesets:
            if ruleset.tms_id in self._rulesets:
                raise TmsConfigError(f"Règles en double pour le TMS : {ruleset.tms_id}")
            self._rulesets[ruleset.tms_id] = ruleset

    @classmethod
    def from_directory(cls, directory: Path = RULES_DIR) -> "RulesRegistry":
        return cls([load_ruleset(path) for path in sorted(directory.glob("*.yaml"))])

    def get(self, tms_id: str) -> RuleSet:
        try:
            return self._rulesets[tms_id]
        except KeyError:
            raise TmsConfigError(f"Aucune règle définie pour le TMS : {tms_id}") from None
