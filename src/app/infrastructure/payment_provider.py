from uuid import UUID

from app.application.ports import PaymentConfirmation
from app.domain.exceptions import FinanceConflictError


class SimulatedPaymentProvider:
    """Deterministic adapter kept as the default for automated tests."""

    name = "simulated"
    currency = "BRL"

    def create_payment(self, payment, *, payer_email: str):
        del payment, payer_email
        return None

    def get_payment(self, provider_reference: str):
        del provider_reference
        raise FinanceConflictError("Consulta não suportada pelo simulador")

    def find_payment(self, external_reference: str):
        del external_reference
        return None

    def validate_webhook(self, *, signature: str, request_id: str, data_id: str):
        del signature, request_id, data_id
        raise FinanceConflictError("Webhook não suportado pelo simulador")

    def confirm_payment(
        self, payment_id: UUID, provider_reference: str
    ) -> PaymentConfirmation:
        del payment_id
        if not provider_reference.strip():
            raise FinanceConflictError("A referência externa é obrigatória")
        return PaymentConfirmation(provider_reference=provider_reference)
