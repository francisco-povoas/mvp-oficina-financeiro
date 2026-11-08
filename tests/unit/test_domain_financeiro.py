from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.models import (
    CustomerContact,
    PaymentAttempt,
    PaymentStatus,
    Quote,
    QuoteCommunicationChannel,
    QuoteCommunicationEvidence,
    QuoteLine,
    QuoteStatus,
)


def line(
    quantity: int = 1,
    price: str = "100.00",
    *,
    active: bool = True,
    reference_id=None,
) -> QuoteLine:
    return QuoteLine(
        reference_id=reference_id or uuid4(),
        description="Troca de óleo",
        quantity=quantity,
        unit_price=Decimal(price),
        active=active,
    )


def quote() -> Quote:
    return Quote(
        quote_id=uuid4(),
        order_id=uuid4(),
        order_version=1,
        order_status="DIAGNOSTICO_CONCLUIDO",
        services=(line(),),
        items=(line(2, "25.00"),),
        customer_contact=CustomerContact(
            email="cliente@example.com", telefone="5511999999999"
        ),
    )


def communication_evidences(
    quote: Quote, *, success: bool = True
) -> tuple[QuoteCommunicationEvidence, ...]:
    communication_id = uuid4()
    now = datetime.now(UTC)
    return tuple(
        QuoteCommunicationEvidence(
            evidence_id=uuid4(),
            communication_id=communication_id,
            quote_id=quote.quote_id,
            order_id=quote.order_id,
            channel=channel,
            destination=(
                quote.customer_contact.email
                if channel == QuoteCommunicationChannel.EMAIL
                else quote.customer_contact.telefone
            ),
            success=success,
            message="enviado" if success else "falhou",
            provider="mock",
            sent_at=now,
        )
        for channel in QuoteCommunicationChannel
    )


def test_quote_calculates_snapshot_total_and_valid_transitions() -> None:
    original = quote()

    communicated = original.comunicar(communication_evidences(original))
    approved = communicated.aprovar()

    assert original.total_services == Decimal("100.00")
    assert original.total_items == Decimal("50.00")
    assert original.total == Decimal("150.00")
    assert original.status == QuoteStatus.RASCUNHO
    assert communicated.status == QuoteStatus.COMUNICADO
    assert communicated.communicated_at is not None
    assert approved.status == QuoteStatus.APROVADO
    assert approved.version == 3


def test_quote_rejection_preserves_reason() -> None:
    original = quote()
    rejected = original.comunicar(communication_evidences(original)).recusar("valor")

    assert rejected.status == QuoteStatus.RECUSADO
    assert rejected.rejection_reason == "valor"


def test_quote_rejects_invalid_transition() -> None:
    with pytest.raises(ValueError):
        quote().aprovar()


def test_quote_requires_diagnosis_completed_and_an_active_service() -> None:
    original = quote()
    with pytest.raises(ValueError, match="DIAGNOSTICO_CONCLUIDO"):
        Quote(**{**original.__dict__, "order_status": "RECEBIDA"})
    with pytest.raises(ValueError, match="serviço ativo"):
        Quote(**{**original.__dict__, "services": (line(active=False),)})


def test_quote_requires_contacts_and_unique_snapshot_references() -> None:
    with pytest.raises(ValueError, match="telefone"):
        CustomerContact(email="cliente@example.com", telefone=" ")

    original = quote()
    repeated_id = uuid4()
    with pytest.raises(ValueError, match="repetir o mesmo serviço"):
        Quote(
            **{
                **original.__dict__,
                "services": (
                    line(reference_id=repeated_id),
                    line(reference_id=repeated_id),
                ),
            }
        )


def test_quote_requires_one_valid_evidence_for_each_channel() -> None:
    original = quote()
    evidences = communication_evidences(original)

    with pytest.raises(ValueError, match="uma evidência por canal"):
        original.comunicar(evidences[:1])
    with pytest.raises(ValueError, match="dois canais"):
        original.comunicar(communication_evidences(original, success=False))


def test_quote_rejects_inactive_snapshots() -> None:
    original = quote()
    with pytest.raises(ValueError, match="somente snapshots ativos"):
        Quote(
            **{
                **original.__dict__,
                "services": (*original.services, line(price="999", active=False)),
            }
        )


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
