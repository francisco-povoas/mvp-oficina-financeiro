from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.domain.models import (
    PaymentAttempt,
    Quote,
    QuoteCommunicationEvidence,
)


class FinanceRepositoryPort(Protocol):
    """Persistence operations required by the financial application service."""

    def create_quote(self, quote: Quote) -> Quote: ...

    def get_quote(self, quote_id: UUID) -> Quote | None: ...

    def record_quote_communication(
        self,
        quote: Quote,
        evidences: tuple[QuoteCommunicationEvidence, ...],
    ) -> Quote: ...

    def list_quote_communications(
        self, quote_id: UUID
    ) -> list[QuoteCommunicationEvidence]: ...

    def save_quote_transition(self, previous: Quote, updated: Quote) -> Quote: ...

    def create_payment(
        self,
        *,
        order_id: UUID,
        quote_id: UUID,
        quote_version: int,
        method: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> PaymentAttempt: ...

    def confirm_payment(
        self, payment_id: UUID, provider_reference: str
    ) -> PaymentAttempt: ...


@dataclass(frozen=True, kw_only=True)
class PaymentConfirmation:
    provider_reference: str


class PaymentProviderPort(Protocol):
    """External payment provider used by the application."""

    def confirm_payment(
        self, payment_id: UUID, provider_reference: str
    ) -> PaymentConfirmation: ...
