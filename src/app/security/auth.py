"""Dependências de autenticação pública e interna do Financeiro."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.security.jwt import InsufficientScope, InvalidToken, JWTProvider
from app.security.roles import Role

bearer = HTTPBearer(auto_error=False)
Credential = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]

_credentials_exception = HTTPException(
    status_code=401,
    detail="Token inválido, expirado ou incompatível",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True)
class UserPrincipal:
    user_id: str
    roles: frozenset[str]


@dataclass(frozen=True)
class ServicePrincipal:
    client_id: str
    scopes: frozenset[str]


def get_jwt_provider(request: Request) -> JWTProvider:
    settings = request.app.state.settings
    return JWTProvider(
        settings.jwt_secret_key,
        settings.jwt_access_token_expire_minutes,
        settings.jwt_algorithm,
        settings.jwt_issuer,
    )


Provider = Annotated[JWTProvider, Depends(get_jwt_provider)]


async def require_user(
    request: Request,
    credential: Credential,
    provider: Provider,
) -> UserPrincipal:
    if credential is None:
        raise _credentials_exception
    try:
        user_id, roles = provider.get_sub(
            credential.credentials,
            audience=request.app.state.settings.jwt_user_audience,
        )
    except InvalidToken as exc:
        raise _credentials_exception from exc
    return UserPrincipal(user_id=user_id, roles=frozenset(roles))


class RequireRoles:
    """Autoriza somente perfis locais conhecidos e explicitamente permitidos."""

    def __init__(self, *roles: Role) -> None:
        if not roles:
            raise ValueError("Política pública exige ao menos um perfil")
        self.roles = frozenset(role.value for role in roles)

    async def __call__(
        self,
        principal: Annotated[UserPrincipal, Depends(require_user)],
    ) -> UserPrincipal:
        known_roles = {role.value for role in Role}
        if not principal.roles or not principal.roles.issubset(known_roles):
            raise HTTPException(status_code=403, detail="Perfil de usuário não reconhecido")
        if self.roles.isdisjoint(principal.roles):
            raise HTTPException(status_code=403, detail="Permissão insuficiente")
        return principal


class RequireServiceScopes:
    def __init__(self, *scopes: str) -> None:
        if not scopes:
            raise ValueError("Endpoint interno exige ao menos um escopo")
        self.scopes = frozenset(scopes)

    async def __call__(
        self,
        request: Request,
        credential: Credential,
        provider: Provider,
    ) -> ServicePrincipal:
        if credential is None:
            raise _credentials_exception
        try:
            client_id, granted = provider.get_service(
                credential.credentials,
                audience=request.app.state.settings.service_name,
                required_scopes=set(self.scopes),
            )
        except InsufficientScope as exc:
            raise HTTPException(
                status_code=403,
                detail="Escopo de serviço insuficiente",
            ) from exc
        except InvalidToken as exc:
            raise _credentials_exception from exc
        return ServicePrincipal(client_id=client_id, scopes=frozenset(granted))

