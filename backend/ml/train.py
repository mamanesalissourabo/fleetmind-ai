"""Entraîne le modèle de risque de retard et enregistre ses mesures à côté de lui."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
from sklearn.model_selection import GroupShuffleSplit

from backend.core.config import PROJECT_ROOT
from backend.ml.evaluate import evaluate
from backend.ml.features import FEATURE_NAMES
from backend.ml.synthetic import LATE_THRESHOLD_MINUTES, generate_dataset

MODEL_DIR = PROJECT_ROOT / "models" / "delay_risk"
MODEL_FILE = "model.txt"
METADATA_FILE = "metadata.json"
PARAMETERS = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 30,
    "feature_fraction": 0.9,
    "seed": 42,
    "verbose": -1,
    "num_threads": 2,
}
ROUNDS = 150


def train_model(missions: int = 3000, seed: int = 42, output: Path = MODEL_DIR) -> dict:
    """Une mission est entièrement en entraînement ou en test, jamais dans les deux."""
    dataset = generate_dataset(missions, seed)
    features = np.array(dataset.features, dtype=float)
    labels = np.array(dataset.labels)
    groups = np.array(dataset.groups)
    split = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=seed)
    train_rows, test_rows = next(split.split(features, labels, groups))

    booster = lgb.train(
        PARAMETERS,
        lgb.Dataset(features[train_rows], labels[train_rows], feature_name=FEATURE_NAMES),
        num_boost_round=ROUNDS,
    )
    probabilities = booster.predict(features[test_rows])
    gap = features[test_rows, FEATURE_NAMES.index("progress_gap")]
    metadata = {
        "model": "delay_risk",
        "trained_at": datetime.now(UTC).isoformat(),
        "training_data": "synthetic",
        "missions": missions,
        "seed": seed,
        "late_threshold_minutes": LATE_THRESHOLD_MINUTES,
        "features": FEATURE_NAMES,
        "metrics": evaluate(labels[test_rows], probabilities, gap),
    }
    output.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(output / MODEL_FILE))
    (output / METADATA_FILE).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--missions", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    print(json.dumps(train_model(args.missions, args.seed)["metrics"], indent=2))
