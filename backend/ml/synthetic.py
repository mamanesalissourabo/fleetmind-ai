"""Missions d'entraînement synthétiques : elles démontrent la méthode, pas une performance réelle."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import Random

from backend.ml.features import extract_features, route_length_km
from backend.schemas.canonical import GeoPoint, Mission, MissionStatus

CITIES = {
    "Casablanca": (33.57, -7.59), "Rabat": (34.02, -6.84), "Tanger": (35.76, -5.83),
    "Fès": (34.03, -5.0), "Meknès": (33.89, -5.55), "Marrakech": (31.63, -8.01),
    "Agadir": (30.43, -9.6), "Oujda": (34.68, -1.91), "Nador": (35.17, -2.93),
    "Safi": (32.3, -9.24), "El Jadida": (33.23, -8.5), "Béni Mellal": (32.34, -6.36),
    "Tétouan": (35.57, -5.37), "Laâyoune": (27.15, -13.2), "Ouarzazate": (30.92, -6.9),
}
START = datetime(2026, 1, 1, tzinfo=UTC)
PLANNED_SPEED_KMH = 60
LATE_THRESHOLD_MINUTES = 30


@dataclass(frozen=True)
class Dataset:
    features: list[list[float]]
    labels: list[int]
    groups: list[int]


def _simulate(rng: Random, index: int) -> tuple[list[list[float]], int]:
    """Simule une mission et deux observations prises avant son arrivée prévue."""
    origin, destination = rng.sample(sorted(CITIES), 2)
    route = [GeoPoint(lat=c[0], lon=c[1]) for c in (CITIES[origin], CITIES[destination])]
    planned_minutes = max(route_length_km(route) / PLANNED_SPEED_KMH * 60, 45.0)
    day = START + timedelta(days=rng.randrange(300))
    planned_departure = day + timedelta(hours=rng.randint(4, 20), minutes=rng.randrange(60))
    planned_arrival = planned_departure + timedelta(minutes=planned_minutes)

    departure_delay = rng.expovariate(1 / 12)
    rush = planned_departure.hour in (7, 8, 9, 17, 18, 19) and planned_departure.weekday() < 5
    traffic = 1 + rng.gauss(0, 0.03) + (0.06 if rush else 0)
    drive_minutes = planned_minutes * max(traffic, 0.85)
    has_incident = rng.random() < 0.1
    incident_delay = rng.uniform(30, 180) if has_incident else 0.0
    incident_at = rng.random()
    total = departure_delay + drive_minutes + incident_delay
    is_late = int(total - planned_minutes > LATE_THRESHOLD_MINUTES)
    actual_departure = planned_departure + timedelta(minutes=departure_delay)
    driver = None if rng.random() < 0.05 else "Chauffeur"
    position_known = rng.random() > 0.06

    rows = []
    for _ in range(2):
        reference = planned_departure + timedelta(minutes=planned_minutes * rng.uniform(0.05, 0.95))
        driven = max((reference - actual_departure).total_seconds() / 60, 0.0)
        reached = incident_at * drive_minutes
        stopped = has_incident and driven > reached
        effective = reached + max(driven - reached - incident_delay, 0) if stopped else driven
        share = min(effective / drive_minutes, 1.0)
        age = rng.expovariate(1 / 8)
        lat = route[0].lat + (route[1].lat - route[0].lat) * share + rng.gauss(0, 0.01)
        lon = route[0].lon + (route[1].lon - route[0].lon) * share + rng.gauss(0, 0.01)
        departed = driven > 0
        mission = Mission(
            source_tms="synthetic", external_id=f"S-{index}", status=MissionStatus.IN_TRANSIT,
            customer_name="Client", vehicle_plate="00000-A-1", driver_name=driver,
            origin_city=origin, destination_city=destination,
            planned_departure=planned_departure, planned_arrival=planned_arrival,
            actual_departure=actual_departure if departed else None,
            planned_route=route,
            last_position=GeoPoint(lat=lat, lon=lon) if position_known and departed else None,
            last_position_at=reference - timedelta(minutes=age) if position_known and departed else None,
            updated_at=reference,
        )
        reported = stopped and rng.random() < 0.7
        rows.append(extract_features(mission, reference, int(reported)))
    return rows, is_late


def generate_dataset(missions: int, seed: int = 42) -> Dataset:
    """Toujours identique pour une même graine, afin de rester reproductible."""
    rng = Random(seed)
    features, labels, groups = [], [], []
    for index in range(missions):
        rows, label = _simulate(rng, index)
        for row in rows:
            features.append(row)
            labels.append(label)
            groups.append(index)
    return Dataset(features, labels, groups)
