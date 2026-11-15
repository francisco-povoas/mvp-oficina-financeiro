from decimal import Decimal
from uuid import UUID, uuid4

from app.application.ports import (
    FinanceRepositoryPort,
    OrdersContextPort,
    PaymentProviderPort,
    ProviderPayment,
)
from app.domain.exceptions import (
    FinanceConflictError,
    FinanceNotFoundError,
    PaymentProviderContractError,
    PaymentProviderRejectedError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
)
from app.domain.models import (
    CustomerContact,
    PaymentAttempt,
    PaymentStatus,
    Quote,
    QuoteCommunicationEvidence,
    QuoteLine,
    QuoteStatus,
)


class FinanceService:
    def __init__(
        self,
        repository: FinanceRepositoryPort,
        payment_provider: PaymentProviderPort,
        orders_context: OrdersContextPort | None = None,
    ) -> None:
        self.repository = repository
        self.payment_provider = payment_provider
        self.orders_context = orders_context

    def create_quote_from_order(
        self,
        *,
        order_id: UUID,
        correlation_id: str,
    ) -> Quote:
        if self.orders_context is None:
            raise FinanceConflictError("Consulta de Ordens não configurada")
        context = self.orders_context.get_quote_context(order_id, correlation_id)
        return self.create_quote(
            order_id=context.order_id,
            order_version=context.order_version,
            order_status=context.status,
            services=tuple(
                QuoteLine(
                    reference_id=line.reference_id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                )
                for line in context.services
            ),
            items=tuple(
                QuoteLine(
                    reference_id=line.reference_id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                )
                for line in context.items
            ),
            customer_contact=CustomerContact(
                email=context.email,
                telefone=context.telefone,
            ),
        )

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
        *,
        correlation_id: UUID | None = None,
    ) -> Quote:
        quote = self._get_quote(quote_id)
        return self.repository.record_quote_communication(
            quote,
            evidences,
            correlation_id=correlation_id,
        )

    def get_quote(self, quote_id: UUID) -> Quote:
        return self._get_quote(quote_id)

    def list_quote_communications(
        self, quote_id: UUID
    ) -> list[QuoteCommunicationEvidence]:
        self._get_quote(quote_id)
        return self.repository.list_quote_communications(quote_id)

    def approve_quote(
        self,
        quote_id: UUID,
        *,
        correlation_id: UUID | None = None,
    ) -> Quote:
        quote = self._get_quote(quote_id)
        return self.repository.save_quote_transition(
            quote,
            quote.aprovar(),
            correlation_id=correlation_id,
        )

    def reject_quote(
        self,
        quote_id: UUID,
        reason: str | None,
        *,
        correlation_id: UUID | None = None,
    ) -> Quote:
        quote = self._get_quote(quote_id)
        return self.repository.save_quote_transition(
            quote,
            quote.recusar(reason),
            correlation_id=correlation_id,
        )

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
        payment = self.repository.create_payment(
            order_id=quote.order_id,
            quote_id=quote.quote_id,
            quote_version=quote.version,
            method=method,
            amount=amount,
            idempotency_key=idempotency_key,
            provider=self.payment_provider.name,
        )
        if payment.status == PaymentStatus.CONFIRMED:
            return payment
        try:
            provider_payment = self.payment_provider.create_payment(
                payment,
                payer_email=quote.customer_contact.email,
            )
        except PaymentProviderRejectedError:
            self.repository.transition_payment_status(
                payment.payment_id, PaymentStatus.FAILED
            )
            raise
        except (
            PaymentProviderContractError,
            PaymentProviderTimeoutError,
            PaymentProviderUnavailableError,
        ):
            self.repository.transition_payment_status(
                payment.payment_id, PaymentStatus.UNKNOWN
            )
            raise
        if provider_payment is None:
            return payment
        return self._apply_provider_payment(payment, provider_payment)

    def create_payment_from_order(
        self,
        *,
        quote_id: UUID,
        method: str,
        amount: Decimal,
        idempotency_key: str,
        correlation_id: str,
    ) -> PaymentAttempt:
        quote = self._get_quote(quote_id)
        if quote.status != QuoteStatus.APROVADO:
            raise FinanceConflictError("O orçamento precisa estar aprovado")
        if self.orders_context is None:
            raise FinanceConflictError("Consulta de Ordens não configurada")
        self.orders_context.get_payment_context(
            order_id=quote.order_id,
            quote_id=quote.quote_id,
            quote_version=quote.version,
            approved_total=quote.total,
            correlation_id=correlation_id,
        )
        return self.create_payment(
            quote_id=quote_id,
            method=method,
            amount=amount,
            idempotency_key=idempotency_key,
        )

    def confirm_simulated_payment(
        self,
        payment_id: UUID,
        provider_reference: str,
        *,
        correlation_id: UUID | None = None,
    ) -> PaymentAttempt:
        confirmation = self.payment_provider.confirm_payment(
            payment_id, provider_reference
        )
        return self.repository.confirm_payment(
            payment_id,
            confirmation.provider_reference,
            correlation_id=correlation_id,
        )

    def process_provider_webhook(
        self,
        *,
        provider_reference: str,
        signature: str,
        request_id: str,
        correlation_id: UUID | None = None,
    ) -> PaymentAttempt:
        self.payment_provider.validate_webhook(
            signature=signature,
            request_id=request_id,
            data_id=provider_reference,
        )
        provider_payment = self.payment_provider.get_payment(provider_reference)
        try:
            payment_id = UUID(provider_payment.external_reference)
        except ValueError as exc:
            raise FinanceConflictError(
                "Referência externa do provedor não identifica uma tentativa"
            ) from exc
        payment = self.repository.get_payment(payment_id)
        if payment is None:
            raise FinanceNotFoundError("Pagamento não encontrado")
        return self._apply_provider_payment(
            payment,
            provider_payment,
            correlation_id=correlation_id,
        )

    def reconcile_payment(
        self,
        payment_id: UUID,
        *,
        correlation_id: UUID | None = None,
    ) -> PaymentAttempt:
        payment = self.repository.get_payment(payment_id)
        if payment is None:
            raise FinanceNotFoundError("Pagamento não encontrado")
        if payment.status == PaymentStatus.CONFIRMED:
            return payment
        provider_payment = (
            self.payment_provider.get_payment(payment.provider_reference)
            if payment.provider_reference
            else self.payment_provider.find_payment(str(payment.payment_id))
        )
        if provider_payment is None:
            if payment.status != PaymentStatus.UNKNOWN:
                return self.repository.transition_payment_status(
                    payment.payment_id,
                    PaymentStatus.UNKNOWN,
                    correlation_id=correlation_id,
                )
            return payment
        return self._apply_provider_payment(
            payment,
            provider_payment,
            correlation_id=correlation_id,
        )

    def _apply_provider_payment(
        self,
        payment: PaymentAttempt,
        provider_payment: ProviderPayment,
        *,
        correlation_id: UUID | None = None,
    ) -> PaymentAttempt:
        if provider_payment.external_reference != str(payment.payment_id):
            raise FinanceConflictError("Referência externa do provedor é incompatível")
        if provider_payment.amount != payment.amount:
            raise FinanceConflictError("Valor autoritativo do provedor é incompatível")
        if provider_payment.currency != self.payment_provider.currency:
            raise FinanceConflictError("Moeda autoritativa do provedor é incompatível")
        if provider_payment.live_mode:
            raise FinanceConflictError("Pagamento real não é aceito neste ambiente")
        if payment.provider != self.payment_provider.name:
            raise FinanceConflictError("Tentativa pertence a outro provedor")
        if payment.status == PaymentStatus.CONFIRMED:
            if payment.provider_reference != provider_payment.provider_reference:
                raise FinanceConflictError("Pagamento já confirmado com outra referência")
            return payment

        status = provider_payment.status.lower()
        if status == "approved":
            linked = self.repository.record_provider_payment(
                payment.payment_id,
                provider_reference=provider_payment.provider_reference,
                checkout_url=provider_payment.checkout_url,
                status=PaymentStatus.PROCESSING,
            )
            return self.repository.confirm_payment(
                linked.payment_id,
                provider_payment.provider_reference,
                correlation_id=correlation_id,
            )
        target = {
            "pending": PaymentStatus.PROCESSING,
            "in_process": PaymentStatus.PROCESSING,
            "authorized": PaymentStatus.PROCESSING,
            "rejected": PaymentStatus.REJECTED,
            "cancelled": (
                PaymentStatus.EXPIRED
                if provider_payment.status_detail == "expired"
                else PaymentStatus.FAILED
            ),
        }.get(status, PaymentStatus.UNKNOWN)
        return self.repository.record_provider_payment(
            payment.payment_id,
            provider_reference=provider_payment.provider_reference,
            checkout_url=provider_payment.checkout_url,
            status=target,
        )

    def get_financial_status(
        self, order_id: UUID
    ) -> tuple[Quote | None, PaymentAttempt | None]:
        return self.repository.get_financial_status(order_id)

    def _get_quote(self, quote_id: UUID) -> Quote:
        quote = self.repository.get_quote(quote_id)
        if quote is None:
            raise FinanceNotFoundError("Orçamento não encontrado")
        return quote
