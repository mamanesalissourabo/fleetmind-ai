"""Prédiction du risque de retard : une estimation, jamais une décision."""

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np

from backend.ml.features import FEATURE_NAMES, extract_features
from backend.schemas.canonical import Mission

FACTOR_LABELS = {
    "planned_duration_min": "durée prévue",
    "route_km": "longueur du trajet",
    "departure_delay_min": "retard au départ",
    "elapsed_ratio": "temps déjà écoulé",
    "progress_ratio": "avancement sur le trajet",
    "progress_gap": "écart entre le temps écoulé et l'avancement",
    "position_age_min": "ancienneté de la position",
    "departure_hour": "heure de départ",
    "weekday": "jour de la semaine",
    "open_incidents": "incident ouvert",
    "driver_known": "chauffeur renseigné",
}


class ModelUnavailableError(Exception):
    """Le modèle n'a pas encore été entraîné ou ne correspond plus au code."""


@dataclass(frozen=True)
class RiskPrediction:
    probability: float
    main_factors: list[str]


class DelayRiskModel:
    def __init__(self, directory: Path) -> None:
        import lightgbm as lgb  # Import tardif : inutile tant qu'aucune prédiction n'est demandée.

        model_path, metadata_path = directory / "model.txt", directory / "metadata.json"
        if not model_path.exists() or not metadata_path.exists():
            raise ModelUnavailableError("Modèle non entraîné : lancer python -m backend.ml.train")
        self.metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if self.metadata["features"] != FEATURE_NAMES:
            raise ModelUnavailableError("Modèle obsolète : relancer python -m backend.ml.train")
        self._booster = lgb.Booster(model_file=str(model_path))

    def predict(self, mission: Mission, reference: datetime, open_incidents: int) -> RiskPrediction:
        row = np.array([extract_features(mission, reference, open_incidents)], dtype=float)
        probability = float(self._booster.predict(row)[0])
        contributions = self._booster.predict(row, pred_contrib=True)[0][:-1]
        order = np.argsort(contributions)[::-1]
        factors = [FACTOR_LABELS[FEATURE_NAMES[i]] for i in order[:2] if contributions[i] > 0.1]
        return RiskPrediction(probability, factors)


@lru_cache
def get_delay_risk_model() -> DelayRiskModel:
    from backend.ml.train import MODEL_DIR

    return DelayRiskModel(MODEL_DIR)
