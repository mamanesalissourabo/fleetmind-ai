"""Outils de test partagés : client générique branché sur les TMS simulés."""

from fastapi.testclient import TestClient

from backend.integrations.generic_client import GenericTmsClient
from backend.integrations.tms_registry import TmsRegistry
from backend.schemas.auth import CurrentUser, Role
from backend.schemas.tms_config import TmsConfig
from mock_tms.common import get_mock_settings
from mock_tms.tms_alpha.app import app as alpha_app
from mock_tms.tms_beta.app import app as beta_app

APPS = {"tms_alpha": alpha_app, "tms_beta": beta_app}
REGISTRY = TmsRegistry.from_directory()


def mock_secret(name: str) -> str:
    return getattr(get_mock_settings(), name.lower())


def client_for(tms_id: str, config: TmsConfig | None = None) -> GenericTmsClient:
    return GenericTmsClient(
        config or REGISTRY.get(tms_id), TestClient(APPS[tms_id]), secret_provider=mock_secret
    )


TEST_ADMIN = CurrentUser(username="test-admin", role=Role.ADMIN, allowed_tms=("*",))
