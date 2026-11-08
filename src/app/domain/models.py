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


class QuoteCommunicationChannel(StrEnum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"


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
    active: bool = True

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("A descrição do item orçado é obrigatória")
        if self.quantity <= 0:
            raise ValueError("A quantidade do item orçado deve ser positiva")
        if self.unit_price < Decimal("0"):
            raise ValueError("O preço unitário não pode ser negativo")

    @property
    def total(self) -> Decimal:
        if not self.active:
            return Decimal("0")
        return self.unit_price * self.quantity


@dataclass(frozen=True, kw_only=True)
class CustomerContact:
    email: str
    telefone: str

    def __post_init__(self) -> None:
        if not self.email.strip():
            raise ValueError("O e-mail do cliente é obrigatório")
        if not self.telefone.strip():
            raise ValueError("O telefone do cliente é obrigatório")


@dataclass(frozen=True, kw_only=True)
class QuoteCommunicationEvidence:
    evidence_id: UUID
    communication_id: UUID
    quote_id: UUID
    order_id: UUID
    channel: QuoteCommunicationChannel
    destination: str
    success: bool
    message: str
    provider: str
    sent_at: datetime
    external_reference: str | None = None
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.destination.strip():
            raise ValueError("O destino da comunicação é obrigatório")
        if not self.message.strip():
            raise ValueError("A mensagem da evidência é obrigatória")
        if not self.provider.strip():
            raise ValueError("O provedor da evidência é obrigatório")
        if self.sent_at.tzinfo is None or self.sent_at.utcoffset() is None:
            raise ValueError("O horário de envio deve possuir fuso horário")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("O horário de registro deve possuir fuso horário")


@dataclass(frozen=True, kw_only=True)
class Quote:
    quote_id: UUID
    order_id: UUID
    order_version: int
    order_status: str
    services: tuple[QuoteLine, ...]
    items: tuple[QuoteLine, ...]
    customer_contact: CustomerContact
    status: QuoteStatus = QuoteStatus.RASCUNHO
    version: int = 1
    rejection_reason: str | None = None
    communicated_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.order_version < 1:
            raise ValueError("A versão da ordem deve ser positiva")
        if self.order_status != "DIAGNOSTICO_CONCLUIDO":
            raise ValueError(
                "Só é possível gerar orçamento para ordem em DIAGNOSTICO_CONCLUIDO"
            )
        if not any(line.active for line in self.services):
            raise ValueError("A ordem deve possuir ao menos um serviço ativo")
        if any(not line.active for line in (*self.services, *self.items)):
            raise ValueError("O orçamento deve conter somente snapshots ativos")
        self._validate_unique_references(self.services, "serviço")
        self._validate_unique_references(self.items, "item")

    @staticmethod
    def _validate_unique_references(lines: tuple[QuoteLine, ...], kind: str) -> None:
        references = [line.reference_id for line in lines]
        if len(references) != len(set(references)):
            raise ValueError(f"O snapshot não pode repetir o mesmo {kind}")

    @property
    def total_services(self) -> Decimal:
        return sum((line.total for line in self.services), Decimal("0"))

    @property
    def total_items(self) -> Decimal:
        return sum((line.total for line in self.items), Decimal("0"))

    @property
    def total(self) -> Decimal:
        return self.total_services + self.total_items

    def comunicar(
        self, evidences: tuple[QuoteCommunicationEvidence, ...]
    ) -> "Quote":
        if self.status != QuoteStatus.RASCUNHO:
            raise ValueError("Somente orçamento em rascunho pode ser comunicado")
        self.validate_communication_evidences(evidences)
        if not all(evidence.success for evidence in evidences):
            raise ValueError("Os dois canais precisam possuir evidência de sucesso")
        now = datetime.now(UTC)
        return replace(
            self,
            status=QuoteStatus.COMUNICADO,
            version=self.version + 1,
            communicated_at=now,
            updated_at=now,
        )

    def validate_communication_evidences(
        self, evidences: tuple[QuoteCommunicationEvidence, ...]
    ) -> None:
        required_channels = {
            QuoteCommunicationChannel.WHATSAPP,
            QuoteCommunicationChannel.EMAIL,
        }
        if len(evidences) != len(required_channels):
            raise ValueError("A comunicação exige uma evidência por canal")
        if {evidence.channel for evidence in evidences} != required_channels:
            raise ValueError("A comunicação exige WhatsApp e e-mail")
        if len({evidence.evidence_id for evidence in evidences}) != len(evidences):
            raise ValueError("As evidências devem possuir identificadores distintos")
        if len({evidence.communication_id for evidence in evidences}) != 1:
            raise ValueError("As evidências devem pertencer à mesma comunicação")
        destinations = {
            QuoteCommunicationChannel.WHATSAPP: self.customer_contact.telefone,
            QuoteCommunicationChannel.EMAIL: self.customer_contact.email,
        }
        for evidence in evidences:
            if evidence.quote_id != self.quote_id or evidence.order_id != self.order_id:
                raise ValueError("A evidência não pertence ao orçamento")
            if evidence.destination != destinations[evidence.channel]:
                raise ValueError("O destino da evidência difere do contato do orçamento")

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
