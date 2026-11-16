import hashlib
import hmac
import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.application.services import FinanceService
from app.config import Settings
from app.domain.exceptions import (
    InvalidWebhookSignatureError,
    PaymentProviderRejectedError,
    PaymentProviderTimeoutError,
)
from app.domain.models import (
    CustomerContact,
    PaymentAttempt,
    PaymentStatus,
    Quote,
    QuoteLine,
    QuoteStatus,
)
from app.infrastructure.mercado_pago import MercadoPagoPaymentProvider
from app.main import create_app


def settings(*, attempts: int = 2) -> Settings:
    return Settings(
        environment="test",
        dynamodb_table="unused",
        payment_provider="mercado_pago",
        mercado_pago_access_token="TEST-token",
        mercado_pago_webhook_secret="webhook-secret",
        mercado_pago_notification_url="https://example.test/webhook",
        mercado_pago_api_base_url="https://api.example.test",
        mercado_pago_max_attempts=attempts,
    )


def attempt() -> PaymentAttempt:
    return PaymentAttempt(
        payment_id=uuid4(),
        order_id=uuid4(),
        quote_id=uuid4(),
        quote_version=3,
        attempt_number=1,
        status=PaymentStatus.PENDING,
        method="PIX",
        amount=Decimal("150.00"),
        provider="mercado_pago",
    )


def payment_response(payment: PaymentAttempt, *, status: str = "pending") -> dict:
    return {
        "id": 987654321,
        "external_reference": str(payment.payment_id),
        "status": status,
        "status_detail": "pending_waiting_transfer",
        "transaction_amount": 150.0,
        "currency_id": "BRL",
        "live_mode": False,
        "point_of_interaction": {
            "transaction_data": {"ticket_url": "https://sandbox.test/pix/987654321"}
        },
    }


def test_creates_pix_charge_with_idempotency_and_external_reference() -> None:
    payment = attempt()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/payments"
        assert request.headers["X-Idempotency-Key"] == str(payment.payment_id)
        assert request.headers["Authorization"] == "Bearer TEST-token"
        body = json.loads(request.content)
        assert body["payment_method_id"] == "pix"
        assert body["external_reference"] == str(payment.payment_id)
        assert body["notification_url"] == "https://example.test/webhook"
        assert body["payer"]["email"] == "cliente@example.com"
        return httpx.Response(201, json=payment_response(payment))

    provider = MercadoPagoPaymentProvider(
        settings(), transport=httpx.MockTransport(handler)
    )
    created = provider.create_payment(payment, payer_email="cliente@example.com")

    assert created.provider_reference == "987654321"
    assert created.external_reference == str(payment.payment_id)
    assert created.checkout_url == "https://sandbox.test/pix/987654321"
    assert created.live_mode is False


def test_retries_transient_failure_and_queries_authoritative_payment() -> None:
    payment = attempt()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"message": "temporary"})
        assert request.url.path == "/v1/payments/987654321"
        return httpx.Response(200, json=payment_response(payment, status="approved"))

    provider = MercadoPagoPaymentProvider(
        settings(), transport=httpx.MockTransport(handler)
    )

    result = provider.get_payment("987654321")

    assert calls == 2
    assert result.status == "approved"


def test_searches_payment_by_external_reference_for_reconciliation() -> None:
    payment = attempt()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/payments/search"
        assert request.url.params["external_reference"] == str(payment.payment_id)
        results = [payment_response(payment)] if calls == 1 else []
        return httpx.Response(200, json={"results": results})

    provider = MercadoPagoPaymentProvider(
        settings(), transport=httpx.MockTransport(handler)
    )

    assert provider.find_payment(str(payment.payment_id)) is not None
    assert provider.find_payment(str(payment.payment_id)) is None


