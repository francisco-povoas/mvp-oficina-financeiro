from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.models import PaymentAttempt, PaymentStatus, Quote, QuoteLine, QuoteStatus


def line(quantity: int = 1, price: str = "100.00") -> QuoteLine:
    return QuoteLine(
        reference_id=uuid4(),
        description="Troca de óleo",
        quantity=quantity,
        unit_price=Decimal(price),
    )


def quote() -> Quote:
    return Quote(
        quote_id=uuid4(),
        order_id=uuid4(),
        order_version=1,
        services=(line(),),
        items=(line(2, "25.00"),),
        customer_contact={"email": "cliente@example.com"},
    )


def test_quote_calculates_snapshot_total_and_valid_transitions() -> None:
    original = quote()

    communicated = original.comunicar()
    approved = communicated.aprovar()

    assert original.total == Decimal("150.00")
    assert original.status == QuoteStatus.RASCUNHO
    assert communicated.status == QuoteStatus.COMUNICADO
    assert approved.status == QuoteStatus.APROVADO
    assert approved.version == 3


def test_quote_rejection_preserves_reason() -> None:
    rejected = quote().comunicar().recusar("valor")

    assert rejected.status == QuoteStatus.RECUSADO
    assert rejected.rejection_reason == "valor"


def test_quote_rejects_invalid_transition() -> None:
    with pytest.raises(ValueError):
        quote().aprovar()


def test_payment_requires_positive_amount_and_attempt() -> None:
    with pytest.raises(ValueError):
        PaymentAttempt(
            payment_id=uuid4(),
            order_id=uuid4(),
            quote_id=uuid4(),
            quote_version=1,
            attempt_number=0,
            status=PaymentStatus.PENDING,
            method="PIX",
            amount=Decimal("0"),
        )
