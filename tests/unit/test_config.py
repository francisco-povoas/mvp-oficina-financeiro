import pytest

from app.config import Settings


def test_runtime_requires_table_name() -> None:
    settings = Settings(environment="test", dynamodb_table="")

    with pytest.raises(ValueError, match="DYNAMODB_TABLE"):
        settings.validate_runtime()


def test_production_rejects_local_endpoint() -> None:
    settings = Settings(
        environment="prod",
        dynamodb_table="mvp-oficina-financeiro-prod",
        dynamodb_endpoint_url="http://localhost:8000",
        jwt_secret_key="production-secret",
    )

    with pytest.raises(ValueError, match="DYNAMODB_ENDPOINT_URL"):
        settings.validate_runtime()
