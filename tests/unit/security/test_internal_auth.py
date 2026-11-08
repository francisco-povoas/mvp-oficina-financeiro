from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security.jwt import JWTProvider

SECRET = "segredo-hs256-exclusivo-dos-testes"
CORRELATION_ID = str(uuid4())


def _settings() -> Settings:
    return Settings(environment="test", jwt_secret_key=SECRET)


def _provider(settings: Settings) -> JWTProvider:
    return JWTProvider(
        settings.jwt_secret_key,
        settings.jwt_access_token_expire_minutes,
        settings.jwt_algorithm,
        settings.jwt_issuer,
    )


def test_internal_finance_endpoint_requires_exact_service_token() -> None:
    settings = _settings()
    provider = _provider(settings)
    correct = provider.create_service_token(
        client_id="mvp-oficina-ordens",
        audience=settings.service_name,
        scopes={"finance:status:read"},
        expire_minutes=5,
    )["access_token"]
    wrong_scope = provider.create_service_token(
        client_id="mvp-oficina-ordens",
        audience=settings.service_name,
        scopes={"resources:operation:read"},
        expire_minutes=5,
    )["access_token"]
    user = provider.create_user_token(
        subject=str(uuid4()),
        roles=["Administrador"],
        audience=settings.jwt_user_audience,
    )["access_token"]
    path = f"/internal/v1/orders/{uuid4()}/financial-status"

    with TestClient(create_app(settings)) as client:
        headers = {"X-Correlation-ID": CORRELATION_ID}
        assert client.get(path, headers=headers).status_code == 401
        assert client.get(
            path,
            headers={**headers, "Authorization": f"Bearer {user}"},
        ).status_code == 401
        wrong_scope_response = client.get(
            path,
            headers={**headers, "Authorization": f"Bearer {wrong_scope}"},
        )
        assert wrong_scope_response.status_code == 403
        assert client.get(
            path,
            headers={**headers, "Authorization": f"Bearer {correct}"},
        ).status_code == 501


def test_public_finance_endpoint_rejects_service_token_before_persistence() -> None:
    settings = _settings()
    service = _provider(settings).create_service_token(
        client_id="mvp-oficina-ordens",
        audience=settings.jwt_user_audience,
        scopes={"finance:status:read"},
        expire_minutes=5,
    )["access_token"]

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/quotes",
            headers={"Authorization": f"Bearer {service}"},
            json={},
        )

    assert response.status_code == 401


def test_provider_refuses_algorithm_other_than_hs256() -> None:
    with pytest.raises(ValueError, match="HS256"):
        JWTProvider(SECRET, 5, "RS256")