def test_rejects_payment_method_outside_the_stage_scope() -> None:
    payment = replace(attempt(), method="CREDIT_CARD")
    provider = MercadoPagoPaymentProvider(
        settings(),
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    with pytest.raises(PaymentProviderRejectedError, match="somente para PIX"):
        provider.create_payment(payment, payer_email="cliente@example.com")


def test_timeout_is_unknown_after_bounded_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    provider = MercadoPagoPaymentProvider(
        settings(), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(PaymentProviderTimeoutError):
        provider.get_payment("987654321")
    assert calls == 2


def test_webhook_signature_is_validated_in_constant_time() -> None:
    provider = MercadoPagoPaymentProvider(
        settings(),
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    data_id = "987654321"
    request_id = "request-123"
    timestamp = "1704908010"
    manifest = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
    digest = hmac.new(
        b"webhook-secret", manifest.encode(), hashlib.sha256
    ).hexdigest()

    provider.validate_webhook(
        signature=f"ts={timestamp},v1={digest}",
        request_id=request_id,
        data_id=data_id,
    )

    with pytest.raises(InvalidWebhookSignatureError):
        provider.validate_webhook(
            signature=f"ts={timestamp},v1={'0' * 64}",
            request_id=request_id,
            data_id=data_id,
        )
    with pytest.raises(InvalidWebhookSignatureError, match="incompleta"):
        provider.validate_webhook(
            signature="",
            request_id=request_id,
            data_id=data_id,
        )


def test_timeout_marks_attempt_unknown_without_confirmation() -> None:
    quote = Quote(
        quote_id=uuid4(),
        order_id=uuid4(),
        order_version=1,
        order_status="DIAGNOSTICO_CONCLUIDO",
        services=(
            QuoteLine(
                reference_id=uuid4(),
                description="Revisão",
                quantity=1,
                unit_price=Decimal("150.00"),
            ),
        ),
        items=(),
        customer_contact=CustomerContact(
            email="cliente@example.com",
            telefone="5511999999999",
        ),
        status=QuoteStatus.APROVADO,
    )
    payment = PaymentAttempt(
        payment_id=uuid4(),
        order_id=quote.order_id,
        quote_id=quote.quote_id,
        quote_version=quote.version,
        attempt_number=1,
        status=PaymentStatus.PENDING,
        method="PIX",
        amount=quote.total,
        provider="mercado_pago",
    )
    repository = Mock()
    repository.get_quote.return_value = quote
    repository.create_payment.return_value = payment
    repository.transition_payment_status.return_value = replace(
        payment, status=PaymentStatus.UNKNOWN
    )
    provider = Mock(name="mercado_pago")
    provider.name = "mercado_pago"
    provider.create_payment.side_effect = PaymentProviderTimeoutError("timeout")
    service = FinanceService(repository, provider)

    with pytest.raises(PaymentProviderTimeoutError):
        service.create_payment(
            quote_id=quote.quote_id,
            method="PIX",
            amount=quote.total,
            idempotency_key="timeout-attempt",
        )

    repository.transition_payment_status.assert_called_once_with(
        payment.payment_id, PaymentStatus.UNKNOWN
    )
    repository.confirm_payment.assert_not_called()


def test_webhook_http_endpoint_accepts_valid_and_rejects_invalid_callback() -> None:
    payment = attempt()
    confirmed = replace(
        payment,
        status=PaymentStatus.CONFIRMED,
        provider_reference="987654321",
    )
    service = Mock()
    service.process_provider_webhook.return_value = confirmed
    app = create_app(
        Settings(environment="test", dynamodb_table="unused"),
        finance_service=service,
        orders_context=Mock(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/payments/mercado-pago/webhook",
            params={"data.id": "987654321", "type": "payment"},
            headers={"x-signature": "valid", "x-request-id": "request-id"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "CONFIRMED"

        ignored = client.post(
            "/api/v1/payments/mercado-pago/webhook",
            params={"data.id": "plan-id", "type": "plan"},
            headers={"x-signature": "unused", "x-request-id": "request-id"},
        )
        assert ignored.json() == {"accepted": True, "ignored": True}

        service.process_provider_webhook.side_effect = InvalidWebhookSignatureError(
            "inválida"
        )
        invalid = client.post(
            "/api/v1/payments/mercado-pago/webhook",
            params={"data.id": "987654321", "type": "payment"},
            headers={"x-signature": "invalid", "x-request-id": "request-id"},
        )
        assert invalid.status_code == 401
