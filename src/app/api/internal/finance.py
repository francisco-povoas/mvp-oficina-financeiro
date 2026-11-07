from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.security.auth import RequireServiceScopes

router = APIRouter()


@router.get(
    "/orders/{order_id}/financial-status",
    dependencies=[Depends(RequireServiceScopes("finance:status:read"))],
)
async def get_financial_status(order_id: UUID) -> None:
    raise HTTPException(
        status_code=501,
        detail={
            "code": "INTEGRATION_PLANNED",
            "message": "Consulta funcional planejada para a Etapa 2",
            "order_id": str(order_id),
        },
    )

