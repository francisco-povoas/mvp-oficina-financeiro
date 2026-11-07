from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from boto3.dynamodb.conditions import Key

from app.config import Settings
from app.domain.exceptions import FinanceConflictError, IdempotencyConflictError
from app.domain.models import OutboxRecord, PaymentStatus, Quote, QuoteLine, QuoteStatus
from app.infrastructure.dynamodb import FinanceRepository, ensure_table

pytestmark = pytest.mark.integration


def make_settings(table_name: str | None = None) -> Settings:
    return Settings(
        environment="test",
        dynamodb_endpoint_url="http://dynamodb-test:8000",
        dynamodb_table=table_name or f"mvp-oficina-financeiro-test-{uuid4().hex}",
    )


def delete_table(table) -> None:
    table.delete()
    table.meta.client.get_waiter("table_not_exists").wait(TableName=table.name)


@pytest.fixture
def repository():
    table = ensure_table(make_settings())
    yield FinanceRepository(table)
    delete_table(table)


def make_quote(*, order_id=None) -> Quote:
    return Quote(
        quote_id=uuid4(),
        order_id=order_id or uuid4(),
        order_version=4,
        services=(
            QuoteLine(
                reference_id=uuid4(),
                description="Alinhamento",
                quantity=1,
                unit_price=Decimal("150.00"),
            ),
        ),
        items=(
            QuoteLine(
                reference_id=uuid4(),
                description="Filtro",
                quantity=2,
                unit_price=Decimal("25.00"),
            ),
        ),
        customer_contact={"telefone": "5511999999999"},
    )


def create_payment(repository, quote, key="payment-request-1"):
    return repository.create_payment(
        order_id=quote.order_id,
        quote_id=quote.quote_id,
        quote_version=quote.version,
        method="PIX",
        amount=quote.total,
        idempotency_key=key,
    )


def get_guard(repository, quote) -> dict:
    return repository.table.get_item(
        Key={"PK": f"ORDER#{quote.order_id}", "SK": "PAYMENT_GUARD"},
        ConsistentRead=True,
    )["Item"]


def test_bootstrap_is_idempotent_and_tables_are_isolated() -> None:
    first_settings = make_settings()
    second_settings = make_settings()
    first = ensure_table(first_settings)
    second = ensure_table(second_settings)
    try:
        same_first = ensure_table(first_settings)
        first.put_item(Item={"PK": "ISOLATION", "SK": "MARKER"})

        assert same_first.name == first.name
        assert first.name != second.name
        assert "Item" not in second.get_item(Key={"PK": "ISOLATION", "SK": "MARKER"})
        assert {index["IndexName"] for index in first.global_secondary_indexes} == {
            "GSI1",
            "GSI2",
        }
    finally:
        delete_table(first)
        delete_table(second)


def test_concurrent_bootstrap_creates_one_usable_table() -> None:
    settings = make_settings()
    barrier = Barrier(2)

    def bootstrap():
        barrier.wait()
        return ensure_table(settings)

    with ThreadPoolExecutor(max_workers=2) as executor:
        tables = list(executor.map(lambda _: bootstrap(), range(2)))
    try:
        assert {table.name for table in tables} == {settings.dynamodb_table}
        tables[0].put_item(Item={"PK": "BOOTSTRAP", "SK": "OK"})
        assert tables[1].get_item(Key={"PK": "BOOTSTRAP", "SK": "OK"})["Item"]
    finally:
        delete_table(tables[0])


def test_quote_snapshots_and_guards_are_persisted(repository) -> None:
    quote = make_quote()
    repository.create_quote(quote)

    stored = repository.get_quote(quote.quote_id)
    guard = repository.table.get_item(
        Key={"PK": f"ORDER#{quote.order_id}", "SK": "QUOTE_GUARD"},
        ConsistentRead=True,
    )["Item"]

    assert stored == quote
    assert stored.total == Decimal("200.00")
    assert stored.services == quote.services
    assert stored.items == quote.items
    assert stored.customer_contact == quote.customer_contact
    assert guard["quote_id"] == str(quote.quote_id)
    assert guard["status"] == QuoteStatus.RASCUNHO

    with pytest.raises(FinanceConflictError):
        repository.create_quote(make_quote(order_id=quote.order_id))


