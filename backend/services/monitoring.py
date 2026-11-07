"""Cycle de surveillance : récupère les données d'un TMS et lui applique ses règles."""

from datetime import UTC, datetime

from backend.integrations.errors import TmsConfigError, TmsError
from backend.integrations.generic_client import GenericTmsClient
from backend.schemas.canonical import MissionStatus
from backend.schemas.findings import CycleStatus, MonitoringReport
from backend.services.rules_engine import evaluate_incident, evaluate_mission
from backend.services.rules_registry import RulesRegistry


class MonitoringService:
    """Exécute des cycles de surveillance, TMS par TMS."""

    def __init__(self, rules: RulesRegistry) -> None:
        self._rules = rules

    def run_cycle(
        self, client: GenericTmsClient, reference_time: datetime | None = None
    ) -> MonitoringReport:
        """Évalue un TMS ; toute erreur donne un rapport FAILED, sans conclusion partielle."""
        reference = reference_time or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("L'heure de référence doit indiquer un fuseau horaire")

        try:
            ruleset = self._rules.get(client.tms_id)
            missions = client.get_missions()
            incidents = client.get_incidents()
        except (TmsError, TmsConfigError, LookupError) as error:
            return MonitoringReport(
                source_tms=client.tms_id,
                status=CycleStatus.FAILED,
                reference_time=reference,
                retrieved_at=datetime.now(UTC),
                error=f"{type(error).__name__}: {error}",
            )

        findings = [
            finding
            for mission in missions.items
            for finding in evaluate_mission(mission, ruleset, reference)
        ] + [
            finding
            for incident in incidents.items
            for finding in evaluate_incident(incident, ruleset, reference)
        ]
        rejected = [
            f"missions[{record.position}]: {record.reason}" for record in missions.rejected
        ] + [
            f"incidents[{record.position}]: {record.reason}" for record in incidents.rejected
        ]
        return MonitoringReport(
            source_tms=client.tms_id,
            status=CycleStatus.PARTIAL if rejected else CycleStatus.COMPLETE,
            reference_time=reference,
            retrieved_at=datetime.now(UTC),
            ruleset_version=ruleset.version,
            ruleset_origin=ruleset.origin,
            missions_evaluated=len(missions.items),
            findings=findings,
            unknown_status_missions=[
                mission.external_id
                for mission in missions.items
                if mission.status == MissionStatus.UNKNOWN
            ],
            rejected_records=rejected,
        )
