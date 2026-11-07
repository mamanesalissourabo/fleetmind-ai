"""Génération d'un brouillon de configuration TMS : le LLM propose, le code contrôle, l'humain valide."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.integrations.openapi_parser import (
    ApiSummary,
    EndpointInfo,
    FieldInfo,
    FieldKind,
    infer_pagination,
)
from backend.schemas.canonical import (
    EventType,
    Incident,
    Mission,
    MissionEvent,
    MissionStatus,
    Severity,
)

MODELS = {"missions": Mission, "events": MissionEvent, "incidents": Incident}
RETRY_PROMPT = """Ces champs sont indispensables et n'ont pas encore été associés. Pour chacun, choisis parmi les champs restants de l'API celui qui a le même sens. Réponds null seulement si aucun ne convient vraiment."""
TEXT = "text"
ENUM = "enum"
RESOURCES: dict[str, dict[str, tuple[str, str]]] = {
    "missions": {
        "external_id": (TEXT, "identifiant unique de la mission"),
        "status": (ENUM, "état de la mission"),
        "customer_name": (TEXT, "nom du client"),
        "vehicle_plate": (TEXT, "immatriculation du véhicule"),
        "driver_name": (TEXT, "nom du chauffeur"),
        "origin_city": (TEXT, "ville de départ"),
        "destination_city": (TEXT, "ville d'arrivée"),
        "planned_departure": (FieldKind.DATETIME, "départ prévu"),
        "planned_arrival": (FieldKind.DATETIME, "arrivée prévue"),
        "actual_departure": (FieldKind.DATETIME, "départ réel"),
        "actual_arrival": (FieldKind.DATETIME, "arrivée réelle"),
        "estimated_arrival": (FieldKind.DATETIME, "arrivée estimée"),
        "planned_route": (FieldKind.GEO_LIST, "itinéraire prévu"),
        "last_position": (FieldKind.GEO, "dernière position"),
        "last_position_at": (FieldKind.DATETIME, "heure de la dernière position"),
        "updated_at": (FieldKind.DATETIME, "dernière mise à jour"),
    },
    "events": {
        "external_id": (TEXT, "identifiant unique de l'événement"),
        "mission_external_id": (TEXT, "identifiant de la mission concernée"),
        "type": (ENUM, "nature de l'événement"),
        "occurred_at": (FieldKind.DATETIME, "heure de l'événement"),
        "description": (TEXT, "description de l'événement"),
    },
    "incidents": {
        "external_id": (TEXT, "identifiant unique de l'incident"),
        "mission_external_id": (TEXT, "identifiant de la mission concernée"),
        "type": (TEXT, "catégorie de l'incident"),
        "severity": (ENUM, "gravité de l'incident"),
        "description": (TEXT, "description de l'incident"),
        "opened_at": (FieldKind.DATETIME, "date d'ouverture"),
        "resolved_at": (FieldKind.DATETIME, "date de clôture"),
    },
}
CANONICAL_VALUES = {
    "status": [value.value for value in MissionStatus],
    "type": [value.value for value in EventType],
    "severity": [value.value for value in Severity],
}
RESOURCE_MEANINGS = {
    "missions": "liste des missions, transports ou expéditions",
    "events": "historique ou suivi d'une seule mission (route avec un identifiant dans le chemin)",
    "incidents": "liste des incidents, anomalies ou problèmes",
}
ENDPOINT_PROMPT = """Tu associes les routes d'une API de transport à trois besoins. Choisis la route la plus adaptée à chaque besoin, parmi celles proposées, ou null si aucune ne convient.

Exemple de réponse : {"missions": "/a", "events": "/a/{id}/log", "incidents": "/b"}"""
MAPPING_PROMPT = """Tu associes les champs d'une API de transport aux champs attendus. Pour chaque champ attendu, choisis le champ de l'API qui a le même sens, en te fiant à son nom et à sa description, ou null si aucun ne convient. Ne devine pas : une date prévue n'est pas une date réelle.

Exemple de réponse : {"customer_name": "client.nom", "driver_name": null}"""
ENUM_PROMPT = """Tu traduis les valeurs d'une API de transport vers les valeurs attendues. Pour chaque valeur de l'API, choisis la valeur attendue de même sens. Choisis UNKNOWN si le sens n'est pas clair."""