@pytest.mark.parametrize(
    ("transition", "expected", "reason"),
    [
        ("approve", QuoteStatus.APROVADO, None),
        ("reject", QuoteStatus.RECUSADO, "valor"),
    ],
)
def test_quote_approval_and_rejection_update_document_and_guard(
    repository, transition, expected, reason
) -> None:
    quote = repository.create_quote(make_quote())
    communicated = repository.save_quote_transition(quote, quote.comunicar())
    updated = (
        communicated.aprovar()
        if transition == "approve"
        else communicated.recusar(reason)
    )
    stored = repository.save_quote_transition(communicated, updated)
    guard = repository.table.get_item(
        Key={"PK": f"ORDER#{quote.order_id}", "SK": "QUOTE_GUARD"},
        ConsistentRead=True,
    )["Item"]

    assert stored.status == expected
    assert stored.rejection_reason == reason
    assert stored.version == 3
    assert guard["status"] == expected
    assert int(guard["version"]) == stored.version
    with pytest.raises(FinanceConflictError):
        repository.save_quote_transition(quote, quote.comunicar())


def test_first_payment_and_same_key_are_idempotent(repository) -> None:
    quote = repository.create_quote(make_quote())
    first = create_payment(repository, quote)
    repeated = create_payment(repository, quote)
    idem_items = repository.table.scan(
        FilterExpression="begins_with(PK, :prefix)",
        ExpressionAttributeValues={":prefix": "IDEMPOTENCY#PAYMENT_CREATE#"},
    )["Items"]

    assert repeated == first
    assert first.attempt_number == 1
    assert len(idem_items) == 1
    assert "ttl" not in idem_items[0]
    assert idem_items[0]["response"]["payment_id"] == str(first.payment_id)
    assert get_guard(repository, quote)["payment_id"] == str(first.payment_id)


def test_same_key_with_different_payload_conflicts_without_changes(repository) -> None:
    quote = repository.create_quote(make_quote())
    first = create_payment(repository, quote)

    with pytest.raises(IdempotencyConflictError):
        repository.create_payment(
            order_id=quote.order_id,
            quote_id=quote.quote_id,
            quote_version=quote.version,
            method="CREDIT_CARD",
            amount=quote.total,
            idempotency_key="payment-request-1",
        )

    assert repository.get_payment(first.payment_id) == first
    assert get_guard(repository, quote)["payment_id"] == str(first.payment_id)
    assert len(repository.list_order_history(quote.order_id)) == 2


@pytest.mark.parametrize(
    "terminal_status",
    [PaymentStatus.FAILED, PaymentStatus.EXPIRED, PaymentStatus.REJECTED],
)
def test_new_attempt_is_allowed_only_after_retryable_status(repository, terminal_status) -> None:
    quote = repository.create_quote(make_quote())
    first = create_payment(repository, quote, "attempt-1")
    ended = repository.transition_payment_status(first.payment_id, terminal_status)
    second = create_payment(repository, quote, "attempt-2")
    history = repository.list_order_history(quote.order_id)
    payments = [item for item in history if item["entity_type"] == "PAYMENT"]
    guard = get_guard(repository, quote)

    assert ended.status == terminal_status
    assert second.payment_id != first.payment_id
    assert second.attempt_number == 2
    assert {(item["payment_id"], int(item["attempt_number"])) for item in payments} == {
        (str(first.payment_id), 1),
        (str(second.payment_id), 2),
    }
    assert guard["payment_id"] == str(second.payment_id)
    assert guard["status"] == PaymentStatus.PENDING
    assert int(guard["attempt_number"]) == 2


@pytest.mark.parametrize("blocking_status", [PaymentStatus.PENDING, PaymentStatus.UNKNOWN])
def test_active_or_unknown_attempt_blocks_a_new_key(repository, blocking_status) -> None:
    quote = repository.create_quote(make_quote())
    first = create_payment(repository, quote, "attempt-1")
    if blocking_status == PaymentStatus.UNKNOWN:
        first = repository.transition_payment_status(first.payment_id, blocking_status)

    with pytest.raises(FinanceConflictError):
        create_payment(repository, quote, "attempt-2")

    guard = get_guard(repository, quote)
    assert first.status == blocking_status
    assert guard["status"] == blocking_status
    assert guard["payment_id"] == str(first.payment_id)


def test_unknown_is_not_treated_as_rejection_and_can_be_resolved(repository) -> None:
    quote = repository.create_quote(make_quote())
    payment = create_payment(repository, quote)

    unknown = repository.transition_payment_status(payment.payment_id, PaymentStatus.UNKNOWN)
    failed = repository.transition_payment_status(payment.payment_id, PaymentStatus.FAILED)

    assert unknown.status == PaymentStatus.UNKNOWN
    assert failed.status == PaymentStatus.FAILED
    assert failed.version == 3
    assert get_guard(repository, quote)["status"] == PaymentStatus.FAILED


