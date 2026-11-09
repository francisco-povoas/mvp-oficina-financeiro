"""Ports e erros do publisher da Outbox financeira."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.domain.models import OutboxRecord


@dataclass(frozen=True, slots=True)
class MessageRoute:
    queue_url: str
    group_id: str


class MessagePublicationError(RuntimeError):
    """Falha conhecida antes de uma confirmação positiva do transporte."""


class MessagePublicationIndeterminateError(MessagePublicationError):
    """O transporte pode ter aceitado a mensagem sem confirmar a resposta."""


class MessageContractError(ValueError):
    """O registro da Outbox não forma um envelope versionado válido."""


class UnknownMessageRouteError(ValueError):
    """Não existe fila aprovada para o tipo e a versão informados."""


class OutboxStore(Protocol):
    async def claim_pending(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_duration: timedelta,
        max_attempts: int,
        now: datetime,
    ) -> list[OutboxRecord]: ...

    async def mark_published(
        self,
        *,
        message_id: UUID,
        claim_id: UUID,
        published_at: datetime,
    ) -> None: ...

    async def mark_failed(
        self,
        *,
        message_id: UUID,
        claim_id: UUID,
        error: str,
        next_attempt_at: datetime,
        failed_at: datetime,
    ) -> None: ...


class MessageRouter(Protocol):
    def resolve(self, message: OutboxRecord) -> MessageRoute: ...


class MessageContractValidator(Protocol):
    def validate(self, envelope: Mapping[str, object]) -> None: ...


class MessageTransport(Protocol):
    async def publish(
        self,
        *,
        route: MessageRoute,
        body: str,
        deduplication_id: str,
    ) -> str: ...

    async def close(self) -> None: ...