class JsonLlm(Protocol):
    def complete_json(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 200
    ) -> dict[str, Any]: ...


@dataclass
class Draft:
    config: dict[str, Any]
    issues: list[str] = field(default_factory=list)


def _nullable_enum(values: list[str]) -> dict[str, Any]:
    return {"anyOf": [{"type": "string", "enum": values}, {"type": "null"}]}


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties)}


def choose_endpoints(llm: JsonLlm, api: ApiSummary) -> dict[str, EndpointInfo | None]:
    """Seules les routes de la bonne forme sont proposées, et la réponse est revérifiée."""
    with_id = [e for e in api.endpoints if e.path_params]
    without_id = [e for e in api.endpoints if not e.path_params]
    choices = {"missions": without_id, "events": with_id, "incidents": without_id}
    lines = [f"{e.path} : {e.description}" for e in api.endpoints]
    needs = [f"{name} : {RESOURCE_MEANINGS[name]}" for name in choices]
    answer = llm.complete_json(
        ENDPOINT_PROMPT,
        "Besoins :\n" + "\n".join(needs) + "\nRoutes :\n" + "\n".join(lines),
        _object_schema(
            {name: _nullable_enum([e.path for e in routes]) for name, routes in choices.items()}
        ),
        max_tokens=80,
    )
    return {
        name: next((e for e in routes if e.path == answer.get(name)), None)
        for name, routes in choices.items()
    }


def _candidates(endpoint: EndpointInfo, kind: str) -> list[FieldInfo]:
    """Ne propose que les champs du bon type : le LLM ne peut pas citer un champ inexistant."""
    if kind in (TEXT, ENUM):
        return [f for f in endpoint.fields if f.kind in (FieldKind.TEXT, FieldKind.NUMBER)]
    return [f for f in endpoint.fields if f.kind == kind]


def map_fields(llm: JsonLlm, resource: str, endpoint: EndpointInfo) -> dict[str, FieldInfo]:
    expected = RESOURCES[resource]
    properties, lines = {}, []
    for name, (kind, meaning) in expected.items():
        candidates = _candidates(endpoint, kind)
        if candidates:
            properties[name] = _nullable_enum([f.path for f in candidates])
            lines.append(f"{name} ({meaning})")
    catalogue = [f"{f.path} ({f.kind.value}) : {f.description}" for f in endpoint.fields]
    answer = llm.complete_json(
        MAPPING_PROMPT,
        "Champs attendus :\n" + "\n".join(lines) + "\nChamps de l'API :\n" + "\n".join(catalogue),
        _object_schema(properties),
        max_tokens=320,
    )
    mapped: dict[str, FieldInfo] = {}
    _accept(answer, expected, endpoint, mapped)
    _retry_required_fields(llm, resource, endpoint, mapped)
    return mapped


def _accept(
    answer: dict[str, Any],
    expected: dict[str, tuple[str, str]],
    endpoint: EndpointInfo,
    mapped: dict[str, FieldInfo],
) -> None:
    """Ne garde que les champs réellement proposés : le reste de la réponse est ignoré."""
    for name, (kind, _) in expected.items():
        match = next((f for f in _candidates(endpoint, kind) if f.path == answer.get(name)), None)
        if match:
            mapped[name] = match


def _retry_required_fields(
    llm: JsonLlm, resource: str, endpoint: EndpointInfo, mapped: dict[str, FieldInfo]
) -> None:
    """Repose une question courte sur les seuls champs obligatoires restés vides."""
    expected = RESOURCES[resource]
    required = {
        name
        for name, info in MODELS[resource].model_fields.items()
        if info.is_required() and name in expected
    }
    used = {source.path for source in mapped.values()}
    properties, lines = {}, []
    for name in sorted(required - set(mapped)):
        kind, meaning = expected[name]
        free = [f for f in _candidates(endpoint, kind) if f.path not in used]
        if free:
            properties[name] = _nullable_enum([f.path for f in free])
            lines.append(f"{name} ({meaning})")
    if not properties:
        return
    catalogue = [f"{f.path} : {f.description}" for f in endpoint.fields if f.path not in used]
    answer = llm.complete_json(
        RETRY_PROMPT,
        "Champs à associer :\n" + "\n".join(lines) + "\nChamps restants :\n" + "\n".join(catalogue),
        _object_schema(properties),
        max_tokens=60,
    )
    _accept(answer, {name: expected[name] for name in properties}, endpoint, mapped)


