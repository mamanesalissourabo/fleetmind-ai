"""Lecture d'une documentation OpenAPI : ce qui peut être déduit l'est par le code, sans LLM."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

LAT_NAMES = ("lat", "latitude")
LON_NAMES = ("lon", "lng", "long", "longitude")
TOTAL_NAMES = ("total", "count", "total_count", "totalcount")
PAGE_NAMES = ("page", "page_number", "pagenumber")
PAGE_SIZE_NAMES = ("page_size", "pagesize", "per_page", "perpage", "size", "limit")
MAX_DEPTH = 4


class OpenApiError(ValueError):
    """La documentation ne permet pas de déduire une information indispensable."""


class FieldKind(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    DATETIME = "datetime"
    GEO = "geo"
    GEO_LIST = "geo_list"


@dataclass(frozen=True)
class FieldInfo:
    path: str
    kind: FieldKind
    description: str = ""
    enum: tuple[str, ...] = ()
    lat_key: str = "lat"
    lon_key: str = "lon"


@dataclass(frozen=True)
class EndpointInfo:
    path: str
    description: str
    query_params: tuple[str, ...]
    path_params: tuple[str, ...]
    items_path: str | None
    total_path: str | None
    fields: tuple[FieldInfo, ...]


@dataclass(frozen=True)
class ApiSummary:
    title: str
    auth_header: str
    endpoints: tuple[EndpointInfo, ...] = field(default=())


def _resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Suit les références et retire la variante null des champs facultatifs."""
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = {**node, **{key: value for key, value in schema.items() if key != "$ref"}}
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            variants = [item for item in schema[keyword] if item.get("type") != "null"]
            if len(variants) == 1:
                merged = {**_resolve(variants[0], spec)}
                if schema.get("description"):
                    merged["description"] = schema["description"]
                return merged
    return schema


def _geo_keys(schema: dict[str, Any]) -> tuple[str, str] | None:
    names = schema.get("properties", {})
    lat = next((name for name in names if name.lower() in LAT_NAMES), None)
    lon = next((name for name in names if name.lower() in LON_NAMES), None)
    return (lat, lon) if lat and lon else None


def _flatten(
    schema: dict[str, Any], spec: dict[str, Any], prefix: str = "", depth: int = 0, context: str = ""
) -> list[FieldInfo]:
    """Liste les champs d'un objet avec leur nature, y compris les champs imbriqués."""
    if depth > MAX_DEPTH:
        return []
    fields = []
    for name, raw in _resolve(schema, spec).get("properties", {}).items():
        item = _resolve(raw, spec)
        path = f"{prefix}{name}"
        own = item.get("description", "")
        description = f"{context} : {own}" if context and own else (own or context)
        kind = item.get("type")
        if kind == "array":
            element = _resolve(item.get("items", {}), spec)
            keys = _geo_keys(element)
            if keys:
                fields.append(FieldInfo(path, FieldKind.GEO_LIST, description, lat_key=keys[0], lon_key=keys[1]))
        elif kind == "object" or "properties" in item:
            keys = _geo_keys(item)
            if keys:
                fields.append(FieldInfo(path, FieldKind.GEO, description, lat_key=keys[0], lon_key=keys[1]))
            fields.extend(_flatten(item, spec, f"{path}.", depth + 1, description))
        elif item.get("format") == "date-time":
            fields.append(FieldInfo(path, FieldKind.DATETIME, description))
        elif kind in ("integer", "number"):
            enum = tuple(str(value) for value in item.get("enum", ()))
            fields.append(FieldInfo(path, FieldKind.NUMBER, description, enum))
        else:
            enum = tuple(str(value) for value in item.get("enum", ()))
            fields.append(FieldInfo(path, FieldKind.TEXT, description, enum))
    return fields


def _find_total(schema: dict[str, Any], spec: dict[str, Any], prefix: str = "", depth: int = 0) -> str | None:
    if depth > 2:
        return None
    for name, raw in schema.get("properties", {}).items():
        item = _resolve(raw, spec)
        if item.get("type") == "integer" and name.lower() in TOTAL_NAMES:
            return f"{prefix}{name}"
        if "properties" in item:
            found = _find_total(item, spec, f"{prefix}{name}.", depth + 1)
            if found:
                return found
    return None


def _parse_list_endpoint(path: str, operation: dict[str, Any], spec: dict[str, Any]) -> EndpointInfo | None:
    """Ne retient que les routes qui renvoient une liste d'objets."""
    content = operation.get("responses", {}).get("200", {}).get("content", {})
    schema = content.get("application/json", {}).get("schema")
    if not schema:
        return None
    body = _resolve(schema, spec)
    items_path = None
    element: dict[str, Any] | None = None
    if body.get("type") == "array":
        element = _resolve(body.get("items", {}), spec)
    else:
        for name, raw in body.get("properties", {}).items():
            item = _resolve(raw, spec)
            if item.get("type") == "array":
                items_path, element = name, _resolve(item.get("items", {}), spec)
                break
    if not element or "properties" not in element:
        return None
    parameters = [_resolve(parameter, spec) for parameter in operation.get("parameters", [])]
    return EndpointInfo(
        path=path,
        description=" ".join(
            part for part in (operation.get("summary"), operation.get("description")) if part
        ),
        query_params=tuple(p["name"] for p in parameters if p.get("in") == "query"),
        path_params=tuple(p["name"] for p in parameters if p.get("in") == "path"),
        items_path=items_path,
        total_path=_find_total(body, spec),
        fields=tuple(_flatten(element, spec)),
    )


def _auth_header(spec: dict[str, Any]) -> str:
    schemes = spec.get("components", {}).get("securitySchemes", {})
    for scheme in schemes.values():
        if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
            return scheme["name"]
    raise OpenApiError(
        "Seule l'authentification par clé d'API dans un en-tête est déduite automatiquement"
    )


def parse_openapi(spec: dict[str, Any]) -> ApiSummary:
    endpoints = []
    for path, methods in spec.get("paths", {}).items():
        operation = methods.get("get")
        if operation:
            endpoint = _parse_list_endpoint(path, operation, spec)
            if endpoint:
                endpoints.append(endpoint)
    if not endpoints:
        raise OpenApiError("Aucune route GET renvoyant une liste d'objets")
    return ApiSummary(spec.get("info", {}).get("title", ""), _auth_header(spec), tuple(endpoints))


def infer_pagination(endpoint: EndpointInfo, page_size: int = 25) -> dict[str, Any]:
    """Déduit la pagination des noms de paramètres ; sans correspondance sûre, une erreur est levée."""
    names = {name.lower(): name for name in endpoint.query_params}
    if endpoint.total_path is None:
        raise OpenApiError(f"Total de pagination introuvable : {endpoint.path}")
    size = next((names[name] for name in PAGE_SIZE_NAMES if name in names), None)
    page = next((names[name] for name in PAGE_NAMES if name in names), None)
    if "offset" in names and size:
        return {
            "type": "offset", "offset_param": names["offset"], "limit_param": size,
            "page_size": page_size, "total_path": endpoint.total_path,
        }
    if page and size:
        return {
            "type": "page", "page_param": page, "size_param": size,
            "page_size": page_size, "total_path": endpoint.total_path,
        }
    raise OpenApiError(f"Pagination non reconnue : {endpoint.path}")
