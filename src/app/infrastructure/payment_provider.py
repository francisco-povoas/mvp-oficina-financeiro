from uuid import UUID

from app.application.ports import PaymentConfirmation
from app.domain.exceptions import FinanceConflictError


class SimulatedPaymentProvider:
    """Deterministic adapter kept until the Mercado Pago stage."""

    def confirm_payment(
        self, payment_id: UUID, provider_reference: str
    ) -> PaymentConfirmation:
        del payment_id
        if not provider_reference.strip():
            raise FinanceConflictError("A referência externa é obrigatória")
        return PaymentConfirmation(provider_reference=provider_reference)
