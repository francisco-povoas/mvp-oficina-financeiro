from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import v1
from app.config import Settings
from app.main import create_app
from app.security.auth import RequireRoles
from app.security.jwt import JWTProvider

SECRET = "segredo-hs256-exclusivo-dos-testes"

PUBLIC_ROUTES = (
    ("POST", "/api/v1/quotes", {}),
    ("GET", "/api/v1/quotes/not-a-uuid", None),
    ("POST", "/api/v1/quotes/not-a-uuid/communicate", {}),
    ("GET", "/api/v1/quotes/not-a-uuid/communications", None),
    ("POST", "/api/v1/quotes/not-a-uuid/approve", None),
    ("POST", "/api/v1/quotes/not-a-uuid/reject", {}),
    ("POST", "/api/v1/payments", {}),
    ("POST", "/api/v1/payments/not-a-uuid/simulate-confirm", {}),
)

READ_ROUTES = frozenset(
    {
        ("GET", "/api/v1/quotes/not-a-uuid"),
        ("GET", "/api/v1/quotes/not-a-uuid/communications"),
    }
)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", jwt_secret_key=SECRET)


@pytest.fixture
def provider(settings: Settings) -> JWTProvider:
    return JWTProvider(
        settings.jwt_secret_key,
        settings.jwt_access_token_expire_minutes,
        settings.jwt_algorithm,
        settings.jwt_issuer,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[v1._service] = object
    with TestClient(app) as test_client:
        yield test_client


def _user_token(
    provider: JWTProvider,
    settings: Settings,
    roles: list[str],
    *,
    audience: str | None = None,
    expire_minutes: int | None = None,
) -> str:
    return provider.create_access_token(
        {
            "sub": str(uuid4()),
            "perfis": roles,
            "aud": audience or settings.jwt_user_audience,
            "token_use": "user",
        },
        expire_minutes=expire_minutes,
    )["access_token"]


def _request(
    client: TestClient,
    method: str,
    path: str,
    body: dict | None,
    token: str | None,
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.request(method, path, headers=headers, json=body)


@pytest.mark.parametrize("role", ["Administrador", "Atendente"])
@pytest.mark.parametrize(("method", "path", "body"), PUBLIC_ROUTES)
def test_administrador_e_atendente_passam_pela_autorizacao_de_todas_as_rotas(
    client: TestClient,
    provider: JWTProvider,
    settings: Settings,
    role: str,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    response = _request(client, method, path, body, _user_token(provider, settings, [role]))

    assert response.status_code not in {401, 403}


@pytest.mark.parametrize(("method", "path", "body"), PUBLIC_ROUTES)
def test_mecanico_so_pode_consultar_orcamento_e_historico(
    client: TestClient,
    provider: JWTProvider,
    settings: Settings,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    response = _request(
        client,
        method,
        path,
        body,
        _user_token(provider, settings, ["Mecanico"]),
    )

    if (method, path) in READ_ROUTES:
        assert response.status_code not in {401, 403}
    else:
        assert response.status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), PUBLIC_ROUTES)
@pytest.mark.parametrize("roles", [[], ["Desconhecido"], ["Administrador", "Desconhecido"]])
def test_perfis_ausentes_ou_desconhecidos_sao_rejeitados_em_todas_as_rotas(
    client: TestClient,
    provider: JWTProvider,
    settings: Settings,
    method: str,
    path: str,
    body: dict | None,
    roles: list[str],
) -> None:
    response = _request(client, method, path, body, _user_token(provider, settings, roles))

    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), PUBLIC_ROUTES)
def test_ausencia_de_credencial_retorna_401_em_todas_as_rotas(
    client: TestClient,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    response = _request(client, method, path, body, None)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path", "body"), PUBLIC_ROUTES)
def test_token_invalido_retorna_401_em_todas_as_rotas(
    client: TestClient,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    response = _request(client, method, path, body, "jwt-invalido")

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), PUBLIC_ROUTES)
def test_token_de_servico_retorna_401_em_todas_as_rotas(
    client: TestClient,
    provider: JWTProvider,
    settings: Settings,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    token = provider.create_service_token(
        client_id="mvp-oficina-ordens",
        audience=settings.jwt_user_audience,
        scopes={"finance:status:read"},
        expire_minutes=5,
    )["access_token"]

    assert _request(client, method, path, body, token).status_code == 401


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda provider, settings: _user_token(
            provider, settings, ["Atendente"], expire_minutes=-1
        ),
        lambda provider, settings: _user_token(
            provider, settings, ["Atendente"], audience="outro-servico"
        ),
    ],
)
def test_token_expirado_ou_com_audiencia_incorreta_retorna_401(
    client: TestClient,
    provider: JWTProvider,
    settings: Settings,
    token_factory,
) -> None:
    token = token_factory(provider, settings)

    assert _request(client, "POST", "/api/v1/quotes", {}, token).status_code == 401


def test_politica_publica_exige_ao_menos_um_perfil() -> None:
    with pytest.raises(ValueError, match="ao menos um perfil"):
        RequireRoles()
