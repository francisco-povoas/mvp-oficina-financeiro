"""Publisher financeiro contra DynamoDB Local e ElasticMQ reais."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from app.application.outbox_publisher import (
    OutboxPublisher,
    PublisherPolicy,
    StaticFinanceiroMessageRouter,
)
from app.config import Settings
from app.domain.models import OutboxRecord, OutboxStatus
from app.infrastructure.dynamodb import DynamoDBFinanceRepository, ensure_table
from app.infrastructure.messaging.contracts import JsonSchemaMessageValidator
from app.infrastructure.messaging.outbox_store import DynamoDBPublisherOutboxStore
from app.infrastructure.messaging.sqs import SqsFifoTransport, create_sqs_client

pytestmark = pytest.mark.integration

QUEUE_URL = os.environ["SQS_FINANCEIRO_TO_ORDENS_URL"]
ENDPOINT_URL = os.environ["SQS_ENDPOINT_URL"]
QUEUE_NAME = "mvp-oficina-financeiro-to-ordens-local.fifo"
SCHEMA = "contracts/messages/financial-events.v1.schema.json"


def _settings() -> Settings:
    return Settings(
        environment="test",
        dynamodb_endpoint_url=os.environ["DYNAMODB_ENDPOINT_URL"],
        dynamodb_table=f"mvp-oficina-financeiro-publisher-{uuid4().hex}",
        sqs_endpoint_url=ENDPOINT_URL,
        sqs_financeiro_to_ordens_url=QUEUE_URL,
    )


@pytest.fixture
def repository_settings():
    settings = _settings()
    table = ensure_table(settings)
    yield DynamoDBFinanceRepository(table), settings
    table.delete()
    table.meta.client.get_waiter("table_not_exists").wait(TableName=table.name)


@pytest.fixture
def sqs_client():
    client = boto3.client(
        "sqs",
        endpoint_url=ENDPOINT_URL,
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 1}),
    )
    try:
        client.get_queue_url(QueueName=QUEUE_NAME)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "AWS.SimpleQueueService.NonExistentQueue",
            "QueueDoesNotExist",
        }:
            raise
        client.create_queue(
            QueueName=QUEUE_NAME,
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
        )
    while True:
        messages = client.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=10).get(
            "Messages", []
        )
        if not messages:
            break
        for message in messages:
            client.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=message["ReceiptHandle"])
    yield client
    client.close()


def _record(*, event_type="QuoteApproved.v1", valid=True) -> OutboxRecord:
    now = datetime.now(UTC)
    return OutboxRecord(
        message_id=uuid4(),
        event_type=event_type,
        event_version=1,
        aggregate_id=uuid4(),
        payload={
            "order_id": str(uuid4()),
            "quote_id": str(uuid4()),
            "quote_version": 2 if valid else 0,
            "approved_at": now.isoformat(),
        },
        correlation_id=uuid4(),
        causation_id=uuid4(),
        operation_id=uuid4(),
        occurred_at=now,
        next_attempt_at=now,
    )


def _publisher(settings, client, worker_id="financeiro-integration", store=None):
    return OutboxPublisher(
        worker_id=worker_id,
        store=store or DynamoDBPublisherOutboxStore(settings),
        router=StaticFinanceiroMessageRouter(QUEUE_URL),
        validator=JsonSchemaMessageValidator(SCHEMA),
        transport=SqsFifoTransport(client, send_timeout_seconds=2),
        policy=PublisherPolicy(
            batch_size=10,
            lease_seconds=10,
            poll_interval_seconds=0.1,
            max_attempts=8,
            initial_backoff_seconds=1,
            max_backoff_seconds=60,
        ),
        jitter=lambda _start, cap: cap,
    )


async def test_publica_envelope_e_atributos_fifo(
    repository_settings, sqs_client
) -> None:
    repository, settings = repository_settings
    record = _record()
    repository.save_outbox_record(record)
    assert await _publisher(settings, sqs_client).run_once() == 1
    [received] = sqs_client.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        AttributeNames=["All"],
    )["Messages"]
    envelope = json.loads(received["Body"])
    assert envelope["message_id"] == str(record.message_id)
    assert envelope["correlation_id"] == str(record.correlation_id)
    assert envelope["causation_id"] == str(record.causation_id)
    assert envelope["operation_id"] == str(record.operation_id)
    assert envelope["saga_id"] is None
    assert received["Attributes"]["MessageDeduplicationId"] == str(record.message_id)
    assert received["Attributes"]["MessageGroupId"] == record.payload["order_id"]
    stored = repository.get_outbox_record(record.message_id)
    assert stored is not None and stored.status == OutboxStatus.PUBLISHED


async def test_falha_real_de_conexao_agenda_retry(repository_settings) -> None:
    repository, settings = repository_settings
    record = _record()
    repository.save_outbox_record(record)
    client = create_sqs_client(
        region="us-east-1",
        endpoint_url="http://127.0.0.1:1",
        connect_timeout_seconds=0.1,
        read_timeout_seconds=0.1,
    )
    assert await _publisher(settings, client).run_once() == 1
    client.close()
    stored = repository.get_outbox_record(record.message_id)
    assert stored is not None and stored.status == OutboxStatus.PENDING
    assert stored.claim_id is None and stored.attempt_count == 1
    assert stored.last_error and stored.next_attempt_at > stored.last_failed_at


async def test_invalida_e_sem_rota_ficam_auditaveis(
    repository_settings, sqs_client
) -> None:
    repository, settings = repository_settings
    invalid = _record(valid=False)
    unrouted = _record(event_type="PaymentRequested.v1")
    repository.save_outbox_record(invalid)
    repository.save_outbox_record(unrouted)
    assert await _publisher(settings, sqs_client).run_once() == 2
    assert not sqs_client.receive_message(QueueUrl=QUEUE_URL).get("Messages")
    first = repository.get_outbox_record(invalid.message_id)
    second = repository.get_outbox_record(unrouted.message_id)
    assert first is not None and first.last_error.startswith("MessageContractError:")
    assert second is not None and second.last_error.startswith("UnknownMessageRouteError:")


class ConfirmationFailureStore(DynamoDBPublisherOutboxStore):
    async def mark_published(self, **_kwargs):
        raise RuntimeError("falha depois do envio")


async def test_recupera_lease_apos_falha_de_confirmacao(
    repository_settings, sqs_client
) -> None:
    repository, settings = repository_settings
    record = _record()
    repository.save_outbox_record(record)
    first_store = ConfirmationFailureStore(settings)
    assert await _publisher(settings, sqs_client, store=first_store).run_once() == 1
    stored = repository.get_outbox_record(record.message_id)
    assert stored is not None and stored.status == OutboxStatus.PENDING and stored.claim_id
    forced_now = datetime.now(UTC)
    repository.table.update_item(
        Key={"PK": f"OUTBOX#{record.message_id}", "SK": "META"},
        UpdateExpression="SET claimed_at=:claimed, claim_expires_at=:expires",
        ExpressionAttributeValues={
            ":claimed": (forced_now - timedelta(seconds=2)).isoformat(),
            ":expires": (forced_now - timedelta(seconds=1)).isoformat(),
        },
    )
    assert await _publisher(settings, sqs_client, "financeiro-restarted").run_once() == 1
    recovered = repository.get_outbox_record(record.message_id)
    assert recovered is not None and recovered.status == OutboxStatus.PUBLISHED
    assert recovered.attempt_count == 2


async def test_publishers_concorrentes_nao_duplicam_claim(
    repository_settings, sqs_client
) -> None:
    repository, settings = repository_settings
    records = [_record() for _ in range(4)]
    for record in records:
        repository.save_outbox_record(record)
    counts = await asyncio.gather(
        _publisher(settings, sqs_client, "financeiro-a").run_once(),
        _publisher(settings, sqs_client, "financeiro-b").run_once(),
    )
    assert sum(counts) == 4
    stored = [repository.get_outbox_record(record.message_id) for record in records]
    assert all(item is not None and item.status == OutboxStatus.PUBLISHED for item in stored)
