"""Adapter assíncrono sobre as operações condicionais da Outbox DynamoDB."""

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

from app.config import Settings
from app.domain.models import OutboxRecord
from app.infrastructure.dynamodb import DynamoDBFinanceRepository, dynamodb_resource


class DynamoDBPublisherOutboxStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _repository(self) -> DynamoDBFinanceRepository:
        table = dynamodb_resource(self.settings).Table(self.settings.dynamodb_table)
        return DynamoDBFinanceRepository(table)

    async def claim_pending(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_duration: timedelta,
        max_attempts: int,
        now: datetime,
    ) -> list[OutboxRecord]:
        return await asyncio.to_thread(
            self._claim_pending,
            worker_id=worker_id,
            batch_size=batch_size,
            lease_duration=lease_duration,
            max_attempts=max_attempts,
            now=now,
        )

    def _claim_pending(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_duration: timedelta,
        max_attempts: int,
        now: datetime,
    ) -> list[OutboxRecord]:
        return self._repository().claim_outbox_records(
            worker_id=worker_id,
            batch_size=batch_size,
            lease_duration=lease_duration,
            max_attempts=max_attempts,
            now=now,
        )

    async def mark_published(
        self,
        *,
        message_id: UUID,
        claim_id: UUID,
        published_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._mark_published,
            message_id=message_id,
            claim_id=claim_id,
            published_at=published_at,
        )

    def _mark_published(
        self,
        *,
        message_id: UUID,
        claim_id: UUID,
        published_at: datetime,
    ) -> None:
        self._repository().mark_outbox_published(
            message_id=message_id,
            claim_id=claim_id,
            published_at=published_at,
        )

    async def mark_failed(
        self,
        *,
        message_id: UUID,
        claim_id: UUID,
        error: str,
        next_attempt_at: datetime,
        failed_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._mark_failed,
            message_id=message_id,
            claim_id=claim_id,
            error=error,
            next_attempt_at=next_attempt_at,
            failed_at=failed_at,
        )

    def _mark_failed(
        self,
        *,
        message_id: UUID,
        claim_id: UUID,
        error: str,
        next_attempt_at: datetime,
        failed_at: datetime,
    ) -> None:
        self._repository().mark_outbox_failed(
            message_id=message_id,
            claim_id=claim_id,
            error=error,
            next_attempt_at=next_attempt_at,
            failed_at=failed_at,
        )
