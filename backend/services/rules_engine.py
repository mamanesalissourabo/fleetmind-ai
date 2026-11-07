"""Moteur de règles déterministe : mêmes données et mêmes règles donnent le même résultat."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from backend.schemas.canonical import Incident, Mission, Severity
from backend.schemas.findings import DetailValue, Finding, FindingNature
from backend.schemas.rules import (
    EstimatedDelayRule,
    MissingInformationRule,
    OpenIncidentRule,
    OverdueArrivalRule,
    RouteDeviationRule,
    RuleSet,
    StaleDataRule,
)
from backend.services.geo import distance_to_route_km

SEVERITY_RANK = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.UNKNOWN: 4}


@dataclass(frozen=True)
class Evidence:
    """Ce qu'une règle a constaté, avant ajout de son contexte."""

    nature: FindingNature
    message: str
    details: dict[str, DetailValue]


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _check_estimated_delay(
    mission: Mission, rule: EstimatedDelayRule, _: datetime
) -> Evidence | None:
    if mission.estimated_arrival is None:
        return None
    delay = _minutes(mission.estimated_arrival - mission.planned_arrival)
    if delay < rule.min_delay_minutes:
        return None
    return Evidence(
        FindingNature.RISK,
        f"Arrivée estimée {delay} min après l'heure prévue",
        {"delay_minutes": delay, "threshold_minutes": rule.min_delay_minutes},
    )


def _check_overdue_arrival(
    mission: Mission, rule: OverdueArrivalRule, reference_time: datetime
) -> Evidence | None:
    overdue = _minutes(reference_time - mission.planned_arrival)
    if mission.actual_arrival is not None or overdue < rule.grace_minutes:
        return None
    return Evidence(
        FindingNature.FACT,
        f"Heure d'arrivée prévue dépassée de {overdue} min, sans arrivée enregistrée",
        {"overdue_minutes": overdue, "grace_minutes": rule.grace_minutes},
    )


def _check_route_deviation(
    mission: Mission, rule: RouteDeviationRule, _: datetime
) -> Evidence | None:
    if mission.last_position is None or len(mission.planned_route) < 2:
        return None
    distance = round(distance_to_route_km(mission.last_position, mission.planned_route), 1)
    if distance <= rule.max_distance_km:
        return None
    return Evidence(
        FindingNature.FACT,
        f"Dernière position à {distance} km de l'itinéraire prévu",
        {"distance_km": distance, "threshold_km": rule.max_distance_km},
    )


def _check_missing_information(
    mission: Mission, rule: MissingInformationRule, _: datetime
) -> Evidence | None:
    missing = [name for name in rule.fields if getattr(mission, name) is None]
    if not missing:
        return None
    return Evidence(
        FindingNature.MISSING_INFORMATION,
        f"Informations absentes : {', '.join(missing)}",
        {"missing_fields": missing},
    )


def _check_stale_data(
    mission: Mission, rule: StaleDataRule, reference_time: datetime
) -> Evidence | None:
    age = _minutes(reference_time - mission.updated_at)
    if age <= rule.max_age_minutes:
        return None
    return Evidence(
        FindingNature.MISSING_INFORMATION,
        f"Aucune mise à jour depuis {age} min",
        {"age_minutes": age, "threshold_minutes": rule.max_age_minutes},
    )


MissionCheck = Callable[[Mission, Any, datetime], Evidence | None]
MISSION_CHECKS: dict[type, MissionCheck] = {
    EstimatedDelayRule: _check_estimated_delay,
    OverdueArrivalRule: _check_overdue_arrival,
    RouteDeviationRule: _check_route_deviation,
    MissingInformationRule: _check_missing_information,
    StaleDataRule: _check_stale_data,
}


def evaluate_mission(
    mission: Mission, ruleset: RuleSet, reference_time: datetime
) -> list[Finding]:
    """Applique à une mission les règles correspondant à son statut."""
    findings = []
    for rule in ruleset.rules:
        check = MISSION_CHECKS.get(type(rule))
        if check is None or mission.status not in rule.statuses:
            continue
        evidence = check(mission, rule, reference_time)
        if evidence is not None:
            findings.append(
                Finding(
                    source_tms=mission.source_tms,
                    mission_external_id=mission.external_id,
                    rule_id=rule.id,
                    rule_type=rule.type,
                    ruleset_version=ruleset.version,
                    nature=evidence.nature,
                    severity=rule.severity,
                    message=evidence.message,
                    details=evidence.details,
                    reference_time=reference_time,
                )
            )
    return findings


def evaluate_incident(
    incident: Incident, ruleset: RuleSet, reference_time: datetime
) -> list[Finding]:
    """Signale un incident ouvert ; une gravité inconnue n'est jamais ignorée."""
    if not incident.is_open:
        return []
    return [
        Finding(
            source_tms=incident.source_tms,
            mission_external_id=incident.mission_external_id,
            subject_id=incident.external_id,
            rule_id=rule.id,
            rule_type=rule.type,
            ruleset_version=ruleset.version,
            nature=FindingNature.FACT,
            severity=incident.severity,
            message=f"Incident ouvert {incident.external_id} : {incident.description}",
            details={"incident_id": incident.external_id, "incident_type": incident.type},
            reference_time=reference_time,
        )
        for rule in ruleset.rules
        if isinstance(rule, OpenIncidentRule)
        and SEVERITY_RANK[incident.severity] >= SEVERITY_RANK[rule.min_severity]
    ]
