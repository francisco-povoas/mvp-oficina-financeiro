import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from app.integrations.http import (
    DependencyAuthorizationError,
    DependencyContractError,
    DependencyTimeoutError,
    ServiceClientConfig,
    ServiceTokenProvider,
)
from app.integrations.orders import (
    OrdersContextConflict,
    OrdersContextNotFound,
    OrdersHTTPClient,
)


def _client(handler) -> OrdersHTTPClient:
    transport = httpx.MockTransport(handler)
    config = ServiceClientConfig(
        token_url="http://ordens/internal/v1/auth/token",
        client_id="mvp-oficina-financeiro",
        client_secret="credencial-externa",
        connect_timeout_seconds=0.2,
        read_timeout_seconds=0.3,
    )
    return OrdersHTTPClient(
        base_url="http://ordens",
        service_name="mvp-oficina-ordens",
        token_provider=ServiceTokenProvider(config, transport=transport),
        timeout=config.timeout,
        transport=transport,
    )


def test_quote_and_payment_context_use_minimal_scopes_and_correlation() -> None:
    order_id = uuid4()
    quote_id = uuid4()
    line_id = uuid4()
    correlation_id = str(uuid4())
    token_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            body = json.loads(request.content)
            token_requests.append(body)
            return httpx.Response(
                200,
                headers={"X-Correlation-ID": correlation_id},
                json={
                    "access_token": f"token-{len(token_requests)}",
                    "token_type": "bearer",
                    "expires_in": 300,
                    "scope": body["scope"],
                },
            )
        assert request.headers["X-Correlation-ID"] == correlation_id
        if request.url.path.endswith("/quote-context"):
            return httpx.Response(
                200,
                headers={"X-Correlation-ID": correlation_id},
                json={
                    "order_id": str(order_id),
                    "order_version": 4,
                    "status": "DIAGNOSTICO_CONCLUIDO",
                    "customer_contact": {
                        "email": "cliente@example.com",
                        "telefone": "5511999999999",
                    },
                    "services": [
                        {
                            "reference_id": str(line_id),
                            "description": "Revisão",
                            "quantity": 1,
                            "unit_price": "200.00",
                        }
                    ],
                    "items": [],
                },
            )
        assert request.url.params["approved_quote_id"] == str(quote_id)
        return httpx.Response(
            200,
            headers={"X-Correlation-ID": correlation_id},
            json={
                "order_id": str(order_id),
                "order_version": 9,
                "status": "FINALIZADA",
                "approved_quote_id": str(quote_id),
                "approved_quote_version": 3,
                "approved_total": "200.00",
            },
        )

    client = _client(handler)
    quote = client.get_quote_context(order_id, correlation_id)
    payment = client.get_payment_context(
        order_id=order_id,
        quote_id=quote_id,
        quote_version=3,
        approved_total=Decimal("200.00"),
        correlation_id=correlation_id,
    )

    assert quote.services[0].unit_price == Decimal("200.00")
    assert payment.status == "FINALIZADA"
    assert [item["scope"] for item in token_requests] == [
        "orders:quote-context:read",
        "orders:payment-context:read",
    ]
    assert all(item["audience"] == "mvp-oficina-ordens" for item in token_requests)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, OrdersContextNotFound),
        (409, OrdersContextConflict),
        (403, DependencyAuthorizationError),
    ],
)
def test_orders_client_classifies_http_failures(status, error) -> None:
    correlation_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200,
                headers={"X-Correlation-ID": correlation_id},
                json={
                    "access_token": "token",
                    "token_type": "bearer",
                    "expires_in": 300,
                    "scope": "orders:quote-context:read",
                },
            )
        return httpx.Response(
            status,
            headers={"X-Correlation-ID": correlation_id},
            json={"code": "ERROR"},
        )

    with pytest.raises(error):
        _client(handler).get_quote_context(uuid4(), correlation_id)


def test_orders_client_rejects_timeout_and_incompatible_response() -> None:
    correlation_id = str(uuid4())

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200,
                headers={"X-Correlation-ID": correlation_id},
                json={
                    "access_token": "token",
                    "token_type": "bearer",
                    "expires_in": 300,
                    "scope": "orders:quote-context:read",
                },
            )
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(DependencyTimeoutError):
        _client(timeout_handler).get_quote_context(uuid4(), correlation_id)

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200,
                headers={"X-Correlation-ID": correlation_id},
                json={
                    "access_token": "token",
                    "token_type": "bearer",
                    "expires_in": 300,
                    "scope": "orders:quote-context:read",
                },
            )
        return httpx.Response(
            200,
            headers={"X-Correlation-ID": correlation_id},
            json={"order_id": str(uuid4())},
        )

    with pytest.raises(DependencyContractError):
        _client(invalid_handler).get_quote_context(uuid4(), correlation_id)
