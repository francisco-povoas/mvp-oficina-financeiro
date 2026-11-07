from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class QuoteStatus(StrEnum):
    RASCUNHO = "RASCUNHO"
    COMUNICADO = "COMUNICADO"
    APROVADO = "APROVADO"
    RECUSADO = "RECUSADO"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


ACTIVE_PAYMENT_STATUSES = {
    PaymentStatus.PENDING,
    PaymentStatus.PROCESSING,
    PaymentStatus.UNKNOWN,
}
RETRYABLE_PAYMENT_STATUSES = {
    PaymentStatus.FAILED,
    PaymentStatus.EXPIRED,
    PaymentStatus.REJECTED,
}


@dataclass(frozen=True, kw_only=True)
class QuoteLine:
    reference_id: UUID
    description: str
    quantity: int
    unit_price: Decimal

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("A descrição do item orçado é obrigatória")
        if self.quantity <= 0:
            raise ValueError("A quantidade do item orçado deve ser positiva")
        if self.unit_price < Decimal("0"):
            raise ValueError("O preço unitário não pode ser negativo")

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass(frozen=True, kw_only=True)
class Quote:
    quote_id: UUID
    order_id: UUID
    order_version: int
    services: tuple[QuoteLine, ...]
    items: tuple[QuoteLine, ...]
    customer_contact: dict[str, str]
    status: QuoteStatus = QuoteStatus.RASCUNHO
    version: int = 1
    rejection_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.order_version < 1:
            raise ValueError("A versão da ordem deve ser positiva")
        if not self.services and not self.items:
            raise ValueError("O orçamento deve possuir ao menos um item ou serviço")

    @property
    def total(self) -> Decimal:
        return sum((line.total for line in (*self.services, *self.items)), Decimal("0"))

    def comunicar(self) -> "Quote":
        if self.status != QuoteStatus.RASCUNHO:
            raise ValueError("Somente orçamento em rascunho pode ser comunicado")
        return replace(
            self,
            status=QuoteStatus.COMUNICADO,
            version=self.version + 1,
            updated_at=datetime.now(UTC),
        )

    def aprovar(self) -> "Quote":
        if self.status != QuoteStatus.COMUNICADO:
            raise ValueError("Somente orçamento comunicado pode ser aprovado")
        return replace(
            self,
            status=QuoteStatus.APROVADO,
            version=self.version + 1,
            updated_at=datetime.now(UTC),
        )

    def recusar(self, reason: str | None = None) -> "Quote":
        if self.status != QuoteStatus.COMUNICADO:
            raise ValueError("Somente orçamento comunicado pode ser recusado")
        return replace(
            self,
            status=QuoteStatus.RECUSADO,
            rejection_reason=reason,
            version=self.version + 1,
            updated_at=datetime.now(UTC),
        )


@dataclass(frozen=True, kw_only=True)
class PaymentAttempt:
    payment_id: UUID
    order_id: UUID
    quote_id: UUID
    quote_version: int
    attempt_number: int
    status: PaymentStatus
    method: str
    amount: Decimal
    provider: str = "simulated"
    provider_reference: str | None = None
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.amount <= Decimal("0"):
            raise ValueError("O valor do pagamento deve ser positivo")
        if self.attempt_number < 1:
            raise ValueError("O número da tentativa deve ser positivo")


@dataclass(frozen=True, kw_only=True)
class OutboxRecord:
    """Estrutura persistente prevista; publicação começa somente na Etapa 2."""

    message_id: UUID
    event_type: str
    aggregate_id: UUID
    payload: dict[str, object]
    next_attempt_at: datetime
    status: str = "PENDING"
    attempt_count: int = 0
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("O tipo do evento da Outbox é obrigatório")
        if self.status != "PENDING":
            raise ValueError("A Outbox estrutural da Etapa 1 aceita somente registros pendentes")
        if self.attempt_count != 0:
            raise ValueError("Retentativas da Outbox pertencem à Etapa 2")
