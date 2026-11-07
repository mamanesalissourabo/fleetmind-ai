"""Comptes, rôles et droits par TMS."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ALL_TMS = "*"


class Role(StrEnum):
    EXECUTIVE = "executive"
    OPERATOR = "operator"
    ADMIN = "admin"


class CurrentUser(BaseModel):
    """Utilisateur connecté : ses droits viennent de la base, jamais de la requête."""

    model_config = ConfigDict(frozen=True)

    username: str
    role: Role
    allowed_tms: tuple[str, ...]

    def can_access(self, tms_id: str) -> bool:
        return ALL_TMS in self.allowed_tms or tms_id in self.allowed_tms

    @property
    def can_act_on_alerts(self) -> bool:
        return self.role in (Role.OPERATOR, Role.ADMIN)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: CurrentUser
