import ast
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.application.ports import PaymentConfirmation
from app.application.services import FinanceService
from app.domain.exceptions import FinanceConflictError
from app.domain.models import PaymentAttempt, PaymentStatus
from app.infrastructure.payment_provider import SimulatedPaymentProvider


def test_application_does_not_import_infrastructure() -> None:
    application = Path("src/app/application")
    imports = []
    for source in application.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports.extend(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )

    assert not any(module.startswith("app.infrastructure") for module in imports)


def test_request_dependency_does_not_import_infrastructure_or_bootstrap_table() -> None:
    sources = (
        Path("src/app/api/v1.py"),
        Path("src/app/presentation/dependencies.py"),
    )
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        calls = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert not any(module.startswith("app.infrastructure") for module in imports)
        assert "ensure_table" not in calls


def test_service_uses_injected_payment_provider_before_persisting_confirmation() -> None:
    payment_id = uuid4()
    stored = PaymentAttempt(
        payment_id=payment_id,
        order_id=uuid4(),
        quote_id=uuid4(),
        quote_version=3,
        attempt_number=1,
        status=PaymentStatus.CONFIRMED,
        method="PIX",
        amount=Decimal("100.00"),
        provider_reference="normalized-reference",
    )
    repository = Mock()
    repository.confirm_payment.return_value = stored
    provider = Mock()
    provider.confirm_payment.return_value = PaymentConfirmation(
        provider_reference="normalized-reference"
    )
    service = FinanceService(repository=repository, payment_provider=provider)

    result = service.confirm_simulated_payment(payment_id, "raw-reference")

    provider.confirm_payment.assert_called_once_with(payment_id, "raw-reference")
    repository.confirm_payment.assert_called_once_with(
        payment_id, "normalized-reference"
    )
    assert result is stored


def test_simulated_provider_preserves_the_external_reference() -> None:
    confirmation = SimulatedPaymentProvider().confirm_payment(
        uuid4(), "simulated-reference"
    )

    assert confirmation.provider_reference == "simulated-reference"


def test_simulated_provider_rejects_an_empty_external_reference() -> None:
    with pytest.raises(FinanceConflictError, match="referência externa"):
        SimulatedPaymentProvider().confirm_payment(uuid4(), "  ")
