import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.ports import (
    PaymentContext,
    QuoteContext,
    SnapshotLine,
)
from app.config import Settings
from app.infrastructure.dynamodb import ensure_table
from app.main import create_app
from app.security.jwt import JWTProvider

pytestmark = pytest.mark.integration


class FakeOrdersContext:
    def __init__(self, line_id: str, price: str) -> None:
        self.line_id = line_id
        self.price = price

    def get_quote_context(self, order_id, correlation_id) -> QuoteContext:
        return QuoteContext(
            order_id=order_id,
            order_version=2,
            status="DIAGNOSTICO_CONCLUIDO",
            email="cliente@example.com",
            telefone="5511999999999",
            services=(
                SnapshotLine(
                    reference_id=UUID(self.line_id),
                    description="Revisão",
                    quantity=1,
                    unit_price=Decimal(self.price),
                ),
            ),
            items=(),
        )

    def get_payment_context(
        self,
        *,
        order_id,
        quote_id,
        quote_version,
        approved_total,
        correlation_id,
    ) -> PaymentContext:
        return PaymentContext(
            order_id=order_id,
            order_version=9,
            status="FINALIZADA",
            approved_quote_id=quote_id,
            approved_quote_version=quote_version,
            approved_total=approved_total,
        )


def test_financial_http_flow_is_persisted_in_isolated_table() -> None:
    settings = Settings(
        environment="test",
        dynamodb_endpoint_url=os.environ["DYNAMODB_ENDPOINT_URL"],
        dynamodb_table=f"mvp-oficina-financeiro-api-{uuid4().hex}",
    )
    order_id = str(uuid4())
    line_id = str(uuid4())
    table = ensure_table(settings)
    try:
        with TestClient(
            create_app(
                settings,
                orders_context=FakeOrdersContext(line_id, "200.00"),
            )
        ) as client:
            token = JWTProvider(
                settings.jwt_secret_key,
                settings.jwt_access_token_expire_minutes,
                settings.jwt_algorithm,
                settings.jwt_issuer,
            ).create_user_token(
                subject=str(uuid4()),
                roles=["Atendente"],
                audience=settings.jwt_user_audience,
            )["access_token"]
            client.headers.update({"Authorization": f"Bearer {token}"})
            created = client.post(
                "/api/v1/quotes",
                json={
                    "order_id": order_id,
                    "order_version": 2,
                    "order_status": "DIAGNOSTICO_CONCLUIDO",
                    "services": [
                        {
                            "reference_id": line_id,
                            "description": "Revisão",
                            "quantity": 1,
                            "unit_price": "200.00",
                        }
                    ],
                    "customer_contact": {
                        "email": "cliente@example.com",
                        "telefone": "5511999999999",
                    },
                },
            )
            assert created.status_code == 201
            quote_id = created.json()["quote_id"]

            communication = {
                "communication_id": str(uuid4()),
                "evidences": [
                    {
                        "evidence_id": str(uuid4()),
                        "channel": "WHATSAPP",
                        "destination": "5511999999999",
                        "success": True,
                        "message": "WhatsApp entregue",
                        "provider": "evidence-import",
                        "external_reference": "wa-api-ref",
                        "sent_at": datetime.now(UTC).isoformat(),
                    },
                    {
                        "evidence_id": str(uuid4()),
                        "channel": "EMAIL",
                        "destination": "cliente@example.com",
                        "success": True,
                        "message": "E-mail entregue",
                        "provider": "evidence-import",
                        "external_reference": "email-api-ref",
                        "sent_at": datetime.now(UTC).isoformat(),
                    },
                ],
            }
            communicated = client.post(
                f"/api/v1/quotes/{quote_id}/communicate", json=communication
            )
            assert communicated.status_code == 200
            assert communicated.json()["status"] == "COMUNICADO"
            repeated_communication = client.post(
                f"/api/v1/quotes/{quote_id}/communicate", json=communication
            )
            assert repeated_communication.status_code == 200

            history = client.get(f"/api/v1/quotes/{quote_id}/communications")
            assert history.status_code == 200
            assert len(history.json()) == 2
            assert {item["channel"] for item in history.json()} == {"WHATSAPP", "EMAIL"}
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

            service_token = JWTProvider(
                settings.jwt_secret_key,
                settings.jwt_access_token_expire_minutes,
                settings.jwt_algorithm,
                settings.jwt_issuer,
            ).create_service_token(
                client_id="mvp-oficina-ordens",
                audience=settings.service_name,
                scopes={"finance:status:read"},
                expire_minutes=5,
            )["access_token"]
            correlation_id = str(uuid4())
            financial_status = client.get(
                f"/internal/v1/orders/{order_id}/financial-status",
                headers={
                    "Authorization": f"Bearer {service_token}",
                    "X-Correlation-ID": correlation_id,
                },
            )
            assert financial_status.status_code == 200
            assert financial_status.headers["X-Correlation-ID"] == correlation_id
            assert financial_status.json()["quote"]["status"] == "APROVADO"
            assert financial_status.json()["payment"]["status"] == "CONFIRMED"
            assert financial_status.json()["quote"]["communicated_at"] is not None
            assert financial_status.json()["quote"]["decided_at"] is not None
            assert financial_status.json()["quote"]["rejection_reason"] is None
            assert financial_status.json()["payment"]["quote_id"] == quote_id
            assert financial_status.json()["payment"]["quote_version"] == 3
            assert financial_status.json()["payment"]["attempt_number"] == 1
            assert financial_status.json()["payment"]["payment_version"] == 2
    finally:
        table.delete()


