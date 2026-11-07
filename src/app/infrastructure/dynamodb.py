from __future__ import annotations

import hashlib
import json
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
    PaymentAttempt,
    PaymentStatus,
    Quote,
    QuoteLine,
    QuoteStatus,
)


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
    table.wait_until_exists()
    return table


def _line_to_item(line: QuoteLine) -> dict[str, Any]:
    return {
        "reference_id": str(line.reference_id),
        "description": line.description,
        "quantity": line.quantity,
        "unit_price": line.unit_price,
    }


def _line_from_item(item: dict[str, Any]) -> QuoteLine:
    return QuoteLine(
        reference_id=UUID(item["reference_id"]),
        description=item["description"],
        quantity=int(item["quantity"]),
        unit_price=Decimal(item["unit_price"]),
    )


def quote_to_item(quote: Quote) -> dict[str, Any]:
    return {
        "PK": f"QUOTE#{quote.quote_id}",
        "SK": "META",
        "entity_type": "QUOTE",
        "quote_id": str(quote.quote_id),
        "order_id": str(quote.order_id),
        "order_version": quote.order_version,
        "status": quote.status.value,
        "version": quote.version,
        "services": [_line_to_item(line) for line in quote.services],
        "items": [_line_to_item(line) for line in quote.items],
        "customer_contact": quote.customer_contact,
        "total": quote.total,
        "rejection_reason": quote.rejection_reason or "",
        "created_at": quote.created_at.isoformat(),
        "updated_at": quote.updated_at.isoformat(),
        "GSI1PK": f"ORDER#{quote.order_id}",
        "GSI1SK": f"QUOTE#{quote.created_at.isoformat()}#{quote.quote_id}",
    }


def quote_from_item(item: dict[str, Any]) -> Quote:
    return Quote(
        quote_id=UUID(item["quote_id"]),
        order_id=UUID(item["order_id"]),
        order_version=int(item["order_version"]),
        services=tuple(_line_from_item(line) for line in item.get("services", [])),
        items=tuple(_line_from_item(line) for line in item.get("items", [])),
        customer_contact=dict(item.get("customer_contact", {})),
        status=QuoteStatus(item["status"]),
        version=int(item["version"]),
        rejection_reason=item.get("rejection_reason") or None,
        created_at=__import__("datetime").datetime.fromisoformat(item["created_at"]),
        updated_at=__import__("datetime").datetime.fromisoformat(item["updated_at"]),
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
    dt = __import__("datetime").datetime
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
        created_at=dt.fromisoformat(item["created_at"]),
        updated_at=dt.fromisoformat(item["updated_at"]),
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

    def save_quote_transition(self, previous: Quote, updated: Quote) -> Quote:
        try:
            response = self.table.update_item(
                Key={"PK": f"QUOTE#{previous.quote_id}", "SK": "META"},
                UpdateExpression=(
                    "SET #status = :new_status, #version = :new_version, "
                    "updated_at = :updated_at, rejection_reason = :reason"
                ),
                ConditionExpression="#status = :old_status AND #version = :old_version",
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues={
                    ":new_status": updated.status.value,
                    ":new_version": updated.version,
                    ":updated_at": updated.updated_at.isoformat(),
                    ":reason": updated.rejection_reason or "",
                    ":old_status": previous.status.value,
                    ":old_version": previous.version,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise FinanceConflictError("Orçamento foi alterado concorrentemente") from exc
            raise
        return quote_from_item(response["Attributes"])

    def get_payment(self, payment_id: UUID) -> PaymentAttempt | None:
        response = self.table.get_item(
            Key={"PK": f"PAYMENT#{payment_id}", "SK": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return payment_from_item(item) if item else None

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
            "amount": str(amount),
        }
        request_hash = hashlib.sha256(
            json.dumps(request_body, sort_keys=True).encode()
        ).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        idem_key = {
            "PK": f"IDEMPOTENCY#PAYMENT_CREATE#{order_id}#{key_hash}",
            "SK": "REQUEST",
        }
        existing = self.table.get_item(Key=idem_key, ConsistentRead=True).get("Item")
        if existing:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflictError("Chave reutilizada com payload diferente")
            payment = self.get_payment(UUID(existing["payment_id"]))
            if payment is None:
                raise FinanceConflictError("Registro idempotente sem pagamento correspondente")
            return payment

        guard_key = {"PK": f"ORDER#{order_id}", "SK": "PAYMENT_GUARD"}
        guard = self.table.get_item(Key=guard_key, ConsistentRead=True).get("Item")
        if guard:
            guard_status = PaymentStatus(guard["status"])
            if guard_status in ACTIVE_PAYMENT_STATUSES or guard_status == PaymentStatus.CONFIRMED:
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
            "created_at": payment.created_at.isoformat(),
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
                raise FinanceConflictError("Conflito concorrente ao criar pagamento") from exc
            raise
        return payment

    def confirm_payment(self, payment_id: UUID, provider_reference: str) -> PaymentAttempt:
        payment = self.get_payment(payment_id)
        if payment is None:
            raise FinanceNotFoundError("Pagamento não encontrado")
        if payment.status == PaymentStatus.CONFIRMED:
            return payment
        if payment.status not in ACTIVE_PAYMENT_STATUSES:
            raise FinanceConflictError("Pagamento não está ativo")
        now = __import__("datetime").datetime.now(__import__("datetime").UTC)
        lock = {
            "PK": f"PROVIDER_REF#{payment.provider}#{provider_reference}",
            "SK": "LOCK",
            "payment_id": str(payment_id),
        }
        values = {
            ":confirmed": PaymentStatus.CONFIRMED.value,
            ":old": payment.status.value,
            ":version": payment.version,
            ":new_version": payment.version + 1,
            ":reference": provider_reference,
            ":updated": now.isoformat(),
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": lock,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"PK": f"PAYMENT#{payment_id}", "SK": "META"},
                            "UpdateExpression": (
                                "SET #status=:confirmed, provider_reference=:reference, "
                                "#version=:new_version, updated_at=:updated"
                            ),
                            "ConditionExpression": "#status=:old AND #version=:version",
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#version": "version",
                            },
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {
                                "PK": f"ORDER#{payment.order_id}",
                                "SK": "PAYMENT_GUARD",
                            },
                            "UpdateExpression": "SET #status=:confirmed, #version=:new_version",
                            "ConditionExpression": "payment_id=:payment_id AND #status=:old",
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#version": "version",
                            },
                            "ExpressionAttributeValues": {
                                ":confirmed": PaymentStatus.CONFIRMED.value,
                                ":new_version": payment.version + 1,
                                ":payment_id": str(payment_id),
                                ":old": payment.status.value,
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise FinanceConflictError("Conflito ao confirmar pagamento") from exc
            raise
        confirmed = self.get_payment(payment_id)
        if confirmed is None:
            raise FinanceNotFoundError("Pagamento desapareceu após confirmação")
        return confirmed

    def list_order_history(self, order_id: UUID) -> list[dict[str, Any]]:
        response = self.table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"ORDER#{order_id}"),
        )
        return response.get("Items", [])
