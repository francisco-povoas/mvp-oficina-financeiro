from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.infrastructure.dynamodb import ensure_table
from app.main import create_app
from app.security.jwt import JWTProvider

pytestmark = pytest.mark.integration


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
    finally:
        ensure_table(settings).delete()


def test_quote_and_communication_http_invariants() -> None:
    settings = Settings(
        environment="test",
        dynamodb_endpoint_url="http://dynamodb-test:8000",
        dynamodb_table=f"mvp-oficina-financeiro-api-rules-{uuid4().hex}",
    )
    try:
        with TestClient(create_app(settings)) as client:
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

            no_service = quote_body()
            no_service["services"] = []
            assert client.post("/api/v1/quotes", json=no_service).status_code == 409

            inactive_service = quote_body()
            inactive_service["services"][0]["active"] = False
            assert client.post("/api/v1/quotes", json=inactive_service).status_code == 409

            missing_phone = quote_body()
            missing_phone["customer_contact"].pop("telefone")
            assert client.post("/api/v1/quotes", json=missing_phone).status_code == 422

            invalid_state = quote_body()
            invalid_state["order_status"] = "RECEBIDA"
            assert client.post("/api/v1/quotes", json=invalid_state).status_code == 409

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
        ensure_table(settings).delete()
