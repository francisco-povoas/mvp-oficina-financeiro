from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from app.infrastructure.dynamodb import ensure_table
from app.main import create_app


def test_financial_http_flow_is_persisted_in_isolated_table() -> None:
    settings = Settings(
        environment="test",
        dynamodb_endpoint_url="http://dynamodb-test:8000",
        dynamodb_table=f"mvp-oficina-financeiro-api-{uuid4().hex}",
    )
    order_id = str(uuid4())
    line_id = str(uuid4())
    try:
        with TestClient(create_app(settings)) as client:
            created = client.post(
                "/api/v1/quotes",
                json={
                    "order_id": order_id,
                    "order_version": 2,
                    "services": [
                        {
                            "reference_id": line_id,
                            "description": "Revisão",
                            "quantity": 1,
                            "unit_price": "200.00",
                        }
                    ],
                    "customer_contact": {"email": "cliente@example.com"},
                },
            )
            assert created.status_code == 201
            quote_id = created.json()["quote_id"]

            assert client.post(f"/api/v1/quotes/{quote_id}/communicate").status_code == 200
            approved = client.post(f"/api/v1/quotes/{quote_id}/approve")
            assert approved.status_code == 200
            assert approved.json()["status"] == "APROVADO"

            payment_body = {"quote_id": quote_id, "method": "PIX", "amount": "200.00"}
            payment = client.post(
                "/api/v1/payments",
                headers={"Idempotency-Key": "api-payment-key"},
                json=payment_body,
            )
            repeated = client.post(
                "/api/v1/payments",
                headers={"Idempotency-Key": "api-payment-key"},
                json=payment_body,
            )
            assert payment.status_code == 201
            assert repeated.json()["payment_id"] == payment.json()["payment_id"]

            confirmed = client.post(
                f"/api/v1/payments/{payment.json()['payment_id']}/simulate-confirm",
                json={"provider_reference": "api-simulated-ref"},
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["status"] == "CONFIRMED"
    finally:
        ensure_table(settings).delete()