def map_enum(llm: JsonLlm, name: str, source: FieldInfo) -> dict[str, str]:
    """Une valeur non traduite reste inconnue ; rien n'est supposé."""
    canonical = CANONICAL_VALUES[name]
    if not source.enum:
        return {}
    properties = {value: {"type": "string", "enum": canonical} for value in source.enum}
    answer = llm.complete_json(
        ENUM_PROMPT,
        f"Valeurs attendues : {', '.join(canonical)}\nValeurs de l'API ({source.description}) : "
        + ", ".join(source.enum),
        _object_schema(properties),
        max_tokens=100,
    )
    return {
        value: answer[value]
        for value in source.enum
        if answer.get(value) in canonical and answer[value] != "UNKNOWN"
    }


def _mapping_entry(kind: str, source: FieldInfo, values: dict[str, str]) -> str | dict[str, Any]:
    if kind == ENUM:
        return {"transform": "enum", "path": source.path, "values": values}
    if kind == FieldKind.DATETIME:
        return {"transform": "datetime", "path": source.path}
    if kind in (FieldKind.GEO, FieldKind.GEO_LIST):
        return {
            "transform": "geo", "path": source.path, "format": "object",
            "lat_key": source.lat_key, "lon_key": source.lon_key,
        }
    return source.path


def build_resource(llm: JsonLlm, resource: str, endpoint: EndpointInfo, draft: Draft) -> dict[str, Any]:
    fields = map_fields(llm, resource, endpoint)
    used: dict[str, str] = {}
    mapping: dict[str, Any] = {}
    for name, source in fields.items():
        kind = RESOURCES[resource][name][0]
        if source.path in used:
            draft.issues.append(f"{resource} : {source.path} utilisé pour {used[source.path]} et {name}")
        used[source.path] = name
        values = map_enum(llm, name, source) if kind == ENUM else {}
        if kind == ENUM and not values:
            draft.issues.append(f"{resource} : valeurs de {source.path} non traduites")
        mapping[name] = _mapping_entry(kind, source, values)
    for name in RESOURCES[resource]:
        if name not in mapping:
            draft.issues.append(f"{resource} : aucun champ trouvé pour {name}")
    path = endpoint.path
    for parameter in endpoint.path_params:
        path = path.replace("{" + parameter + "}", "{mission_id}")
    resource_config: dict[str, Any] = {"path": path, "mapping": mapping}
    if endpoint.items_path:
        resource_config["items_path"] = endpoint.items_path
    if resource == "events" or endpoint.total_path is None:
        resource_config["paginated"] = False
    return resource_config


def generate_draft(
    llm: JsonLlm, api: ApiSummary, tms_id: str, name: str, base_url: str, key_env: str
) -> Draft:
    """Assemble le brouillon ; ce qui manque est signalé, jamais comblé par une supposition."""
    draft = Draft(config={})
    endpoints = choose_endpoints(llm, api)
    for resource, endpoint in endpoints.items():
        if endpoint is None:
            draft.issues.append(f"Aucune route trouvée pour {resource}")
    if any(endpoint is None for endpoint in endpoints.values()):
        return draft
    pagination = infer_pagination(endpoints["missions"])
    if infer_pagination(endpoints["incidents"]) != pagination:
        draft.issues.append("Les incidents n'utilisent pas la même pagination que les missions")
    draft.config = {
        "id": tms_id,
        "name": name,
        "config_version": 1,
        "base_url": base_url,
        "auth": {"type": "api_key", "header": api.auth_header, "key_env": key_env},
        "pagination": pagination,
        "resources": {
            resource: build_resource(llm, resource, endpoint, draft)
            for resource, endpoint in endpoints.items()
        },
    }
    return draft
