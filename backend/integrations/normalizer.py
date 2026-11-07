"""Conversion des réponses brutes d'un TMS vers le modèle canonique."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from backend.integrations.field_path import get_path
from backend.schemas.canonical import CanonicalRecord, GeoPoint
from backend.schemas.tms_config import (
    DatetimeMapping,
    EnumMapping,
    GeoMapping,
    TextMapping,
)

T = TypeVar("T", bound=CanonicalRecord)
AnyMapping = TextMapping | EnumMapping | DatetimeMapping | GeoMapping


@dataclass(frozen=True)
class RejectedRecord:
    """Enregistrement écarté, sans recopier son contenu potentiellement confidentiel."""

    position: int
    reason: str


@dataclass(frozen=True)
class NormalizationResult(Generic[T]):
    items: list[T] = field(default_factory=list)
    rejected: list[RejectedRecord] = field(default_factory=list)


def _to_utc(raw: Any, mapping: DatetimeMapping) -> datetime:
    text = str(raw)
    parsed = (
        datetime.fromisoformat(text)
        if mapping.format == "iso8601"
        else datetime.strptime(text, mapping.format)
    )
    if parsed.tzinfo is None:
        if mapping.timezone is None:
            raise ValueError("date sans fuseau horaire et aucun fuseau configuré")
        parsed = parsed.replace(tzinfo=ZoneInfo(mapping.timezone))
    return parsed.astimezone(UTC)


def _to_geo_point(raw: Any, mapping: GeoMapping) -> GeoPoint:
    if mapping.format == "object":
        return GeoPoint(lat=get_path(raw, mapping.lat_key), lon=get_path(raw, mapping.lon_key))
    lat, lon = str(raw).split(",")
    return GeoPoint(lat=float(lat), lon=float(lon))


def _convert(raw: Any, mapping: AnyMapping) -> Any:
    match mapping:
        case EnumMapping():
            return mapping.values.get(str(raw), mapping.default)
        case DatetimeMapping():
            return _to_utc(raw, mapping)
        case GeoMapping():
            if isinstance(raw, list):
                return [_to_geo_point(point, mapping) for point in raw]
            return _to_geo_point(raw, mapping)
        case TextMapping():
            return raw


def _describe(error: ValidationError) -> str:
    """Décrit une erreur de validation sans inclure les valeurs reçues."""
    return "; ".join(
        f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
        for detail in error.errors(include_input=False, include_url=False)
    )


def normalize_records(
    records: list[Mapping[str, Any]],
    mapping: Mapping[str, AnyMapping],
    model: type[T],
    source_tms: str,
) -> NormalizationResult[T]:
    """Normalise des enregistrements ; ceux qui sont illisibles sont écartés et signalés."""
    result: NormalizationResult[T] = NormalizationResult()
    for position, record in enumerate(records):
        try:
            values: dict[str, Any] = {"source_tms": source_tms}
            for name, field_mapping in mapping.items():
                raw = get_path(record, field_mapping.path)
                if raw is not None:
                    values[name] = _convert(raw, field_mapping)
            result.items.append(model.model_validate(values))
        except ValidationError as error:
            result.rejected.append(RejectedRecord(position, _describe(error)))
        except ValueError as error:
            result.rejected.append(RejectedRecord(position, f"valeur illisible ({type(error).__name__})"))
    return result
