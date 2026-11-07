"""Mesures de qualité du modèle, comparées à des références simples."""

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score


def evaluate(labels: np.ndarray, probabilities: np.ndarray, baseline_score: np.ndarray) -> dict[str, float]:
    """La référence naïve prédit toujours le taux moyen ; l'autre n'utilise qu'une seule variable."""
    prevalence = float(labels.mean())
    known = ~np.isnan(baseline_score)
    predicted_late = probabilities >= 0.5
    true_late = labels == 1
    return {
        "test_rows": int(labels.size),
        "late_rate": round(prevalence, 3),
        "roc_auc": round(float(roc_auc_score(labels, probabilities)), 3),
        "baseline_progress_gap_roc_auc": round(
            float(roc_auc_score(labels[known], baseline_score[known])), 3
        ),
        "average_precision": round(float(average_precision_score(labels, probabilities)), 3),
        "brier": round(float(brier_score_loss(labels, probabilities)), 3),
        "baseline_brier": round(float(np.mean((prevalence - labels) ** 2)), 3),
        "precision_at_50": round(float(true_late[predicted_late].mean()) if predicted_late.any() else 0.0, 3),
        "recall_at_50": round(float(predicted_late[true_late].mean()) if true_late.any() else 0.0, 3),
    }
