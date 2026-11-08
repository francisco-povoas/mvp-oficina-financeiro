from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.application.services import FinanceService
from app.domain.exceptions import FinanceConflictError, FinanceNotFoundError
from app.domain.models import (
    CustomerContact,
    PaymentAttempt,
    Quote,
    QuoteCommunicationChannel,
    QuoteCommunicationEvidence,
    QuoteLine,
)
from app.infrastructure.dynamodb import FinanceRepository, ensure_table
from app.security.auth import require_user

router = APIRouter(
    prefix="/api/v1",
    tags=["Financeiro"],
    dependencies=[Depends(require_user)],
)


class QuoteLineRequest(BaseModel):
    reference_id: UUID
    description: str
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    active: bool = True


class CustomerContactRequest(BaseModel):
    email: str = Field(min_length=1)
    telefone: str = Field(min_length=1)


class CreateQuoteRequest(BaseModel):
    order_id: UUID
    order_version: int = Field(gt=0)
    order_status: str
    services: list[QuoteLineRequest] = Field(default_factory=list)
    items: list[QuoteLineRequest] = Field(default_factory=list)
    customer_contact: CustomerContactRequest


class CommunicationEvidenceRequest(BaseModel):
    evidence_id: UUID
    channel: QuoteCommunicationChannel
    destination: str = Field(min_length=1)
    success: bool
    message: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    external_reference: str | None = None
    sent_at: datetime


class CommunicateQuoteRequest(BaseModel):
    communication_id: UUID
    evidences: list[CommunicationEvidenceRequest] = Field(min_length=2, max_length=2)


class RejectQuoteRequest(BaseModel):
    reason: str | None = None


class CreatePaymentRequest(BaseModel):
    quote_id: UUID
    method: str
    amount: Decimal = Field(gt=0)


class ConfirmPaymentRequest(BaseModel):
    provider_reference: str


def _service(request: Request) -> FinanceService:
    table = ensure_table(request.app.state.settings)
    return FinanceService(FinanceRepository(table))


ServiceDep = Annotated[FinanceService, Depends(_service)]


def _line(value: QuoteLineRequest) -> QuoteLine:
    return QuoteLine(**value.model_dump())


def _quote_response(quote: Quote) -> dict:
    return {
        "quote_id": quote.quote_id,
        "order_id": quote.order_id,
        "order_version": quote.order_version,
        "order_status": quote.order_status,
        "status": quote.status.value,
        "version": quote.version,
        "services": [
            {
                "reference_id": line.reference_id,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "active": line.active,
            }
            for line in quote.services
        ],
        "items": [
            {
                "reference_id": line.reference_id,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "active": line.active,
            }
            for line in quote.items
        ],
        "customer_contact": {
            "email": quote.customer_contact.email,
            "telefone": quote.customer_contact.telefone,
        },
        "total_services": quote.total_services,
        "total_items": quote.total_items,
        "total": quote.total,
        "communicated_at": quote.communicated_at,
        "rejection_reason": quote.rejection_reason,
        "created_at": quote.created_at,
        "updated_at": quote.updated_at,
    }


def _communication_response(evidence: QuoteCommunicationEvidence) -> dict:
    return {
        "evidence_id": evidence.evidence_id,
        "communication_id": evidence.communication_id,
        "quote_id": evidence.quote_id,
        "order_id": evidence.order_id,
        "channel": evidence.channel.value,
        "destination": evidence.destination,
        "success": evidence.success,
        "message": evidence.message,
        "provider": evidence.provider,
        "external_reference": evidence.external_reference,
        "sent_at": evidence.sent_at,
        "recorded_at": evidence.recorded_at,
    }


def _payment_response(payment: PaymentAttempt) -> dict:
    return {
        "payment_id": payment.payment_id,
        "order_id": payment.order_id,
        "quote_id": payment.quote_id,
        "quote_version": payment.quote_version,
        "attempt_number": payment.attempt_number,
        "status": payment.status.value,
        "method": payment.method,
        "amount": payment.amount,
        "provider": payment.provider,
        "provider_reference": payment.provider_reference,
        "version": payment.version,
    }


@router.post("/quotes", status_code=201)
def create_quote(body: CreateQuoteRequest, service: ServiceDep) -> dict:
    try:
        quote = service.create_quote(
            order_id=body.order_id,
            order_version=body.order_version,
            order_status=body.order_status,
            services=tuple(_line(line) for line in body.services),
            items=tuple(_line(line) for line in body.items),
            customer_contact=CustomerContact(**body.customer_contact.model_dump()),
        )
        return _quote_response(quote)
    except (ValueError, FinanceConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/quotes/{quote_id}")
def get_quote(quote_id: UUID, service: ServiceDep) -> dict:
    return _transition(lambda: service.get_quote(quote_id))


@router.post("/quotes/{quote_id}/communicate")
def communicate_quote(
    quote_id: UUID,
    body: CommunicateQuoteRequest,
    service: ServiceDep,
) -> dict:
    def operation() -> Quote:
        quote = service.get_quote(quote_id)
        evidences = tuple(
            QuoteCommunicationEvidence(
                **value.model_dump(),
                communication_id=body.communication_id,
                quote_id=quote.quote_id,
                order_id=quote.order_id,
            )
            for value in body.evidences
        )
        return service.communicate_quote(quote_id, evidences)

    return _transition(operation)


@router.get("/quotes/{quote_id}/communications")
def list_quote_communications(quote_id: UUID, service: ServiceDep) -> list[dict]:
    try:
        return [
            _communication_response(evidence)
            for evidence in service.list_quote_communications(quote_id)
        ]
    except FinanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/quotes/{quote_id}/approve")
def approve_quote(quote_id: UUID, service: ServiceDep) -> dict:
    return _transition(lambda: service.approve_quote(quote_id))


@router.post("/quotes/{quote_id}/reject")
def reject_quote(quote_id: UUID, body: RejectQuoteRequest, service: ServiceDep) -> dict:
    return _transition(lambda: service.reject_quote(quote_id, body.reason))


def _transition(operation) -> dict:
    try:
        return _quote_response(operation())
    except FinanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, FinanceConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/payments", status_code=201)
def create_payment(
    body: CreatePaymentRequest,
    service: ServiceDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    try:
        return _payment_response(
            service.create_payment(
                quote_id=body.quote_id,
                method=body.method,
                amount=body.amount,
                idempotency_key=idempotency_key,
            )
        )
    except FinanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FinanceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/payments/{payment_id}/simulate-confirm")
def confirm_payment(
    payment_id: UUID, body: ConfirmPaymentRequest, service: ServiceDep
) -> dict:
    try:
        return _payment_response(
            service.confirm_simulated_payment(payment_id, body.provider_reference)
        )
    except FinanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FinanceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
