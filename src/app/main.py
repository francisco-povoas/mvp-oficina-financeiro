import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.internal.router import router as internal_router
from app.api.v1 import router as v1_router
from app.application.ports import OrdersContextPort
from app.application.services import FinanceService
from app.config import Settings, get_settings
from app.domain.exceptions import (
    PaymentProviderContractError,
    PaymentProviderRejectedError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
)
from app.http_contract import install_http_contract, problem_response
from app.infrastructure.dynamodb import DynamoDBFinanceRepository, dynamodb_resource
from app.infrastructure.mercado_pago import MercadoPagoPaymentProvider
from app.infrastructure.payment_provider import SimulatedPaymentProvider
from app.integrations.http import (
    DependencyAuthenticationError,
    DependencyAuthorizationError,
    DependencyContractError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ServiceClientConfig,
    ServiceTokenProvider,
)
from app.integrations.orders import (
    OrdersContextConflict,
    OrdersContextNotFound,
    OrdersHTTPClient,
)


def _dependency_handler(status_code: int, code: str, message: str):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return problem_response(
            request,
            status_code=status_code,
            code=code,
            message=message,
        )

    return handler


def create_app(
    settings: Settings | None = None,
    finance_service: FinanceService | None = None,
    orders_context: OrdersContextPort | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_settings.validate_runtime()
    app = FastAPI(
        title="MVP Oficina — Financeiro",
        version=resolved_settings.service_version,
    )
    app.state.settings = resolved_settings
    if orders_context is None:
        token_provider = ServiceTokenProvider(
            ServiceClientConfig(
                token_url=resolved_settings.internal_auth_url,
                client_id=resolved_settings.service_client_id,
                client_secret=resolved_settings.service_client_secret,
                connect_timeout_seconds=resolved_settings.internal_connect_timeout_seconds,
                read_timeout_seconds=resolved_settings.internal_read_timeout_seconds,
            )
        )
        orders_context = OrdersHTTPClient(
            base_url=resolved_settings.orders_base_url,
            service_name="mvp-oficina-ordens",
            token_provider=token_provider,
            timeout=httpx.Timeout(
                connect=resolved_settings.internal_connect_timeout_seconds,
                read=resolved_settings.internal_read_timeout_seconds,
                write=resolved_settings.internal_read_timeout_seconds,
                pool=resolved_settings.internal_connect_timeout_seconds,
            ),
        )
    if finance_service is None:
        table = dynamodb_resource(resolved_settings).Table(
            resolved_settings.dynamodb_table
        )
        payment_provider = (
            MercadoPagoPaymentProvider(resolved_settings)
            if resolved_settings.payment_provider == "mercado_pago"
            else SimulatedPaymentProvider()
        )
        finance_service = FinanceService(
            repository=DynamoDBFinanceRepository(table),
            payment_provider=payment_provider,
            orders_context=orders_context,
        )
    app.state.finance_service = finance_service
    install_http_contract(app)
    app.add_exception_handler(
        DependencyAuthenticationError,
        _dependency_handler(502, "DEPENDENCY_AUTHENTICATION_FAILED", "Autenticação interna falhou"),
    )
    app.add_exception_handler(
        DependencyAuthorizationError,
        _dependency_handler(502, "DEPENDENCY_AUTHORIZATION_FAILED", "Autorização interna falhou"),
    )
    app.add_exception_handler(
        DependencyContractError,
        _dependency_handler(502, "DEPENDENCY_CONTRACT_INVALID", "Resposta interna incompatível"),
    )
    app.add_exception_handler(
        DependencyTimeoutError,
        _dependency_handler(504, "DEPENDENCY_TIMEOUT", "Dependência interna excedeu o timeout"),
    )
    app.add_exception_handler(
        DependencyUnavailableError,
        _dependency_handler(503, "DEPENDENCY_UNAVAILABLE", "Dependência interna indisponível"),
    )
    app.add_exception_handler(
        OrdersContextNotFound,
        _dependency_handler(404, "ORDER_NOT_FOUND", "Ordem não encontrada"),
    )
    app.add_exception_handler(
        OrdersContextConflict,
        _dependency_handler(409, "ORDER_CONTEXT_CONFLICT", "Estado da ordem incompatível"),
    )
    app.add_exception_handler(
        PaymentProviderTimeoutError,
        _dependency_handler(504, "PAYMENT_PROVIDER_TIMEOUT", "Provedor excedeu o timeout"),
    )
    app.add_exception_handler(
        PaymentProviderUnavailableError,
        _dependency_handler(503, "PAYMENT_PROVIDER_UNAVAILABLE", "Provedor indisponível"),
    )
    app.add_exception_handler(
        PaymentProviderRejectedError,
        _dependency_handler(502, "PAYMENT_PROVIDER_REJECTED", "Provedor rejeitou a operação"),
    )
    app.add_exception_handler(
        PaymentProviderContractError,
        _dependency_handler(
            502,
            "PAYMENT_PROVIDER_CONTRACT_INVALID",
            "Resposta incompatível do provedor",
        ),
    )
    app.include_router(health_router)
    app.include_router(v1_router)
    app.include_router(internal_router)
    return app
