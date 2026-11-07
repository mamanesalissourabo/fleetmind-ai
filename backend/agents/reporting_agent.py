"""Rapports du chatbot : le code rassemble les données, le LLM ne rédige qu'un résumé contrôlé."""

import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agents.common import describe_alert, is_faithful, other_tms_names
from backend.agents.llm_client import LlmUnavailableError, OllamaClient
from backend.integrations.errors import TmsError
from backend.ml.predict import DelayRiskModel, ModelUnavailableError
from backend.models.alert import Alert
from backend.models.monitoring_run import MonitoringRun
from backend.schemas.alerts import AlertStatus
from backend.schemas.canonical import Mission, MissionStatus
from backend.schemas.chat import (
    ChatIntent,
    Report,
    ReportItem,
    ReportPeriod,
    SummarySource,
    TmsScope,
)
from backend.schemas.findings import CycleStatus, FindingNature
from backend.schemas.tms_config import TmsConfig
from backend.services.cycle_runner import ClientOpener
from backend.services.monitoring import MonitoringService

DELAY_RULES = {"estimated_delay", "overdue_arrival"}
MISSING_RULES = {"missing_information", "stale_data"}
MAX_ITEMS_FOR_LLM = 12
RISK_THRESHOLD = 0.5
PREDICTABLE_STATUSES = {MissionStatus.PLANNED, MissionStatus.IN_TRANSIT, MissionStatus.BLOCKED}
MODEL_WARNING = "Estimation de démonstration : modèle entraîné sur des données synthétiques."
STATUS_LABELS = {
    "PLANNED": "planifiée(s)",
    "IN_TRANSIT": "en transit",
    "DELIVERED": "livrée(s)",
    "BLOCKED": "bloquée(s)",
    "CANCELLED": "annulée(s)",
    "UNKNOWN": "au statut inconnu",
}
EMPTY_MESSAGES = {
    ChatIntent.OPEN_INCIDENTS: "Aucun incident ouvert",
    ChatIntent.OPEN_ALERTS: "Aucune alerte active",
    ChatIntent.DELIVERY_SUMMARY: "Aucune mission",
    ChatIntent.DELAYED_MISSIONS: "Aucun retard détecté",
    ChatIntent.MISSING_INFORMATION: "Aucune information manquante détectée",
    ChatIntent.DELAY_RISK: "Aucune mission évaluée au-dessus du seuil de risque",
}
WRITER_PROMPT = (
    "Rédige en français, en deux phrases au maximum, une synthèse pour un exploitant de "
    "transport, à partir des seules données fournies. N'invente rien, n'ajoute aucun chiffre "
    "ni conseil, et ne décris pas ton raisonnement."
)
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"synthese": {"type": "string"}},
    "required": ["synthese"],
}


def _in_period(period: ReportPeriod | None, moment: datetime) -> bool:
    return period is None or period.contains(moment)


