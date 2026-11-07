"""Calcul de distance entre une position et un itinéraire."""

from math import cos, hypot, radians

from backend.schemas.canonical import GeoPoint

EARTH_RADIUS_KM = 6371.0


def distance_to_route_km(position: GeoPoint, route: list[GeoPoint]) -> float:
    """Retourne la distance approchée entre une position et un itinéraire d'au moins 2 points.

    L'itinéraire est assimilé à des segments droits entre ses points, et la projection
    plane n'est valable qu'à l'échelle régionale.
    """
    if len(route) < 2:
        raise ValueError("Un itinéraire doit contenir au moins deux points")

    def project(point: GeoPoint) -> tuple[float, float]:
        x = radians(point.lon - position.lon) * cos(radians(position.lat)) * EARTH_RADIUS_KM
        y = radians(point.lat - position.lat) * EARTH_RADIUS_KM
        return x, y

    def distance_to_segment(start: GeoPoint, end: GeoPoint) -> float:
        (ax, ay), (bx, by) = project(start), project(end)
        dx, dy = bx - ax, by - ay
        length_squared = dx * dx + dy * dy
        ratio = 0.0 if length_squared == 0 else (-(ax * dx + ay * dy)) / length_squared
        ratio = min(1.0, max(0.0, ratio))
        return hypot(ax + ratio * dx, ay + ratio * dy)

    return min(distance_to_segment(a, b) for a, b in zip(route, route[1:]))
