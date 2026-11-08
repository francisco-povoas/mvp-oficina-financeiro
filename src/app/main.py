from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.internal.router import router as internal_router
from app.api.v1 import router as v1_router
from app.application.services import FinanceService
from app.config import Settings, get_settings
from app.http_contract import install_http_contract
from app.infrastructure.dynamodb import DynamoDBFinanceRepository, dynamodb_resource
from app.infrastructure.payment_provider import SimulatedPaymentProvider


def create_app(
    settings: Settings | None = None,
    finance_service: FinanceService | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_settings.validate_runtime()
    app = FastAPI(
        title="MVP Oficina — Financeiro",
        version=resolved_settings.service_version,
    )
    app.state.settings = resolved_settings
    if finance_service is None:
        table = dynamodb_resource(resolved_settings).Table(
            resolved_settings.dynamodb_table
        )
        finance_service = FinanceService(
            repository=DynamoDBFinanceRepository(table),
            payment_provider=SimulatedPaymentProvider(),
        )
    app.state.finance_service = finance_service
    install_http_contract(app)
    app.include_router(health_router)
    app.include_router(v1_router)
    app.include_router(internal_router)
    return app
