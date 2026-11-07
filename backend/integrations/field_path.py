"""Lecture d'une valeur dans une réponse JSON à partir d'un chemin pointé."""

from collections.abc import Mapping
from typing import Any


def get_path(data: Any, path: str) -> Any:
    """Retourne la valeur située à `path` (ex. `driver.name`), ou None si elle est absente."""
    current = data
    for key in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current