def _late_deliveries(tms_id: str, missions: list[Mission]) -> list[ReportItem]:
    """Constate un écart déjà réalisé : c'est un fait, pas une règle à appliquer."""
    items = []
    for mission in missions:
        if mission.actual_arrival is None or mission.actual_arrival <= mission.planned_arrival:
            continue
        delay = int((mission.actual_arrival - mission.planned_arrival).total_seconds() // 60)
        items.append(
            ReportItem(
                nature=FindingNature.FACT, source_tms=tms_id, mission_id=mission.external_id,
                text=f"Mission {mission.external_id} : livrée {delay} min après l'heure prévue",
            )
        )
    return items


class ReportingAgent:
    def __init__(
        self,
        configs: list[TmsConfig],
        open_client: ClientOpener,
        monitoring: MonitoringService,
        session: Session,
        now: Callable[[], datetime],
        llm: OllamaClient,
        risk_model_factory: Callable[[], DelayRiskModel],
    ) -> None:
        self._configs = {config.id: config for config in configs}
        self._open_client = open_client
        self._monitoring = monitoring
        self._session = session
        self._now = now
        self._llm = llm
        self._risk_model_factory = risk_model_factory

    def build_report(
        self, intent: ChatIntent, tms_id: str | None, period: ReportPeriod | None = None
    ) -> Report:
        """Rassemble les données du périmètre demandé, sans jamais les inventer."""
        report = Report(intent=intent, reference_time=self._now(), period=period, scope=[])
        builders = {
            ChatIntent.OPEN_INCIDENTS: self._open_incidents,
            ChatIntent.OPEN_ALERTS: self._open_alerts,
            ChatIntent.DELIVERY_SUMMARY: self._delivery_summary,
            ChatIntent.DELAY_RISK: self._delay_risk,
            ChatIntent.DELAYED_MISSIONS: lambda config, rep: self._findings(config, rep, DELAY_RULES),
            ChatIntent.MISSING_INFORMATION: lambda config, rep: self._findings(config, rep, MISSING_RULES),
        }
        configs = [self._configs[tms_id]] if tms_id else list(self._configs.values())
        for config in configs:
            try:
                builders[intent](config, report)
            except (TmsError, LookupError) as error:
                self._mark_unavailable(report, config.id, f"{type(error).__name__}: {error}")
        return report

    def summarize(self, question: str, report: Report) -> tuple[str, SummarySource]:
        """Le LLM ne rédige que si les données sont complètes, et son texte est vérifié."""
        fallback = self._deterministic_summary(report)
        if (
            not report.items
            or not report.complete
            or report.intent in (ChatIntent.DELIVERY_SUMMARY, ChatIntent.DELAY_RISK)
            or len(report.items) > MAX_ITEMS_FOR_LLM
        ):
            return fallback, SummarySource.DETERMINISTIC
        payload = {
            "question": question,
            "perimetre": [tms.tms_id for tms in report.scope],
            "periode": report.period.label if report.period else "état actuel",
            "nombre_de_constats": len(report.items),
            "constats": [item.text for item in report.items],
        }
        try:
            result = self._llm.complete_json(
                WRITER_PROMPT, json.dumps(payload, ensure_ascii=False), SUMMARY_SCHEMA
            )
        except LlmUnavailableError:
            return fallback, SummarySource.DETERMINISTIC
        summary = str(result.get("synthese", "")).strip()
        if summary and self._is_faithful(summary, report):
            return summary, SummarySource.LLM
        return fallback, SummarySource.DETERMINISTIC

    def _is_faithful(self, summary: str, report: Report) -> bool:
        """Refuse ce qui est inventé (identifiant, nombre, autre TMS) et ce qui est oublié."""
        allowed = " ".join(
            [item.text for item in report.items] + [str(len(report.items))]
            + ([report.period.label] if report.period else [])
            + [str(sum(item.source_tms == tms.tms_id for item in report.items)) for tms in report.scope]
        )
        return is_faithful(
            summary,
            allowed,
            {item.mission_id for item in report.items if item.mission_id not in (None, "*")},
            other_tms_names(self._configs.values(), {tms.tms_id for tms in report.scope}),
        )

    @staticmethod
    def _deterministic_summary(report: Report) -> str:
        unavailable = [tms.tms_id for tms in report.scope if not tms.available]
        where = f"la période {report.period.label}" if report.period else "le périmètre consulté"
        if report.intent == ChatIntent.DELIVERY_SUMMARY and report.complete:
            prefix = f"Sur la période {report.period.label} : " if report.period else ""
            return prefix + ". ".join(item.text for item in report.items) + "."
        parts = [
            f"{len(report.by_nature(FindingNature.FACT))} fait(s), "
            f"{len(report.by_nature(FindingNature.RISK))} risque(s) et "
            f"{len(report.by_nature(FindingNature.MISSING_INFORMATION))} information(s) "
            f"manquante(s) sur {where}."
            if report.items
            else f"{EMPTY_MESSAGES[report.intent]} sur {where}."
        ]
        if report.intent == ChatIntent.DELAY_RISK:
            parts.extend(report.notes)
            parts.append(MODEL_WARNING)
        missions = sorted(
            {item.mission_id for item in report.items if item.mission_id not in (None, "*")}
        )
        if missions:
            parts.append(f"Missions concernées : {', '.join(missions)}.")
        if unavailable:
            parts.append(f"Résultat incomplet : données indisponibles pour {', '.join(unavailable)}.")
        elif not report.complete:
            parts.append("Résultat incomplet : des enregistrements n'ont pas pu être lus.")
        return " ".join(parts)

    @staticmethod
    def _mark_unavailable(report: Report, tms_id: str, error: str) -> None:
        report.scope.append(TmsScope(tms_id=tms_id, available=False, error=error))
        report.items.append(
            ReportItem(
                nature=FindingNature.MISSING_INFORMATION,
                source_tms=tms_id,
                text=f"{tms_id} : données indisponibles, aucune conclusion possible",
            )
        )

    def _open_incidents(self, config: TmsConfig, report: Report) -> None:
        with self._open_client(config) as client:
            fetched = client.get_incidents()
        report.scope.append(
            TmsScope(
                tms_id=config.id, available=True, data_as_of=datetime.now(UTC),
                rejected_records=len(fetched.rejected),
            )
        )
        report.items.extend(
            ReportItem(
                nature=FindingNature.FACT, source_tms=config.id,
                mission_id=incident.mission_external_id,
                text=f"Incident {incident.external_id}, mission {incident.mission_external_id}, "
                     f"gravité {incident.severity.value} : {incident.description}",
            )
            for incident in fetched.items
            if incident.is_open and _in_period(report.period, incident.opened_at)
        )

    def _delay_risk(self, config: TmsConfig, report: Report) -> None:
        """Le modèle informe : son estimation ne crée ni alerte ni notification."""
        try:
            model = self._risk_model_factory()
        except ModelUnavailableError as error:
            self._mark_unavailable(report, config.id, str(error))
            return
        with self._open_client(config) as client:
            missions = client.get_missions()
            incidents = client.get_incidents()
        report.scope.append(
            TmsScope(
                tms_id=config.id, available=True, data_as_of=datetime.now(UTC),
                rejected_records=len(missions.rejected) + len(incidents.rejected),
            )
        )
        open_counts = Counter(
            incident.mission_external_id for incident in incidents.items if incident.is_open
        )
        active = [
            mission for mission in missions.items
            if mission.status in PREDICTABLE_STATUSES and mission.actual_arrival is None
        ]
        evaluated = [m for m in active if report.reference_time < m.planned_arrival]
        predictions = [
            (mission, model.predict(mission, report.reference_time, open_counts[mission.external_id]))
            for mission in evaluated
        ]
        predictions.sort(key=lambda pair: pair[1].probability, reverse=True)
        report.items.extend(
            ReportItem(
                nature=FindingNature.RISK, source_tms=config.id, mission_id=mission.external_id,
                text=f"Mission {mission.external_id} : risque de retard estimé à "
                     f"{prediction.probability:.0%}" + (
                         f", principaux facteurs : {', '.join(prediction.main_factors)}"
                         if prediction.main_factors else ""
                     ),
            )
            for mission, prediction in predictions
            if prediction.probability >= RISK_THRESHOLD
        )
        report.notes.append(
            f"{config.id} : {len(evaluated)} mission(s) évaluée(s), "
            f"{len(active) - len(evaluated)} dont l'heure d'arrivée est déjà dépassée."
        )

    def _delivery_summary(self, config: TmsConfig, report: Report) -> None:
        with self._open_client(config) as client:
            fetched = client.get_missions()
        report.scope.append(
            TmsScope(
                tms_id=config.id, available=True, data_as_of=datetime.now(UTC),
                rejected_records=len(fetched.rejected),
            )
        )
        missions = [m for m in fetched.items if _in_period(report.period, m.planned_arrival)]
        counts = Counter(mission.status.value for mission in missions)
        detail = ", ".join(
            f"{count} {STATUS_LABELS[status]}" for status, count in sorted(counts.items())
        )
        report.items.append(
            ReportItem(
                nature=FindingNature.FACT, source_tms=config.id,
                text=f"{config.id} : {len(missions)} mission(s) : {detail or 'aucune'}",
            )
        )

    def _findings(self, config: TmsConfig, report: Report, rule_types: set[str]) -> None:
        """Applique les règles propres au TMS, sans créer d'alerte ni de notification."""
        with self._open_client(config) as client:
            cycle = self._monitoring.run_cycle(client, report.reference_time)
            if cycle.status == CycleStatus.FAILED:
                self._mark_unavailable(report, config.id, cycle.error or "inconnue")
                return
            missions = [
                mission
                for mission in client.get_missions().items
                if _in_period(report.period, mission.planned_arrival)
            ]
        selected = {mission.external_id for mission in missions}
        report.scope.append(
            TmsScope(
                tms_id=config.id, available=True, data_as_of=cycle.retrieved_at,
                ruleset_version=cycle.ruleset_version,
                rejected_records=len(cycle.rejected_records),
            )
        )
        report.items.extend(
            ReportItem(
                nature=finding.nature, source_tms=config.id,
                mission_id=finding.mission_external_id,
                text=f"Mission {finding.mission_external_id} : {finding.message}",
            )
            for finding in cycle.findings
            if finding.rule_type in rule_types and finding.mission_external_id in selected
        )
        report.items.extend(
            ReportItem(
                nature=FindingNature.MISSING_INFORMATION, source_tms=config.id,
                mission_id=mission_id,
                text=f"Mission {mission_id} : statut inconnu, règles non applicables",
            )
            for mission_id in cycle.unknown_status_missions
            if mission_id in selected
        )
        if rule_types == DELAY_RULES:
            report.items.extend(_late_deliveries(config.id, missions))

    def _open_alerts(self, config: TmsConfig, report: Report) -> None:
        """Lit les alertes en base ; leur fraîcheur est celle du dernier cycle réussi."""
        last_success = self._session.scalar(
            select(func.max(MonitoringRun.started_at)).where(
                MonitoringRun.source_tms == config.id,
                MonitoringRun.status != CycleStatus.FAILED.value,
            )
        )
        report.scope.append(
            TmsScope(
                tms_id=config.id, available=last_success is not None, data_as_of=last_success
            )
        )
        if last_success is None:
            report.items.append(
                ReportItem(
                    nature=FindingNature.MISSING_INFORMATION, source_tms=config.id,
                    text=f"{config.id} : aucun cycle de surveillance réussi à ce jour",
                )
            )
        alerts = self._session.scalars(
            select(Alert)
            .where(Alert.source_tms == config.id, Alert.status != AlertStatus.RESOLVED.value)
            .order_by(Alert.id)
        )
        report.items.extend(
            ReportItem(
                nature=FindingNature(alert.nature), source_tms=config.id,
                mission_id=alert.mission_external_id,
                text=describe_alert(alert),
            )
            for alert in alerts
            if _in_period(report.period, alert.opened_at)
        )
