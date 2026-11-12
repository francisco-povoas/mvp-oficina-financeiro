from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import PaymentStatus
from app.presentation.dependencies import ServiceDep
from app.security.auth import RequireServiceScopes

router = APIRouter()


class QuoteStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: UUID
    quote_version: int = Field(ge=1)
    status: str
    total: str = Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{2})$")
    communicated_at: datetime | None
    decided_at: datetime | None
    rejection_reason: str | None


class PaymentStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_id: UUID
    quote_id: UUID
    quote_version: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    payment_version: int = Field(ge=1)
    status: str
    amount: str = Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{2})$")
    confirmed_at: datetime | None


class FinancialStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: UUID
    quote: QuoteStatusResponse | None
    payment: PaymentStatusResponse | None
    reconciled_at: datetime


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


@router.get(
    "/orders/{order_id}/financial-status",
    response_model=FinancialStatusResponse,
    dependencies=[Depends(RequireServiceScopes("finance:status:read"))],
)
def get_financial_status(
    order_id: UUID,
    service: ServiceDep,
) -> FinancialStatusResponse:
    quote, payment = service.get_financial_status(order_id)
    if quote is None and payment is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "FINANCIAL_STATUS_NOT_FOUND",
                "message": "Contexto financeiro não encontrado",
            },
        )
    return FinancialStatusResponse(
        order_id=order_id,
        quote=(
            QuoteStatusResponse(
                quote_id=quote.quote_id,
                quote_version=quote.version,
                status=quote.status.value,
                total=_money(quote.total),
                communicated_at=quote.communicated_at,
                decided_at=(
                    quote.updated_at
                    if quote.status.value in {"APROVADO", "RECUSADO"}
                    else None
                ),
                rejection_reason=quote.rejection_reason,
            )
            if quote
            else None
        ),
        payment=(
            PaymentStatusResponse(
                payment_id=payment.payment_id,
                quote_id=payment.quote_id,
                quote_version=payment.quote_version,
                attempt_number=payment.attempt_number,
                payment_version=payment.version,
                status=payment.status.value,
                amount=_money(payment.amount),
                confirmed_at=(
                    payment.updated_at
                    if payment.status == PaymentStatus.CONFIRMED
                    else None
                ),
            )
            if payment
            else None
        ),
        reconciled_at=datetime.now(UTC),
    )

