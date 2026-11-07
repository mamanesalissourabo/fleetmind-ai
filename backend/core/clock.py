"""Horloge de surveillance : réelle, ou simulée pour rejouer les données de démonstration."""

from collections.abc import Callable
from datetime import UTC, datetime


def _real_now() -> datetime:
    return datetime.now(UTC)


class Clock:
    """Donne l'heure de référence des cycles."""

    def __init__(
        self,
        simulated_start: datetime | None = None,
        speed: float = 1.0,
        real_now: Callable[[], datetime] = _real_now,
    ) -> None:
        self._simulated_start = simulated_start
        self._speed = speed
        self._real_now = real_now
        self._origin = real_now()

    def now(self) -> datetime:
        if self._simulated_start is None:
            return self._real_now()
        return self._simulated_start + (self._real_now() - self._origin) * self._speed
