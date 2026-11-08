from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.domain.exceptions import (
    FinanceConflictError,
    FinanceNotFoundError,
    IdempotencyConflictError,
)
from app.domain.models import (
    ACTIVE_PAYMENT_STATUSES,
    RETRYABLE_PAYMENT_STATUSES,
    CustomerContact,
    OutboxRecord,
    PaymentAttempt,
    PaymentStatus,
    Quote,
    QuoteCommunicationChannel,
    QuoteCommunicationEvidence,
    QuoteLine,
    QuoteStatus,
)

ALLOWED_PAYMENT_TRANSITIONS = {
    PaymentStatus.PENDING: {
        PaymentStatus.PROCESSING,
        PaymentStatus.CONFIRMED,
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.REJECTED,
        PaymentStatus.UNKNOWN,
    },
    PaymentStatus.PROCESSING: {
        PaymentStatus.CONFIRMED,
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.REJECTED,
        PaymentStatus.UNKNOWN,
    },
    PaymentStatus.UNKNOWN: {
        PaymentStatus.CONFIRMED,
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.REJECTED,
    },
}


def dynamodb_resource(settings: Settings):
    return boto3.resource(
        "dynamodb",
        region_name=settings.aws_region,
        endpoint_url=settings.dynamodb_endpoint_url,
    )


