"""Génère un brouillon de configuration TMS depuis sa documentation OpenAPI, puis l'essaie.

Le brouillon n'est jamais activé : il est écrit dans config/tms_drafts/ pour relecture humaine.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError

from backend.agents.llm_client import OllamaClient
from backend.core.config import PROJECT_ROOT, get_settings
from backend.integrations.errors import TmsError
from backend.integrations.generic_client import GenericTmsClient, create_http_client
from backend.integrations.openapi_parser import OpenApiError, parse_openapi
from backend.integrations.tms_onboarding import generate_draft
from backend.schemas.canonical import MissionStatus
from backend.schemas.tms_config import TmsConfig

DRAFTS_DIR = PROJECT_ROOT / "config" / "tms_drafts"
LLM_TIMEOUT_SECONDS = 600


def probe(client: GenericTmsClient) -> tuple[list[str], list[str]]:
    """Appelle réellement le TMS et retourne le bilan et les anomalies constatées."""
    missions = client.get_missions()
    incidents = client.get_incidents()
    report = [
        f"Missions : {len(missions.items)} lues, {len(missions.rejected)} rejetées",
        f"Incidents : {len(incidents.items)} lus, {len(incidents.rejected)} rejetés",
    ]
    issues = [f"Mission rejetée {r.position} : {r.reason}" for r in missions.rejected[:3]]
    issues += [f"Incident rejeté {r.position} : {r.reason}" for r in incidents.rejected[:3]]
    if not missions.items:
        return report, issues + ["Aucune mission lue : le brouillon ne peut pas être vérifié"]
    events = client.get_mission_events(missions.items[0].external_id)
    report.append(
        f"Événements de {missions.items[0].external_id} : "
        f"{len(events.items)} lus, {len(events.rejected)} rejetés"
    )
    issues += [f"Événement rejeté {r.position} : {r.reason}" for r in events.rejected[:3]]
    unknown = [m.external_id for m in missions.items if m.status == MissionStatus.UNKNOWN]
    if unknown:
        issues.append(f"Statut non traduit pour : {', '.join(unknown)}")
    inverted = [m.external_id for m in missions.items if m.planned_departure > m.planned_arrival]
    if inverted:
        issues.append(f"Départ prévu après l'arrivée prévue pour : {', '.join(inverted)}")
    return report, issues


def write_draft(path: Path, config: TmsConfig, issues: list[str]) -> None:
    header = [f"# BROUILLON généré le {datetime.now():%d/%m/%Y %H:%M} : à relire avant toute activation."]
    header += [f"# À vérifier : {issue}" for issue in issues] or ["# Aucune anomalie détectée automatiquement."]
    data = config.model_dump(mode="json", exclude_none=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    path.write_text("\n".join(header) + "\n" + body, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="Identifiant du TMS, par exemple tms_gamma")
    parser.add_argument("--name", required=True, help="Nom affiché, par exemple 'TMS Gamma'")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-env", required=True, help="Variable d'environnement contenant la clé")
    args = parser.parse_args()

    settings = get_settings()
    llm = OllamaClient(settings.ollama_base_url, settings.llm_model, LLM_TIMEOUT_SECONDS)
    try:
        spec = httpx.get(f"{args.base_url}/openapi.json", timeout=30).raise_for_status().json()
        api = parse_openapi(spec)
        draft = generate_draft(llm, api, args.id, args.name, args.base_url, args.key_env)
    except (httpx.HTTPError, OpenApiError) as error:
        print(f"Échec : {error}")
        return 1
    if not draft.config:
        print("Brouillon impossible :", *draft.issues, sep="\n- ")
        return 1
    try:
        config = TmsConfig.model_validate(draft.config)
    except ValidationError as error:
        print("Brouillon invalide :", *draft.issues, str(error), sep="\n- ")
        return 1

    issues = list(draft.issues)
    try:
        with create_http_client(config) as http:
            report, found = probe(GenericTmsClient(config, http))
    except (TmsError, LookupError) as error:
        report, found = [], [f"Essai réel impossible : {type(error).__name__}: {error}"]
    issues += found
    path = DRAFTS_DIR / f"{args.id}.yaml"
    write_draft(path, config, issues)
    print(f"Brouillon : {path}", *report, sep="\n")
    if issues:
        print("À vérifier :", *issues, sep="\n- ")
    else:
        print("Aucune anomalie détectée.")
    print("Relisez le fichier : rien n'est activé tant qu'il n'est pas déplacé dans config/tms/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
