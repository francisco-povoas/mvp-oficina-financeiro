from typing import Annotated

from fastapi import Depends, Request

from app.application.services import FinanceService


def get_finance_service(request: Request) -> FinanceService:
    return request.app.state.finance_service


ServiceDep = Annotated[FinanceService, Depends(get_finance_service)]
