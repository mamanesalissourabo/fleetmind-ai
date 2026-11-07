"""Présentation des données : traductions, dates et ordre d'affichage."""

from datetime import datetime
from typing import Any

STATUS_LABELS = {"OPEN": "Ouverte", "ACKNOWLEDGED": "Prise en charge", "RESOLVED": "Résolue"}
SEVERITY_LABELS = {"HIGH": "Élevée", "MEDIUM": "Moyenne", "LOW": "Faible", "UNKNOWN": "Inconnue"}
NATURE_LABELS = {"FACT": "Fait", "RISK": "Risque", "MISSING_INFORMATION": "Information manquante"}
SEVERITY_ORDER = {"UNKNOWN": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
MISSION_LABELS = {
    "PLANNED": "Planifiée",
    "IN_TRANSIT": "En transit",
    "DELIVERED": "Livrée",
    "BLOCKED": "Bloquée",
    "CANCELLED": "Annulée",
    "UNKNOWN": "Inconnu",
}
ACTION_LABELS = {
    "OPENED": "Ouverture",
    "SEVERITY_CHANGED": "Changement de gravité",
    "NOTIFIED": "Notification",
    "REMINDED": "Relance",
    "ESCALATED": "Escalade",
    "ACKNOWLEDGED": "Prise en charge",
    "RESOLVED": "Résolution",
    "AUTO_RESOLVED": "Résolution automatique",
}
NOT_PROVIDED = "Non renseigné"


def format_time(value: str | None) -> str:
    """Affiche une date du backend ; une date absente reste visible comme telle."""
    if not value:
        return NOT_PROVIDED
    moment = datetime.fromisoformat(value)
    return f"{moment:%d/%m/%Y %H:%M} UTC"


def label(labels: dict[str, str], value: str | None) -> str:
    return labels.get(value or "", value or NOT_PROVIDED)


def alert_row(alert: dict[str, Any]) -> dict[str, Any]:
    return {
        "N°": alert["id"],
        "TMS": alert["source_tms"],
        "Mission": alert["mission_external_id"],
        "Gravité": label(SEVERITY_LABELS, alert["severity"]),
        "Statut": label(STATUS_LABELS, alert["status"]),
        "Nature": label(NATURE_LABELS, alert["nature"]),
        "Message": alert["message"],
        "Escalade": alert["escalation_level"],
        "Ouverte le": format_time(alert["opened_at"]),
    }


def sort_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordre d'affichage : non prise en charge, grave, escaladée, ancienne."""
    return sorted(
        alerts,
        key=lambda a: (
            a["status"] != "OPEN",
            SEVERITY_ORDER.get(a["severity"], 0),
            -a["escalation_level"],
            a["opened_at"],
        ),
    )


def mission_row(mission: dict[str, Any]) -> dict[str, Any]:
    return {
        "Mission": mission["external_id"],
        "Statut": label(MISSION_LABELS, mission["status"]),
        "Client": mission["customer_name"],
        "Trajet": f"{mission['origin_city']} vers {mission['destination_city']}",
        "Chauffeur": mission.get("driver_name") or NOT_PROVIDED,
        "Arrivée prévue": format_time(mission["planned_arrival"]),
        "Arrivée estimée": format_time(mission.get("estimated_arrival")),
        "Dernière mise à jour": format_time(mission["updated_at"]),
    }
