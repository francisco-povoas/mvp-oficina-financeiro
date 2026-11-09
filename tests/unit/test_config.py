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


def test_publisher_requires_queue_and_rejects_local_endpoint_in_production() -> None:
    missing = Settings(
        environment="test",
        dynamodb_table="finance-test",
        sqs_financeiro_to_ordens_url="",
    )
    with pytest.raises(ValueError, match="SQS_FINANCEIRO_TO_ORDENS_URL"):
        missing.validate_publisher_runtime()

    production = Settings(
        environment="prod",
        dynamodb_table="mvp-oficina-financeiro-prod",
        dynamodb_endpoint_url=None,
        jwt_secret_key="production-secret",
        sqs_endpoint_url="http://localhost:9324",
        sqs_financeiro_to_ordens_url="https://sqs.us-east-1.amazonaws.com/queue.fifo",
    )
    with pytest.raises(ValueError, match="SQS_ENDPOINT_URL"):
        production.validate_publisher_runtime()


def test_publisher_rejects_invalid_timing_and_attempt_settings() -> None:
    settings = Settings(
        environment="test",
        dynamodb_table="finance-test",
        sqs_financeiro_to_ordens_url="queue.fifo",
        publisher_lease_seconds=5,
        sqs_send_timeout_seconds=5,
    )
    with pytest.raises(ValueError, match="lease"):
        settings.validate_publisher_runtime()

    settings.publisher_lease_seconds = 30
    settings.publisher_max_attempts = 0
    with pytest.raises(ValueError, match="max attempts"):
        settings.validate_publisher_runtime()
