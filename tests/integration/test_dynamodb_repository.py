from decimal import Decimal
from uuid import uuid4

import pytest

from app.config import Settings
from app.domain.exceptions import FinanceConflictError, IdempotencyConflictError
from app.domain.models import PaymentStatus, Quote, QuoteLine
from app.infrastructure.dynamodb import FinanceRepository, ensure_table

pytestmark = pytest.mark.integration


@pytest.fixture
def repository(monkeypatch):
    table_name = f"mvp-oficina-financeiro-test-{uuid4().hex}"
    settings = Settings(
        environment="test",
        dynamodb_endpoint_url="http://dynamodb-test:8000",
        dynamodb_table=table_name,
    )
    table = ensure_table(settings)
    yield FinanceRepository(table)
    table.delete()


def make_quote() -> Quote:
    return Quote(
        quote_id=uuid4(),
        order_id=uuid4(),
        order_version=4,
        services=(
            QuoteLine(
                reference_id=uuid4(),
                description="Alinhamento",
                quantity=1,
                unit_price=Decimal("150.00"),
            ),
        ),
        items=(),
        customer_contact={"telefone": "5511999999999"},
    )


def test_bootstrap_and_quote_guards_are_idempotent(repository) -> None:
    quote = make_quote()
    repository.create_quote(quote)

    stored = repository.get_quote(quote.quote_id)
    assert stored == quote

    with pytest.raises(FinanceConflictError):
        repository.create_quote(make_quote().__class__(
            **{**make_quote().__dict__, "order_id": quote.order_id}
        ))


def test_quote_transition_is_conditional(repository) -> None:
    quote = repository.create_quote(make_quote())
    communicated = repository.save_quote_transition(quote, quote.comunicar())
    approved = repository.save_quote_transition(communicated, communicated.aprovar())

    assert approved.version == 3
    with pytest.raises(FinanceConflictError):
        repository.save_quote_transition(quote, quote.comunicar())


def test_payment_creation_is_idempotent_and_active_guard_blocks_new_key(repository) -> None:
    quote = repository.create_quote(make_quote())
    first = repository.create_payment(
        order_id=quote.order_id,
        quote_id=quote.quote_id,
        quote_version=quote.version,
        method="PIX",
        amount=quote.total,
        idempotency_key="payment-request-1",
    )
    repeated = repository.create_payment(
        order_id=quote.order_id,
        quote_id=quote.quote_id,
        quote_version=quote.version,
        method="PIX",
        amount=quote.total,
        idempotency_key="payment-request-1",
    )

    assert repeated.payment_id == first.payment_id
    assert repeated.attempt_number == 1
    with pytest.raises(IdempotencyConflictError):
        repository.create_payment(
            order_id=quote.order_id,
            quote_id=quote.quote_id,
            quote_version=quote.version,
            method="CREDIT_CARD",
            amount=quote.total,
            idempotency_key="payment-request-1",
        )
    with pytest.raises(FinanceConflictError):
        repository.create_payment(
            order_id=quote.order_id,
            quote_id=quote.quote_id,
            quote_version=quote.version,
            method="PIX",
            amount=quote.total,
            idempotency_key="payment-request-2",
        )


def test_simulated_confirmation_and_order_history(repository) -> None:
    quote = repository.create_quote(make_quote())
    payment = repository.create_payment(
        order_id=quote.order_id,
        quote_id=quote.quote_id,
        quote_version=quote.version,
        method="PIX",
        amount=quote.total,
        idempotency_key="payment-confirm",
    )

    confirmed = repository.confirm_payment(payment.payment_id, "simulated-ref-1")
    history = repository.list_order_history(quote.order_id)

    assert confirmed.status == PaymentStatus.CONFIRMED
    assert {item["entity_type"] for item in history} == {"QUOTE", "PAYMENT"}
    with pytest.raises(FinanceConflictError):
        repository.create_payment(
            order_id=quote.order_id,
            quote_id=quote.quote_id,
            quote_version=quote.version,
            method="PIX",
            amount=quote.total,
            idempotency_key="after-confirmation",
        )
