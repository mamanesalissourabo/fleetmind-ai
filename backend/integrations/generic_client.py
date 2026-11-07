"""Client unique pour tous les TMS : son comportement vient entièrement de la configuration."""

import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import quote

import httpx

from backend.core.config import get_secret
from backend.integrations.errors import TmsAuthError, TmsResponseError, TmsUnavailableError
from backend.integrations.field_path import get_path
from backend.integrations.normalizer import NormalizationResult, normalize_records
from backend.schemas.canonical import CanonicalRecord, Incident, Mission, MissionEvent
from backend.schemas.tms_config import ApiKeyAuth, ResourceConfig, TmsConfig, TokenAuth

T = TypeVar("T", bound=CanonicalRecord)
TOKEN_EXPIRY_MARGIN_SECONDS = 30


def create_http_client(config: TmsConfig) -> httpx.Client:
    """Crée le client HTTP correspondant à la configuration d'un TMS."""
    return httpx.Client(base_url=config.base_url, timeout=config.timeout_seconds)


class GenericTmsClient:
    """Récupère et normalise les données d'un TMS décrit par sa configuration."""

    def __init__(
        self,
        config: TmsConfig,
        http_client: httpx.Client,
        secret_provider: Callable[[str], str] = get_secret,
    ) -> None:
        self._config = config
        self._http = http_client
        self._secret = secret_provider
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def tms_id(self) -> str:
        return self._config.id

    def get_missions(self) -> NormalizationResult[Mission]:
        return self._fetch(self._config.resources.missions, Mission)

    def get_incidents(self) -> NormalizationResult[Incident]:
        return self._fetch(self._config.resources.incidents, Incident)

    def get_mission_events(self, mission_id: str) -> NormalizationResult[MissionEvent]:
        return self._fetch(self._config.resources.events, MissionEvent, mission_id=mission_id)

    def _fetch(
        self, resource: ResourceConfig, model: type[T], **path_params: str
    ) -> NormalizationResult[T]:
        escaped = {name: quote(value, safe="") for name, value in path_params.items()}
        path = resource.path.format(**escaped)
        records = self._fetch_paginated(path, resource) if resource.paginated else (
            self._extract_items(self._get_json(path), resource)
        )
        return normalize_records(records, resource.mapping, model, self._config.id)

    def _fetch_paginated(self, path: str, resource: ResourceConfig) -> list[Mapping[str, Any]]:
        pagination = self._config.pagination
        records: list[Mapping[str, Any]] = []
        while True:
            body = self._get_json(path, pagination.query_params(len(records)))
            items = self._extract_items(body, resource)
            total = get_path(body, pagination.total_path)
            if not isinstance(total, int):
                raise TmsResponseError(f"{self._config.id} : total de pagination illisible")
            records.extend(items)
            if len(records) >= total:
                return records
            if not items:
                raise TmsResponseError(
                    f"{self._config.id} : pagination incomplète ({len(records)}/{total})"
                )

    def _extract_items(self, body: Any, resource: ResourceConfig) -> list[Mapping[str, Any]]:
        items = get_path(body, resource.items_path) if resource.items_path else body
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            raise TmsResponseError(f"{self._config.id} : liste attendue dans la réponse")
        return items

    def _get_json(self, path: str, params: Mapping[str, int] | None = None) -> Any:
        response = self._send("GET", path, params=params, headers=self._auth_headers())
        if response.status_code == 401 and isinstance(self._config.auth, TokenAuth):
            self._token = None
            response = self._send("GET", path, params=params, headers=self._auth_headers())
        return self._parse(response)

    def _auth_headers(self) -> dict[str, str]:
        auth = self._config.auth
        if isinstance(auth, ApiKeyAuth):
            return {auth.header: self._secret(auth.key_env)}
        return {"Authorization": f"Bearer {self._valid_token(auth)}"}

    def _valid_token(self, auth: TokenAuth) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            credentials = {name: self._secret(env) for name, env in auth.credentials_env.items()}
            body = self._parse(self._send("POST", auth.token_path, json=credentials))
            token = get_path(body, auth.token_field)
            expires_in = get_path(body, auth.expires_in_field)
            if not isinstance(token, str) or not isinstance(expires_in, int):
                raise TmsResponseError(f"{self._config.id} : réponse de jeton illisible")
            self._token = token
            self._token_expires_at = time.monotonic() + expires_in - TOKEN_EXPIRY_MARGIN_SECONDS
        return self._token

    def _send(self, method: str, path: str, **options: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **options)
        except httpx.TransportError as error:
            raise TmsUnavailableError(
                f"{self._config.id} : injoignable ({type(error).__name__})"
            ) from error

    def _parse(self, response: httpx.Response) -> Any:
        status = response.status_code
        if status in (401, 403):
            raise TmsAuthError(f"{self._config.id} : accès refusé (HTTP {status})")
        if status >= 500:
            raise TmsUnavailableError(f"{self._config.id} : erreur serveur (HTTP {status})")
        if status >= 400:
            raise TmsResponseError(f"{self._config.id} : requête refusée (HTTP {status})")
        try:
            return response.json()
        except ValueError as error:
            raise TmsResponseError(f"{self._config.id} : réponse non JSON") from error