def test_quote_and_communication_http_invariants() -> None:
    settings = Settings(
        environment="test",
        dynamodb_endpoint_url=os.environ["DYNAMODB_ENDPOINT_URL"],
        dynamodb_table=f"mvp-oficina-financeiro-api-rules-{uuid4().hex}",
    )
    table = ensure_table(settings)
    try:
        line_id = str(uuid4())
        with TestClient(
            create_app(
                settings,
                orders_context=FakeOrdersContext(line_id, "150.00"),
            )
        ) as client:
            token = JWTProvider(
                settings.jwt_secret_key,
                settings.jwt_access_token_expire_minutes,
                settings.jwt_algorithm,
                settings.jwt_issuer,
            ).create_user_token(
                subject=str(uuid4()),
                roles=["Atendente"],
                audience=settings.jwt_user_audience,
            )["access_token"]
            client.headers.update({"Authorization": f"Bearer {token}"})

            def quote_body() -> dict:
                return {
                    "order_id": str(uuid4()),
                    "order_version": 3,
                    "order_status": "DIAGNOSTICO_CONCLUIDO",
                    "services": [
                        {
                            "reference_id": str(uuid4()),
                            "description": "Alinhamento",
                            "quantity": 1,
                            "unit_price": "150.00",
                            "active": True,
                        }
                    ],
                    "items": [
                        {
                            "reference_id": str(uuid4()),
                            "description": "Item cancelado",
                            "quantity": 2,
                            "unit_price": "999.00",
                            "active": False,
                        }
                    ],
                    "customer_contact": {
                        "email": "cliente@example.com",
                        "telefone": "5511999999999",
                    },
                }

            created = client.post("/api/v1/quotes", json=quote_body())
            assert created.status_code == 201
            assert created.json()["total_services"] == "150.00"
            assert created.json()["total_items"] == "0"
            assert created.json()["items"] == []
            quote_id = created.json()["quote_id"]

            communication_id = str(uuid4())
            sent_at = datetime.now(UTC).isoformat()
            attempts = [
                {
                    "evidence_id": str(uuid4()),
                    "channel": "WHATSAPP",
                    "destination": "5511999999999",
                    "success": False,
                    "message": "Falha confirmada pelo provedor",
                    "provider": "evidence-import",
                    "sent_at": sent_at,
                },
                {
                    "evidence_id": str(uuid4()),
                    "channel": "EMAIL",
                    "destination": "cliente@example.com",
                    "success": True,
                    "message": "E-mail entregue",
                    "provider": "evidence-import",
                    "sent_at": sent_at,
                },
            ]
            incomplete = client.post(
                f"/api/v1/quotes/{quote_id}/communicate",
                json={"communication_id": communication_id, "evidences": attempts[:1]},
            )
            assert incomplete.status_code == 422
            assert client.get(
                f"/api/v1/quotes/{quote_id}/communications"
            ).json() == []

            failed_round = client.post(
                f"/api/v1/quotes/{quote_id}/communicate",
                json={"communication_id": communication_id, "evidences": attempts},
            )
            assert failed_round.status_code == 200
            assert failed_round.json()["status"] == "RASCUNHO"
            assert client.post(f"/api/v1/quotes/{quote_id}/approve").status_code == 409

            for attempt in attempts:
                attempt["evidence_id"] = str(uuid4())
                attempt["success"] = True
                attempt["message"] = "Entrega confirmada"
            successful = client.post(
                f"/api/v1/quotes/{quote_id}/communicate",
                json={"communication_id": str(uuid4()), "evidences": attempts},
            )
            assert successful.status_code == 200
            assert successful.json()["status"] == "COMUNICADO"
            history = client.get(f"/api/v1/quotes/{quote_id}/communications").json()
            assert len(history) == 4

            new_round = client.post(
                f"/api/v1/quotes/{quote_id}/communicate",
                json={"communication_id": str(uuid4()), "evidences": attempts},
            )
            assert new_round.status_code == 409
    finally:
        table.delete()