def ensure_table(settings: Settings):
    """Cria a tabela e os dois GSIs de forma idempotente."""

    settings.validate_runtime()
    dynamodb = dynamodb_resource(settings)
    table = dynamodb.Table(settings.dynamodb_table)
    try:
        table.load()
        return table
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    try:
        table = dynamodb.create_table(
            TableName=settings.dynamodb_table,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
                {"AttributeName": "GSI2PK", "AttributeType": "S"},
                {"AttributeName": "GSI2SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2",
                    "KeySchema": [
                        {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
        table = dynamodb.Table(settings.dynamodb_table)
    table.wait_until_exists()
    return table


def _line_to_item(line: QuoteLine) -> dict[str, Any]:
    return {
        "reference_id": str(line.reference_id),
        "description": line.description,
        "quantity": line.quantity,
        "unit_price": line.unit_price,
        "active": line.active,
    }


def _line_from_item(item: dict[str, Any]) -> QuoteLine:
    return QuoteLine(
        reference_id=UUID(item["reference_id"]),
        description=item["description"],
        quantity=int(item["quantity"]),
        unit_price=Decimal(item["unit_price"]),
        active=bool(item.get("active", True)),
    )


def quote_to_item(quote: Quote) -> dict[str, Any]:
    return {
        "PK": f"QUOTE#{quote.quote_id}",
        "SK": "META",
        "entity_type": "QUOTE",
        "quote_id": str(quote.quote_id),
        "order_id": str(quote.order_id),
        "order_version": quote.order_version,
        "order_status": quote.order_status,
        "status": quote.status.value,
        "version": quote.version,
        "services": [_line_to_item(line) for line in quote.services],
        "items": [_line_to_item(line) for line in quote.items],
        "customer_contact": {
            "email": quote.customer_contact.email,
            "telefone": quote.customer_contact.telefone,
        },
        "total_services": quote.total_services,
        "total_items": quote.total_items,
        "total": quote.total,
        "rejection_reason": quote.rejection_reason or "",
        "communicated_at": (
            quote.communicated_at.isoformat() if quote.communicated_at else ""
        ),
        "created_at": quote.created_at.isoformat(),
        "updated_at": quote.updated_at.isoformat(),
        "GSI1PK": f"ORDER#{quote.order_id}",
        "GSI1SK": f"QUOTE#{quote.created_at.isoformat()}#{quote.quote_id}",
    }


def quote_from_item(item: dict[str, Any]) -> Quote:
    customer_contact = item.get("customer_contact", {})
    quote = Quote(
        quote_id=UUID(item["quote_id"]),
        order_id=UUID(item["order_id"]),
        order_version=int(item["order_version"]),
        order_status=item["order_status"],
        services=tuple(_line_from_item(line) for line in item.get("services", [])),
        items=tuple(_line_from_item(line) for line in item.get("items", [])),
        customer_contact=CustomerContact(
            email=customer_contact.get("email", ""),
            telefone=customer_contact.get("telefone", ""),
        ),
        status=QuoteStatus(item["status"]),
        version=int(item["version"]),
        rejection_reason=item.get("rejection_reason") or None,
        communicated_at=(
            datetime.fromisoformat(item["communicated_at"])
            if item.get("communicated_at")
            else None
        ),
        created_at=__import__("datetime").datetime.fromisoformat(item["created_at"]),
        updated_at=__import__("datetime").datetime.fromisoformat(item["updated_at"]),
    )
    expected_totals = {
        "total_services": quote.total_services,
        "total_items": quote.total_items,
        "total": quote.total,
    }
    for attribute, expected in expected_totals.items():
        if Decimal(item[attribute]) != expected:
            raise FinanceConflictError(
                f"Documento de orçamento possui {attribute} inconsistente"
            )
    return quote


def communication_to_item(evidence: QuoteCommunicationEvidence) -> dict[str, Any]:
    return {
        "PK": f"QUOTE#{evidence.quote_id}",
        "SK": f"COMMUNICATION#{evidence.communication_id}#{evidence.channel.value}",
        "entity_type": "QUOTE_COMMUNICATION",
        "evidence_id": str(evidence.evidence_id),
        "communication_id": str(evidence.communication_id),
        "quote_id": str(evidence.quote_id),
        "order_id": str(evidence.order_id),
        "channel": evidence.channel.value,
        "destination": evidence.destination,
        "success": evidence.success,
        "message": evidence.message,
        "provider": evidence.provider,
        "external_reference": evidence.external_reference or "",
        "sent_at": evidence.sent_at.isoformat(),
        "recorded_at": evidence.recorded_at.isoformat(),
        "GSI1PK": f"ORDER#{evidence.order_id}",
        "GSI1SK": (
            f"COMMUNICATION#{evidence.sent_at.isoformat()}#{evidence.evidence_id}"
        ),
    }


def _communication_key(evidence: QuoteCommunicationEvidence) -> dict[str, str]:
    return {
        "PK": f"QUOTE#{evidence.quote_id}",
        "SK": f"COMMUNICATION#{evidence.communication_id}#{evidence.channel.value}",
    }


def _communication_matches(
    stored: QuoteCommunicationEvidence,
    expected: QuoteCommunicationEvidence,
) -> bool:
    return replace(stored, recorded_at=expected.recorded_at) == expected


def communication_from_item(item: dict[str, Any]) -> QuoteCommunicationEvidence:
    return QuoteCommunicationEvidence(
        evidence_id=UUID(item["evidence_id"]),
        communication_id=UUID(item["communication_id"]),
        quote_id=UUID(item["quote_id"]),
        order_id=UUID(item["order_id"]),
        channel=QuoteCommunicationChannel(item["channel"]),
        destination=item["destination"],
        success=bool(item["success"]),
        message=item["message"],
        provider=item["provider"],
        external_reference=item.get("external_reference") or None,
        sent_at=datetime.fromisoformat(item["sent_at"]),
        recorded_at=datetime.fromisoformat(item["recorded_at"]),
    )


def payment_to_item(payment: PaymentAttempt) -> dict[str, Any]:
    return {
        "PK": f"PAYMENT#{payment.payment_id}",
        "SK": "META",
        "entity_type": "PAYMENT",
        "payment_id": str(payment.payment_id),
        "order_id": str(payment.order_id),
        "quote_id": str(payment.quote_id),
        "quote_version": payment.quote_version,
        "attempt_number": payment.attempt_number,
        "status": payment.status.value,
        "method": payment.method,
        "amount": payment.amount,
        "provider": payment.provider,
        "provider_reference": payment.provider_reference or "",
        "version": payment.version,
        "created_at": payment.created_at.isoformat(),
        "updated_at": payment.updated_at.isoformat(),
        "GSI1PK": f"ORDER#{payment.order_id}",
        "GSI1SK": f"PAYMENT#{payment.created_at.isoformat()}#{payment.payment_id}",
    }


def payment_from_item(item: dict[str, Any]) -> PaymentAttempt:
    return PaymentAttempt(
        payment_id=UUID(item["payment_id"]),
        order_id=UUID(item["order_id"]),
        quote_id=UUID(item["quote_id"]),
        quote_version=int(item["quote_version"]),
        attempt_number=int(item["attempt_number"]),
        status=PaymentStatus(item["status"]),
        method=item["method"],
        amount=Decimal(item["amount"]),
        provider=item["provider"],
        provider_reference=item.get("provider_reference") or None,
        version=int(item["version"]),
        created_at=datetime.fromisoformat(item["created_at"]),
        updated_at=datetime.fromisoformat(item["updated_at"]),
    )


def outbox_to_item(record: OutboxRecord) -> dict[str, Any]:
    return {
        "PK": f"OUTBOX#{record.message_id}",
        "SK": "META",
        "entity_type": "OUTBOX",
        "message_id": str(record.message_id),
        "event_type": record.event_type,
        "aggregate_id": str(record.aggregate_id),
        "payload": record.payload,
        "status": record.status,
        "attempt_count": record.attempt_count,
        "version": record.version,
        "next_attempt_at": record.next_attempt_at.isoformat(),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "GSI2PK": "OUTBOX#PENDING",
        "GSI2SK": f"{record.next_attempt_at.isoformat()}#{record.message_id}",
    }


def outbox_from_item(item: dict[str, Any]) -> OutboxRecord:
    return OutboxRecord(
        message_id=UUID(item["message_id"]),
        event_type=item["event_type"],
        aggregate_id=UUID(item["aggregate_id"]),
        payload=dict(item["payload"]),
        next_attempt_at=datetime.fromisoformat(item["next_attempt_at"]),
        status=item["status"],
        attempt_count=int(item["attempt_count"]),
        version=int(item["version"]),
        created_at=datetime.fromisoformat(item["created_at"]),
        updated_at=datetime.fromisoformat(item["updated_at"]),
    )


class FinanceRepository:
    def __init__(self, table) -> None:
        self.table = table
        self.client = table.meta.client

    def create_quote(self, quote: Quote) -> Quote:
        quote_item = quote_to_item(quote)
        guard = {
            "PK": f"ORDER#{quote.order_id}",
            "SK": "QUOTE_GUARD",
            "quote_id": str(quote.quote_id),
            "status": quote.status.value,
            "version": quote.version,
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": quote_item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": guard,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise FinanceConflictError("Já existe orçamento vigente para a ordem") from exc
            raise
        return quote

    def get_quote(self, quote_id: UUID) -> Quote | None:
        response = self.table.get_item(
            Key={"PK": f"QUOTE#{quote_id}", "SK": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return quote_from_item(item) if item else None

    def list_quote_communications(
        self, quote_id: UUID
    ) -> list[QuoteCommunicationEvidence]:
        items: list[dict[str, Any]] = []
        query: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("PK").eq(f"QUOTE#{quote_id}")
                & Key("SK").begins_with("COMMUNICATION#")
            ),
            "ConsistentRead": True,
        }
        while True:
            response = self.table.query(**query)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if last_key is None:
                break
            query["ExclusiveStartKey"] = last_key
        evidences = [communication_from_item(item) for item in items]
        return sorted(evidences, key=lambda evidence: (evidence.sent_at, evidence.evidence_id))

    def _existing_communication(
        self, evidences: tuple[QuoteCommunicationEvidence, ...]
    ) -> Quote | None:
        stored_items = [
            self.table.get_item(Key=_communication_key(evidence), ConsistentRead=True).get(
                "Item"
            )
            for evidence in evidences
        ]
        if not any(stored_items):
            return None
        if not all(stored_items):
            raise FinanceConflictError("Comunicação idempotente está incompleta")
        stored = [communication_from_item(item) for item in stored_items]
        if not all(
            _communication_matches(current, expected)
            for current, expected in zip(stored, evidences, strict=True)
        ):
            raise FinanceConflictError(
                "Identificador de comunicação reutilizado com evidência diferente"
            )
        quote = self.get_quote(evidences[0].quote_id)
        if quote is None:
            raise FinanceNotFoundError("Orçamento não encontrado")
        return quote

    def record_quote_communication(
        self,
        quote: Quote,
        evidences: tuple[QuoteCommunicationEvidence, ...],
    ) -> Quote:
        quote.validate_communication_evidences(evidences)
        existing = self._existing_communication(evidences)
        if existing is not None:
            return existing
        if quote.status != QuoteStatus.RASCUNHO:
            raise FinanceConflictError("Somente orçamento em rascunho pode ser comunicado")

        successful = all(evidence.success for evidence in evidences)
        updated = quote.comunicar(evidences) if successful else quote
        transaction: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": communication_to_item(evidence),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
            for evidence in evidences
        ]
        quote_condition = "#status = :old_status AND #version = :old_version"
        names = {"#status": "status", "#version": "version"}
        values = {
            ":old_status": quote.status.value,
            ":old_version": quote.version,
        }
        guard_key = {"PK": f"ORDER#{quote.order_id}", "SK": "QUOTE_GUARD"}
        if successful:
            transition_values = {
                **values,
                ":new_status": updated.status.value,
                ":new_version": updated.version,
            }
            transaction.extend(
                [
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"PK": f"QUOTE#{quote.quote_id}", "SK": "META"},
                            "UpdateExpression": (
                                "SET #status = :new_status, #version = :new_version, "
                                "updated_at = :updated_at, communicated_at = :communicated_at"
                            ),
                            "ConditionExpression": quote_condition,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": {
                                **transition_values,
                                ":updated_at": updated.updated_at.isoformat(),
                                ":communicated_at": updated.communicated_at.isoformat(),
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": guard_key,
                            "UpdateExpression": (
                                "SET #status = :new_status, #version = :new_version"
                            ),
                            "ConditionExpression": (
                                "quote_id = :quote_id AND " + quote_condition
                            ),
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": {
                                **transition_values,
                                ":quote_id": str(quote.quote_id),
                            },
                        }
                    },
                ]
            )
        else:
            transaction.extend(
                [
                    {
                        "ConditionCheck": {
                            "TableName": self.table.name,
                            "Key": {"PK": f"QUOTE#{quote.quote_id}", "SK": "META"},
                            "ConditionExpression": quote_condition,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": self.table.name,
                            "Key": guard_key,
                            "ConditionExpression": (
                                "quote_id = :quote_id AND " + quote_condition
                            ),
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": {
                                **values,
                                ":quote_id": str(quote.quote_id),
                            },
                        }
                    },
                ]
            )
        try:
            self.client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                concurrent = self._existing_communication(evidences)
                if concurrent is not None:
                    return concurrent
                raise FinanceConflictError(
                    "Conflito concorrente ao registrar comunicação"
                ) from exc
            raise
        stored = self.get_quote(quote.quote_id)
        if stored is None:
            raise FinanceNotFoundError("Orçamento desapareceu após comunicação")
        return stored

    def save_quote_transition(self, previous: Quote, updated: Quote) -> Quote:
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"PK": f"QUOTE#{previous.quote_id}", "SK": "META"},
                            "UpdateExpression": (
                                "SET #status = :new_status, #version = :new_version, "
                                "updated_at = :updated_at, rejection_reason = :reason, "
                                "communicated_at = :communicated_at"
                            ),
                            "ConditionExpression": (
                                "#status = :old_status AND #version = :old_version"
                            ),
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#version": "version",
                            },
                            "ExpressionAttributeValues": {
                                ":new_status": updated.status.value,
                                ":new_version": updated.version,
                                ":updated_at": updated.updated_at.isoformat(),
                                ":reason": updated.rejection_reason or "",
                                ":communicated_at": (
                                    updated.communicated_at.isoformat()
                                    if updated.communicated_at
                                    else ""
                                ),
                                ":old_status": previous.status.value,
                                ":old_version": previous.version,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {
                                "PK": f"ORDER#{previous.order_id}",
                                "SK": "QUOTE_GUARD",
                            },
                            "UpdateExpression": (
                                "SET #status = :new_status, #version = :new_version"
                            ),
                            "ConditionExpression": (
                                "quote_id = :quote_id AND #status = :old_status "
                                "AND #version = :old_version"
                            ),
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#version": "version",
                            },
                            "ExpressionAttributeValues": {
                                ":quote_id": str(previous.quote_id),
                                ":new_status": updated.status.value,
                                ":new_version": updated.version,
                                ":old_status": previous.status.value,
                                ":old_version": previous.version,
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise FinanceConflictError("Orçamento foi alterado concorrentemente") from exc
            raise
        stored = self.get_quote(previous.quote_id)
        if stored is None:
            raise FinanceNotFoundError("Orçamento desapareceu após transição")
        return stored

    def get_payment(self, payment_id: UUID) -> PaymentAttempt | None:
        response = self.table.get_item(
            Key={"PK": f"PAYMENT#{payment_id}", "SK": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return payment_from_item(item) if item else None

    def _get_idempotent_payment(
        self,
        *,
        idem_key: dict[str, str],
        request_hash: str,
    ) -> PaymentAttempt | None:
        existing = self.table.get_item(Key=idem_key, ConsistentRead=True).get("Item")
        if existing is None:
            return None
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflictError("Chave reutilizada com payload diferente")
        payment = self.get_payment(UUID(existing["payment_id"]))
        if payment is None:
            raise FinanceConflictError("Registro idempotente sem pagamento correspondente")
        return payment

    def create_payment(
        self,
        *,
        order_id: UUID,
        quote_id: UUID,
        quote_version: int,
        method: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> PaymentAttempt:
        request_body = {
            "order_id": str(order_id),
            "quote_id": str(quote_id),
            "quote_version": quote_version,
            "method": method,
            "amount": format(amount.normalize(), "f"),
        }
        request_hash = hashlib.sha256(
            json.dumps(request_body, sort_keys=True).encode()
        ).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        idem_key = {
            "PK": f"IDEMPOTENCY#PAYMENT_CREATE#{order_id}#{key_hash}",
            "SK": "REQUEST",
        }
        existing_payment = self._get_idempotent_payment(
            idem_key=idem_key,
            request_hash=request_hash,
        )
        if existing_payment is not None:
            return existing_payment

        guard_key = {"PK": f"ORDER#{order_id}", "SK": "PAYMENT_GUARD"}
        guard = self.table.get_item(Key=guard_key, ConsistentRead=True).get("Item")
        if guard:
            guard_status = PaymentStatus(guard["status"])
            if guard_status in ACTIVE_PAYMENT_STATUSES or guard_status == PaymentStatus.CONFIRMED:
                concurrent_payment = self._get_idempotent_payment(
                    idem_key=idem_key,
                    request_hash=request_hash,
                )
                if concurrent_payment is not None:
                    return concurrent_payment
                raise FinanceConflictError("A ordem já possui pagamento ativo ou confirmado")
            if guard_status not in RETRYABLE_PAYMENT_STATUSES:
                raise FinanceConflictError("O estado atual não permite nova tentativa")
            attempt_number = int(guard["attempt_number"]) + 1
            guard_version = int(guard["version"])
        else:
            attempt_number = 1
            guard_version = 0

        payment = PaymentAttempt(
            payment_id=uuid4(),
            order_id=order_id,
            quote_id=quote_id,
            quote_version=quote_version,
            attempt_number=attempt_number,
            status=PaymentStatus.PENDING,
            method=method,
            amount=amount,
        )
        idem_item = {
            **idem_key,
            "request_hash": request_hash,
            "payment_id": str(payment.payment_id),
            "response_status": payment.status.value,
            "response": {
                "payment_id": str(payment.payment_id),
                "attempt_number": attempt_number,
                "status": payment.status.value,
            },
            "created_at": payment.created_at.isoformat(),
            "updated_at": payment.updated_at.isoformat(),
        }
        guard_item = {
            **guard_key,
            "payment_id": str(payment.payment_id),
            "status": payment.status.value,
            "attempt_number": attempt_number,
            "version": guard_version + 1,
        }
        guard_write: dict[str, Any]
        if guard:
            guard_write = {
                "Put": {
                    "TableName": self.table.name,
                    "Item": guard_item,
                    "ConditionExpression": "#version = :version AND #status = :status",
                    "ExpressionAttributeNames": {"#version": "version", "#status": "status"},
                    "ExpressionAttributeValues": {
                        ":version": guard_version,
                        ":status": guard["status"],
                    },
                }
            }
        else:
            guard_write = {
                "Put": {
                    "TableName": self.table.name,
                    "Item": guard_item,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": idem_item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": payment_to_item(payment),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    guard_write,
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                concurrent_payment = self._get_idempotent_payment(
                    idem_key=idem_key,
                    request_hash=request_hash,
                )
                if concurrent_payment is not None:
                    return concurrent_payment
                raise FinanceConflictError("Conflito concorrente ao criar pagamento") from exc
            raise
        return payment

    def confirm_payment(self, payment_id: UUID, provider_reference: str) -> PaymentAttempt:
        if not provider_reference.strip():
            raise FinanceConflictError("A referência externa é obrigatória")
        return self.transition_payment_status(
            payment_id,
            PaymentStatus.CONFIRMED,
            provider_reference=provider_reference,
        )

    def transition_payment_status(
        self,
        payment_id: UUID,
        new_status: PaymentStatus,
        *,
        provider_reference: str | None = None,
    ) -> PaymentAttempt:
        payment = self.get_payment(payment_id)
        if payment is None:
            raise FinanceNotFoundError("Pagamento não encontrado")
        if payment.status == new_status:
            if (
                new_status == PaymentStatus.CONFIRMED
                and payment.provider_reference != provider_reference
            ):
                raise FinanceConflictError("Pagamento já confirmado com outra referência")
            return payment
        if new_status not in ALLOWED_PAYMENT_TRANSITIONS.get(payment.status, set()):
            raise FinanceConflictError("Transição de pagamento não permitida")
        if new_status == PaymentStatus.CONFIRMED and not provider_reference:
            raise FinanceConflictError("A referência externa é obrigatória")
        if new_status != PaymentStatus.CONFIRMED and provider_reference is not None:
            raise FinanceConflictError("Referência externa só é aceita na confirmação")

        guard_key = {"PK": f"ORDER#{payment.order_id}", "SK": "PAYMENT_GUARD"}
        guard = self.table.get_item(Key=guard_key, ConsistentRead=True).get("Item")
        if (
            guard is None
            or guard.get("payment_id") != str(payment_id)
            or guard.get("status") != payment.status.value
        ):
            raise FinanceConflictError("PAYMENT_GUARD inconsistente com a tentativa")
        guard_version = int(guard["version"])

        now = datetime.now(UTC)
        updated = replace(
            payment,
            status=new_status,
            provider_reference=provider_reference,
            version=payment.version + 1,
            updated_at=now,
        )
        attempt_values = {
            ":new_status": new_status.value,
            ":old": payment.status.value,
            ":version": payment.version,
            ":new_version": updated.version,
            ":reference": provider_reference or "",
            ":updated": now.isoformat(),
        }
        guard_values = {
            ":new_status": new_status.value,
            ":old": payment.status.value,
            ":version": guard_version,
            ":new_version": guard_version + 1,
            ":payment_id": str(payment_id),
        }
        transaction: list[dict[str, Any]] = []
        if new_status == PaymentStatus.CONFIRMED:
            transaction.append(
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            "PK": (
                                f"PROVIDER_REF#{payment.provider}#{provider_reference}"
                            ),
                            "SK": "LOCK",
                            "payment_id": str(payment_id),
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        transaction.extend(
            [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": {"PK": f"PAYMENT#{payment_id}", "SK": "META"},
                        "UpdateExpression": (
                            "SET #status=:new_status, provider_reference=:reference, "
                            "#version=:new_version, updated_at=:updated"
                        ),
                        "ConditionExpression": "#status=:old AND #version=:version",
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#version": "version",
                        },
                        "ExpressionAttributeValues": attempt_values,
                    }
                },
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": {
                            "PK": f"ORDER#{payment.order_id}",
                            "SK": "PAYMENT_GUARD",
                        },
                        "UpdateExpression": (
                            "SET #status=:new_status, #version=:new_version"
                        ),
                        "ConditionExpression": (
                            "payment_id=:payment_id AND #status=:old "
                            "AND #version=:version"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#version": "version",
                        },
                        "ExpressionAttributeValues": guard_values,
                    }
                },
            ]
        )
        try:
            self.client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                concurrent = self.get_payment(payment_id)
                if (
                    concurrent is not None
                    and concurrent.status == new_status
                    and concurrent.provider_reference == provider_reference
                ):
                    return concurrent
                raise FinanceConflictError("Conflito ao atualizar pagamento") from exc
            raise
        stored = self.get_payment(payment_id)
        if stored is None:
            raise FinanceNotFoundError("Pagamento desapareceu após atualização")
        return stored

    def save_outbox_record(self, record: OutboxRecord) -> OutboxRecord:
        try:
            self.table.put_item(
                Item=outbox_to_item(record),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise FinanceConflictError("Mensagem da Outbox já existe") from exc
            raise
        return record

    def get_outbox_record(self, message_id: UUID) -> OutboxRecord | None:
        response = self.table.get_item(
            Key={"PK": f"OUTBOX#{message_id}", "SK": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return outbox_from_item(item) if item else None

    def list_order_history(self, order_id: UUID) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        query: dict[str, Any] = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("GSI1PK").eq(f"ORDER#{order_id}"),
        }
        while True:
            response = self.table.query(**query)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if last_key is None:
                return items
            query["ExclusiveStartKey"] = last_key
