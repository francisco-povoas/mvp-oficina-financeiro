from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal, InvalidOperation

import httpx

from app.application.ports import ProviderPayment
from app.config import Settings
from app.domain.exceptions import (
    InvalidWebhookSignatureError,
    PaymentProviderContractError,
    PaymentProviderRejectedError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
)
from app.domain.models import PaymentAttempt


class MercadoPagoWebhookValidator:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def validate(self, *, signature: str, request_id: str, data_id: str) -> None:
        parts = {}
        for raw_part in signature.split(","):
            key, separator, value = raw_part.strip().partition("=")
            if separator and key and value:
                parts[key] = value
        timestamp = parts.get("ts")
        received = parts.get("v1")
        if not timestamp or not received or not request_id or not data_id:
            raise InvalidWebhookSignatureError("Assinatura do webhook incompleta")
        manifest = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
        expected = hmac.new(
            self._secret.encode(), manifest.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise InvalidWebhookSignatureError("Assinatura do webhook inválida")


class MercadoPagoPaymentProvider:
    name = "mercado_pago"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._notification_url = settings.mercado_pago_notification_url
        self.currency = settings.mercado_pago_currency
        self._max_attempts = settings.mercado_pago_max_attempts
        self.webhooks = MercadoPagoWebhookValidator(
            settings.mercado_pago_webhook_secret
        )
        self._client = httpx.Client(
            base_url=settings.mercado_pago_api_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {settings.mercado_pago_access_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=settings.mercado_pago_connect_timeout_seconds,
                read=settings.mercado_pago_read_timeout_seconds,
                write=settings.mercado_pago_read_timeout_seconds,
                pool=settings.mercado_pago_connect_timeout_seconds,
            ),
            transport=transport,
        )

    def create_payment(
        self,
        payment: PaymentAttempt,
        *,
        payer_email: str,
    ) -> ProviderPayment:
        if payment.method.upper() != "PIX":
            raise PaymentProviderRejectedError("Mercado Pago está habilitado somente para PIX")
        response = self._request(
            "POST",
            "/v1/payments",
            headers={"X-Idempotency-Key": str(payment.payment_id)},
            json={
                "transaction_amount": float(f"{payment.amount:.2f}"),
                "description": f"Ordem de serviço {payment.order_id}",
                "payment_method_id": "pix",
                "payer": {"email": payer_email},
                "external_reference": str(payment.payment_id),
                "notification_url": self._notification_url,
            },
        )
        return self._parse_payment(response.json())

    def get_payment(self, provider_reference: str) -> ProviderPayment:
        response = self._request("GET", f"/v1/payments/{provider_reference}")
        return self._parse_payment(response.json())

    def find_payment(self, external_reference: str) -> ProviderPayment | None:
        response = self._request(
            "GET",
            "/v1/payments/search",
            params={
                "external_reference": external_reference,
                "sort": "date_created",
                "criteria": "desc",
            },
        )
        try:
            results = response.json()["results"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PaymentProviderContractError("Busca retornou contrato inválido") from exc
        if not results:
            return None
        return self._parse_payment(results[0])

    def confirm_payment(self, payment_id, provider_reference):
        del payment_id, provider_reference
        raise PaymentProviderRejectedError(
            "Mercado Pago só pode ser confirmado por consulta autoritativa"
        )

    def validate_webhook(
        self,
        *,
        signature: str,
        request_id: str,
        data_id: str,
    ) -> None:
        self.webhooks.validate(
            signature=signature,
            request_id=request_id,
            data_id=data_id,
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for _ in range(self._max_attempts):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = exc
                continue
            except httpx.TransportError as exc:
                last_error = exc
                continue
            if response.status_code >= 500 or response.status_code == 429:
                last_error = PaymentProviderUnavailableError(
                    f"Mercado Pago indisponível ({response.status_code})"
                )
                continue
            if response.status_code >= 400:
                raise PaymentProviderRejectedError(
                    f"Mercado Pago rejeitou a requisição ({response.status_code})"
                )
            return response
        if isinstance(last_error, httpx.TimeoutException):
            raise PaymentProviderTimeoutError("Mercado Pago excedeu o timeout") from last_error
        raise PaymentProviderUnavailableError("Mercado Pago indisponível") from last_error

    def _parse_payment(self, payload: object) -> ProviderPayment:
        if not isinstance(payload, dict):
            raise PaymentProviderContractError("Pagamento retornou contrato inválido")
        try:
            transaction_data = payload.get("point_of_interaction", {}).get(
                "transaction_data", {}
            )
            return ProviderPayment(
                provider_reference=str(payload["id"]),
                external_reference=str(payload["external_reference"]),
                status=str(payload["status"]),
                status_detail=str(payload.get("status_detail", "")),
                amount=Decimal(str(payload["transaction_amount"])),
                currency=str(payload["currency_id"]),
                live_mode=bool(payload["live_mode"]),
                checkout_url=transaction_data.get("ticket_url"),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise PaymentProviderContractError("Pagamento retornou contrato inválido") from exc