def test_confirmation_blocks_retries_and_external_reference_is_unique(repository) -> None:
    first_quote = repository.create_quote(make_quote())
    first = create_payment(repository, first_quote, "first-order")
    confirmed = repository.confirm_payment(first.payment_id, "simulated-ref-1")

    assert repository.confirm_payment(first.payment_id, "simulated-ref-1") == confirmed
    with pytest.raises(FinanceConflictError):
        repository.confirm_payment(first.payment_id, "another-ref")
    with pytest.raises(FinanceConflictError):
        create_payment(repository, first_quote, "after-confirmation")

    second_quote = repository.create_quote(make_quote())
    second = create_payment(repository, second_quote, "second-order")
    with pytest.raises(FinanceConflictError):
        repository.confirm_payment(second.payment_id, "simulated-ref-1")

    assert repository.get_payment(second.payment_id).status == PaymentStatus.PENDING
    assert get_guard(repository, second_quote)["status"] == PaymentStatus.PENDING


def test_concurrent_payment_requests_create_only_one_active_attempt(repository) -> None:
    quote = repository.create_quote(make_quote())
    barrier = Barrier(2)

    def request_payment(key):
        worker_repository = FinanceRepository(repository.table)
        barrier.wait()
        try:
            return create_payment(worker_repository, quote, key)
        except FinanceConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request_payment, ("concurrent-1", "concurrent-2")))

    successes = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, FinanceConflictError)]
    history = repository.list_order_history(quote.order_id)
    payments = [item for item in history if item["entity_type"] == "PAYMENT"]

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(payments) == 1
    assert int(payments[0]["attempt_number"]) == 1
    assert get_guard(repository, quote)["payment_id"] == str(successes[0].payment_id)


def test_concurrent_same_idempotency_key_returns_the_same_payment(repository) -> None:
    quote = repository.create_quote(make_quote())
    barrier = Barrier(2)

    def request_payment(_):
        worker_repository = FinanceRepository(repository.table)
        barrier.wait()
        return create_payment(worker_repository, quote, "same-concurrent-key")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request_payment, range(2)))

    assert results[0].payment_id == results[1].payment_id
    assert len(
        [
            item
            for item in repository.list_order_history(quote.order_id)
            if item["entity_type"] == "PAYMENT"
        ]
    ) == 1


def test_concurrent_status_updates_preserve_terminal_state_and_guard(repository) -> None:
    quote = repository.create_quote(make_quote())
    payment = create_payment(repository, quote)
    barrier = Barrier(2)

    def update(status):
        worker_repository = FinanceRepository(repository.table)
        barrier.wait()
        try:
            return worker_repository.transition_payment_status(payment.payment_id, status)
        except FinanceConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(update, (PaymentStatus.REJECTED, PaymentStatus.FAILED))
        )

    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == 1
    stored = repository.get_payment(payment.payment_id)
    guard = get_guard(repository, quote)
    assert stored.status == successes[0].status
    assert stored.status in {PaymentStatus.REJECTED, PaymentStatus.FAILED}
    assert guard["status"] == stored.status
    assert int(guard["version"]) == stored.version
    with pytest.raises(FinanceConflictError):
        repository.transition_payment_status(payment.payment_id, PaymentStatus.PROCESSING)


def test_outbox_structure_is_persisted_and_indexed_without_publisher(repository) -> None:
    record = OutboxRecord(
        message_id=uuid4(),
        event_type="QuoteApproved.v1",
        aggregate_id=uuid4(),
        payload={"quote_id": str(uuid4()), "version": 1},
        next_attempt_at=datetime.now(UTC),
    )
    repository.save_outbox_record(record)

    stored = repository.get_outbox_record(record.message_id)
    indexed = repository.table.query(
        IndexName="GSI2",
        KeyConditionExpression=Key("GSI2PK").eq("OUTBOX#PENDING"),
    )["Items"]

    assert stored == record
    assert len(indexed) == 1
    assert indexed[0]["message_id"] == str(record.message_id)
    assert indexed[0]["GSI2SK"].endswith(f"#{record.message_id}")
    with pytest.raises(FinanceConflictError):
        repository.save_outbox_record(record)
