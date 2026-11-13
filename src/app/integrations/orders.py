"""Adapter REST dos contextos autoritativos mantidos por Ordens."""

from decimal import Decimal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.application.ports import (
    OrdersContextPort,
    PaymentContext,
    QuoteContext,
    SnapshotLine,
)
from app.integrations.http import (
    DependencyAuthenticationError,
    DependencyAuthorizationError,
    DependencyContractError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ServiceTokenProvider,
    validate_correlation,
)


class OrdersContextNotFound(LookupError):
    pass


class OrdersContextConflict(RuntimeError):
    pass


class _LineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_id: UUID
    description: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class _ContactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1)
    telefone: str = Field(min_length=1)


class _QuoteContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: UUID
    order_version: int = Field(ge=1)
    status: str
    customer_contact: _ContactResponse
    services: tuple[_LineResponse, ...]
    items: tuple[_LineResponse, ...]


class _PaymentContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: UUID
    order_version: int = Field(ge=1)
    status: str
    approved_quote_id: UUID
    approved_quote_version: int = Field(ge=1)
    approved_total: Decimal = Field(gt=0)


class OrdersHTTPClient(OrdersContextPort):
    def __init__(
        self,
        *,
        base_url: str,
        service_name: str,
        token_provider: ServiceTokenProvider,
        timeout: httpx.Timeout,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_name = service_name
        self._token_provider = token_provider
        self._timeout = timeout
        self._transport = transport

    def get_quote_context(self, order_id: UUID, correlation_id: str) -> QuoteContext:
        response = self._get(
            path=f"/internal/v1/orders/{order_id}/quote-context",
            scope="orders:quote-context:read",
            correlation_id=correlation_id,
        )
        try:
            payload = _QuoteContextResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise DependencyContractError("Quote context incompatível") from exc
        if payload.order_id != order_id or payload.status != "DIAGNOSTICO_CONCLUIDO":
            raise DependencyContractError("Quote context divergente")
        return QuoteContext(
            order_id=payload.order_id,
            order_version=payload.order_version,
            status=payload.status,
            email=payload.customer_contact.email,
            telefone=payload.customer_contact.telefone,
            services=tuple(
                SnapshotLine(**line.model_dump()) for line in payload.services
            ),
            items=tuple(SnapshotLine(**line.model_dump()) for line in payload.items),
        )

    def get_payment_context(
        self,
        *,
        order_id: UUID,
        quote_id: UUID,
        quote_version: int,
        approved_total: Decimal,
        correlation_id: str,
    ) -> PaymentContext:
        response = self._get(
            path=f"/internal/v1/orders/{order_id}/payment-context",
            scope="orders:payment-context:read",
            correlation_id=correlation_id,
            params={
                "approved_quote_id": str(quote_id),
                "approved_quote_version": str(quote_version),
                "approved_total": format(approved_total.quantize(Decimal("0.01")), "f"),
            },
        )
        try:
            payload = _PaymentContextResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise DependencyContractError("Payment context incompatível") from exc
        if (
            payload.order_id != order_id
            or payload.status != "FINALIZADA"
            or payload.approved_quote_id != quote_id
            or payload.approved_quote_version != quote_version
            or payload.approved_total != approved_total
        ):
            raise DependencyContractError("Payment context divergente")
        return PaymentContext(**payload.model_dump())

    def _get(
        self,
        *,
        path: str,
        scope: str,
        correlation_id: str,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = self._token_provider.get_token(
            audience=self._service_name,
            scopes={scope},
            correlation_id=correlation_id,
        )
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.get(
                    path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Correlation-ID": correlation_id,
                    },
                )
        except httpx.TimeoutException as exc:
            raise DependencyTimeoutError("Timeout ao consultar Ordens") from exc
        except httpx.RequestError as exc:
            raise DependencyUnavailableError("Ordens indisponível") from exc
        validate_correlation(response, correlation_id)
        if response.status_code == 404:
            raise OrdersContextNotFound("Ordem não encontrada")
        if response.status_code == 409:
            raise OrdersContextConflict("Estado da ordem incompatível")
        if response.status_code == 401:
            raise DependencyAuthenticationError("Token recusado por Ordens")
        if response.status_code == 403:
            raise DependencyAuthorizationError("Escopo recusado por Ordens")
        if response.status_code >= 500:
            raise DependencyUnavailableError("Ordens indisponível")
        if response.status_code != 200:
            raise DependencyContractError("Resposta inesperada de Ordens")
        return response
