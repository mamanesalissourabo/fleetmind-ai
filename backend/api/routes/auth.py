"""Connexion et déconnexion."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from backend.api.dependencies import CurrentUserDep, SessionDep, bearer_scheme
from backend.schemas.auth import CurrentUser, LoginRequest, LoginResponse
from backend.services.auth import AuthError, login, logout

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def log_in(request: LoginRequest, session: SessionDep) -> LoginResponse:
    """Retourne un jeton à transmettre dans l'en-tête Authorization, valable 8 heures."""
    try:
        token, user = login(session, request.username, request.password, datetime.now(UTC))
    except AuthError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
    return LoginResponse(access_token=token, user=user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def log_out(
    _: CurrentUserDep,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> None:
    logout(session, credentials.credentials)


@router.get("/me", response_model=CurrentUser)
def me(user: CurrentUserDep) -> CurrentUser:
    return user
