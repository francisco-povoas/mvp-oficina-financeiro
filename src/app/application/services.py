from decimal import Decimal
from uuid import UUID, uuid4

from app.domain.exceptions import FinanceConflictError, FinanceNotFoundError
from app.domain.models import (
    CustomerContact,
    PaymentAttempt,
    Quote,
    QuoteCommunicationEvidence,
    QuoteLine,
    QuoteStatus,
)
from app.infrastructure.dynamodb import FinanceRepository


class FinanceService:
    def __init__(self, repository: FinanceRepository) -> None:
        self.repository = repository

    def create_quote(
        self,
        *,
        order_id: UUID,
        order_version: int,
        order_status: str,
        services: tuple[QuoteLine, ...],
        items: tuple[QuoteLine, ...],
        customer_contact: CustomerContact,
    ) -> Quote:
        return self.repository.create_quote(
            Quote(
                quote_id=uuid4(),
                order_id=order_id,
                order_version=order_version,
                order_status=order_status,
                services=tuple(line for line in services if line.active),
                items=tuple(line for line in items if line.active),
                customer_contact=customer_contact,
            )
        )

    def communicate_quote(
        self,
        quote_id: UUID,
        evidences: tuple[QuoteCommunicationEvidence, ...],
    ) -> Quote:
        quote = self._get_quote(quote_id)
        return self.repository.record_quote_communication(quote, evidences)

    def get_quote(self, quote_id: UUID) -> Quote:
        return self._get_quote(quote_id)

    def list_quote_communications(
        self, quote_id: UUID
    ) -> list[QuoteCommunicationEvidence]:
        self._get_quote(quote_id)
        return self.repository.list_quote_communications(quote_id)

    def approve_quote(self, quote_id: UUID) -> Quote:
        quote = self._get_quote(quote_id)
        return self.repository.save_quote_transition(quote, quote.aprovar())

    def reject_quote(self, quote_id: UUID, reason: str | None) -> Quote:
        quote = self._get_quote(quote_id)
        return self.repository.save_quote_transition(quote, quote.recusar(reason))

    def create_payment(
        self,
        *,
        quote_id: UUID,
        method: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> PaymentAttempt:
        quote = self._get_quote(quote_id)
        if quote.status != QuoteStatus.APROVADO:
            raise FinanceConflictError("O orçamento precisa estar aprovado")
        if amount < quote.total:
            raise FinanceConflictError("O valor não pode ser menor que o orçamento aprovado")
        return self.repository.create_payment(
            order_id=quote.order_id,
            quote_id=quote.quote_id,
            quote_version=quote.version,
            method=method,
            amount=amount,
            idempotency_key=idempotency_key,
        )

    def confirm_simulated_payment(
        self, payment_id: UUID, provider_reference: str
    ) -> PaymentAttempt:
        return self.repository.confirm_payment(payment_id, provider_reference)

    def _get_quote(self, quote_id: UUID) -> Quote:
        quote = self.repository.get_quote(quote_id)
        if quote is None:
            raise FinanceNotFoundError("Orçamento não encontrado")
        return quote
