from fastapi import APIRouter

from app.api.internal import finance

router = APIRouter(prefix="/internal/v1", include_in_schema=True)
router.include_router(finance.router, tags=["Contratos internos do Financeiro"])

