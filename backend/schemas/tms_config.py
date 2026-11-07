"""Format des fichiers de configuration décrivant chaque TMS."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PositiveFloat, PositiveInt
from pydantic import model_validator

from backend.schemas.canonical import CanonicalRecord, Incident, Mission, MissionEvent

INJECTED_FIELDS = {"source_tms"}


class StrictConfig(BaseModel):
    """Refuse les clés inconnues, pour détecter les erreurs de saisie."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiKeyAuth(StrictConfig):
    type: Literal["api_key"]
    header: str
    key_env: str


class TokenAuth(StrictConfig):
    type: Literal["token"]
    token_path: str
    credentials_env: dict[str, str]
    token_field: str
    expires_in_field: str


class PagePagination(StrictConfig):
    type: Literal["page"]
    page_param: str
    size_param: str
    page_size: PositiveInt
    total_path: str

    def query_params(self, fetched: int) -> dict[str, int]:
        return {self.page_param: fetched // self.page_size + 1, self.size_param: self.page_size}


class OffsetPagination(StrictConfig):
    type: Literal["offset"]
    offset_param: str
    limit_param: str
    page_size: PositiveInt
    total_path: str

    def query_params(self, fetched: int) -> dict[str, int]:
        return {self.offset_param: fetched, self.limit_param: self.page_size}


class TextMapping(StrictConfig):
    transform: Literal["text"] = "text"
    path: str


class EnumMapping(StrictConfig):
    transform: Literal["enum"]
    path: str
    values: dict[str, str]
    default: str = "UNKNOWN"


class DatetimeMapping(StrictConfig):
    transform: Literal["datetime"]
    path: str
    format: str = "iso8601"
    timezone: str | None = None


class GeoMapping(StrictConfig):
    transform: Literal["geo"]
    path: str
    format: Literal["object", "string"]
    lat_key: str = "lat"
    lon_key: str = "lon"


def _expand_text_shorthand(value: Any) -> Any:
    """Permet d'écrire `champ: chemin` pour une simple copie de texte."""
    return {"path": value, "transform": "text"} if isinstance(value, str) else value


AnyFieldMapping = Annotated[
    TextMapping | EnumMapping | DatetimeMapping | GeoMapping,
    Field(discriminator="transform"),
]
FieldMapping = Annotated[AnyFieldMapping, BeforeValidator(_expand_text_shorthand)]


class ResourceConfig(StrictConfig):
    path: str
    items_path: str | None = None
    paginated: bool = True
    mapping: dict[str, FieldMapping]

    def check_mapping_covers(self, model: type[CanonicalRecord]) -> None:
        """Vérifie que le mapping correspond bien au modèle canonique visé."""
        fields = {name: info for name, info in model.model_fields.items()
                  if name not in INJECTED_FIELDS}
        unknown = set(self.mapping) - set(fields)
        missing = {name for name, info in fields.items() if info.is_required()} - set(self.mapping)
        if unknown or missing:
            raise ValueError(
                f"Mapping invalide pour {model.__name__} : "
                f"champs inconnus {sorted(unknown)}, champs obligatoires absents {sorted(missing)}"
            )


class Resources(StrictConfig):
    missions: ResourceConfig
    events: ResourceConfig
    incidents: ResourceConfig


class TmsConfig(StrictConfig):
    id: str
    name: str
    config_version: PositiveInt
    base_url: str
    timeout_seconds: PositiveFloat = 10
    polling_interval_seconds: PositiveInt = 300
    auth: ApiKeyAuth | TokenAuth = Field(discriminator="type")
    pagination: PagePagination | OffsetPagination = Field(discriminator="type")
    resources: Resources

    @model_validator(mode="after")
    def check_mappings(self) -> "TmsConfig":
        self.resources.missions.check_mapping_covers(Mission)
        self.resources.events.check_mapping_covers(MissionEvent)
        self.resources.incidents.check_mapping_covers(Incident)
        return self
