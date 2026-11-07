"""Variables du modèle : le même calcul sert à l'entraînement et aux prédictions."""

from datetime import datetime
from math import asin, cos, nan, radians, sin, sqrt

from backend.schemas.canonical import GeoPoint, Mission

EARTH_RADIUS_KM = 6371.0
FEATURE_NAMES = [
    "planned_duration_min",
    "route_km",
    "departure_delay_min",
    "elapsed_ratio",
    "progress_ratio",
    "progress_gap",
    "position_age_min",
    "departure_hour",
    "weekday",
    "open_incidents",
    "driver_known",
]


def distance_km(a: GeoPoint, b: GeoPoint) -> float:
    lat_a, lat_b = radians(a.lat), radians(b.lat)
    delta_lat, delta_lon = lat_b - lat_a, radians(b.lon - a.lon)
    h = sin(delta_lat / 2) ** 2 + cos(lat_a) * cos(lat_b) * sin(delta_lon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


def route_length_km(route: list[GeoPoint]) -> float:
    return sum(distance_km(a, b) for a, b in zip(route, route[1:]))


def _minutes(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 60


def extract_features(mission: Mission, reference: datetime, open_incidents: int) -> list[float]:
    """Une information absente reste absente (nan) : LightGBM sait la traiter."""
    duration = max(_minutes(mission.planned_arrival, mission.planned_departure), 1.0)
    route_km = route_length_km(mission.planned_route)
    if mission.actual_departure:
        departure_delay = _minutes(mission.actual_departure, mission.planned_departure)
    else:
        departure_delay = max(_minutes(reference, mission.planned_departure), 0.0)
    elapsed = _minutes(reference, mission.planned_departure) / duration

    progress = gap = position_age = nan
    if mission.last_position and mission.planned_route and route_km > 0:
        remaining = distance_km(mission.last_position, mission.planned_route[-1])
        progress = min(max(1 - remaining / route_km, 0.0), 1.0)
        gap = elapsed - progress
    if mission.last_position_at:
        position_age = max(_minutes(reference, mission.last_position_at), 0.0)

    return [
        duration,
        route_km if route_km > 0 else nan,
        departure_delay,
        elapsed,
        progress,
        gap,
        position_age,
        float(mission.planned_departure.hour),
        float(mission.planned_departure.weekday()),
        float(open_incidents),
        float(mission.driver_name is not None),
    ]
