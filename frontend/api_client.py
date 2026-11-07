"""Accès à l'API du backend : l'interface ne contient aucune logique métier."""

import os
from functools import lru_cache
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 300


class ApiError(Exception):
    """Message prêt à être affiché, sans détail technique inutile."""


class SessionExpiredError(ApiError):
    """Le jeton n'est plus valable : il faut se reconnecter."""


class FleetMindApi:
    def __init__(self, http: httpx.Client, token: str | None = None) -> None:
        self._http = http
        self._token = token

    def with_token(self, token: str) -> "FleetMindApi":
        return FleetMindApi(self._http, token)

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request("POST", "/auth/login", json={"username": username, "password": password})

    def logout(self) -> None:
        self._request("POST", "/auth/logout")

    def monitoring_status(self) -> dict[str, Any]:
        return self._request("GET", "/monitoring/status")

    def run_cycle(self) -> list[dict[str, Any]]:
        return self._request("POST", "/monitoring/cycles", json={})

    def alerts(self, status: str | None = None, tms_id: str | None = None) -> list[dict[str, Any]]:
        params = {"alert_status": status, "tms_id": tms_id}
        clean = {key: value for key, value in params.items() if value}
        return self._request("GET", "/alerts", params=clean)

    def alert(self, alert_id: int) -> dict[str, Any]:
        return self._request("GET", f"/alerts/{alert_id}")

    def acknowledge(self, alert_id: int, note: str | None) -> dict[str, Any]:
        return self._request("POST", f"/alerts/{alert_id}/acknowledge", json={"note": note or None})

    def resolve(self, alert_id: int, note: str | None) -> dict[str, Any]:
        return self._request("POST", f"/alerts/{alert_id}/resolve", json={"note": note or None})

    def missions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/missions")

    def ask(self, question: str) -> dict[str, Any]:
        return self._request("POST", "/chatbot/ask", json={"question": question})

    def reindex_documents(self) -> dict[str, Any]:
        return self._request("POST", "/documents/reindex")

    def _request(self, method: str, path: str, **options: Any) -> Any:
        try:
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            response = self._http.request(method, path, headers=headers, **options)
        except httpx.TimeoutException as error:
            raise ApiError("Le backend met trop de temps à répondre.") from error
        except httpx.TransportError as error:
            raise ApiError("Le backend est injoignable. Vérifiez qu'il est lancé.") from error
        if response.status_code == 401 and self._token:
            raise SessionExpiredError("Votre session a expiré : reconnectez-vous.")
        if response.status_code >= 400:
            raise ApiError(_error_message(response))
        return response.json() if response.content else None


def _error_message(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, str):
        return detail
    return f"Le backend a refusé la demande (code {response.status_code})."


@lru_cache
def get_api() -> FleetMindApi:
    base_url = os.environ.get("FLEETMIND_API_URL", DEFAULT_URL)
    return FleetMindApi(httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS))
