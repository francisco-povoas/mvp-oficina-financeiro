"""Caso de uso independente para publicação at-least-once da Outbox financeira."""

import asyncio
import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.application.messaging import (
    MessageContractError,
    MessageContractValidator,
    MessagePublicationError,
    MessageRoute,
    MessageRouter,
    MessageTransport,
    OutboxStore,
    UnknownMessageRouteError,
)
from app.domain.exceptions import FinanceConflictError
from app.domain.models import OutboxRecord


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PublisherPolicy:
    batch_size: int = 10
    lease_seconds: float = 30.0
    poll_interval_seconds: float = 1.0
    max_attempts: int = 8
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("publisher batch_size deve ser positivo")
        if self.lease_seconds <= 0:
            raise ValueError("publisher lease_seconds deve ser positivo")
        if self.poll_interval_seconds <= 0:
            raise ValueError("publisher poll_interval_seconds deve ser positivo")
        if self.max_attempts <= 0:
            raise ValueError("publisher max_attempts deve ser positivo")
        if self.initial_backoff_seconds <= 0:
            raise ValueError("publisher initial_backoff_seconds deve ser positivo")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("publisher max_backoff_seconds deve ser >= initial_backoff_seconds")


def build_envelope(message: OutboxRecord) -> dict[str, object]:
    suffix = f".v{message.event_version}"
    if not message.event_type.endswith(suffix):
        raise MessageContractError("tipo e versão persistidos são incompatíveis")
    message_type = message.event_type[: -len(suffix)]
    if not message_type:
        raise MessageContractError("tipo de mensagem vazio")
    return {
        "message_id": str(message.message_id),
        "message_type": message_type,
        "message_version": message.event_version,
        "occurred_at": message.occurred_at.isoformat().replace("+00:00", "Z"),
        "correlation_id": str(message.correlation_id),
        "causation_id": str(message.causation_id) if message.causation_id else None,
        "operation_id": str(message.operation_id),
        "saga_id": None,
        "payload": message.payload,
    }


class OutboxPublisher:
    def __init__(
        self,
        *,
        worker_id: str,
        store: OutboxStore,
        router: MessageRouter,
        validator: MessageContractValidator,
        transport: MessageTransport,
        policy: PublisherPolicy,
        clock: Callable[[], datetime] = utc_now,
        jitter: Callable[[float, float], float] = random.uniform,
        logger: logging.Logger | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id não pode ser vazio")
        self.worker_id = worker_id
        self.store = store
        self.router = router
        self.validator = validator
        self.transport = transport
        self.policy = policy
        self.clock = clock
        self.jitter = jitter
        self.logger = logger or logging.getLogger(__name__)

    async def run_once(self) -> int:
        claimed_at = self.clock()
        messages = await self.store.claim_pending(
            worker_id=self.worker_id,
            batch_size=self.policy.batch_size,
            lease_duration=timedelta(seconds=self.policy.lease_seconds),
            max_attempts=self.policy.max_attempts,
            now=claimed_at,
        )
        for message in messages:
            await self._publish_one(message)
        return len(messages)

    async def run(self, stop_event: asyncio.Event) -> None:
        self.logger.info("outbox_publisher_started worker_id=%s", self.worker_id)
        try:
            while not stop_event.is_set():
                try:
                    claimed = await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.error(
                        "outbox_claim_failed worker_id=%s error_type=%s",
                        self.worker_id,
                        type(exc).__name__,
                    )
                    claimed = 0
                if claimed == 0:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=self.policy.poll_interval_seconds,
                        )
                    except TimeoutError:
                        pass
        finally:
            self.logger.info("outbox_publisher_stopped worker_id=%s", self.worker_id)

    async def _publish_one(self, message: OutboxRecord) -> None:
        try:
            envelope = build_envelope(message)
            route = self.router.resolve(message)
            self.validator.validate(envelope)
            body = json.dumps(
                envelope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            transport_message_id = await self.transport.publish(
                route=route,
                body=body,
                deduplication_id=str(message.message_id),
            )
        except asyncio.CancelledError:
            raise
        except (MessageContractError, UnknownMessageRouteError, MessagePublicationError) as exc:
            await self._record_failure(message, exc)
            return
        except Exception as exc:
            await self._record_failure(message, exc)
            return

        assert message.claim_id is not None
        try:
            await self.store.mark_published(
                message_id=message.message_id,
                claim_id=message.claim_id,
                published_at=self.clock(),
            )
        except Exception as exc:
            self.logger.error(
                "outbox_confirmation_failed message_id=%s correlation_id=%s "
                "operation_id=%s claim_id=%s error_type=%s",
                message.message_id,
                message.correlation_id,
                message.operation_id,
                message.claim_id,
                type(exc).__name__,
            )
            return

        self.logger.info(
            "outbox_published message_id=%s correlation_id=%s operation_id=%s "
            "transport_message_id=%s attempt=%s",
            message.message_id,
            message.correlation_id,
            message.operation_id,
            transport_message_id,
            message.attempt_count,
        )

    async def _record_failure(self, message: OutboxRecord, exc: Exception) -> None:
        assert message.claim_id is not None
        failed_at = self.clock()
        next_attempt_at = failed_at + timedelta(
            seconds=self._retry_delay(message.attempt_count)
        )
        safe_error = self._safe_error(exc)
        try:
            await self.store.mark_failed(
                message_id=message.message_id,
                claim_id=message.claim_id,
                error=safe_error,
                next_attempt_at=next_attempt_at,
                failed_at=failed_at,
            )
        except FinanceConflictError:
            self.logger.warning(
                "outbox_failure_not_recorded_stale_claim message_id=%s claim_id=%s",
                message.message_id,
                message.claim_id,
            )
            return
        except Exception as store_exc:
            self.logger.error(
                "outbox_failure_persistence_failed message_id=%s claim_id=%s error_type=%s",
                message.message_id,
                message.claim_id,
                type(store_exc).__name__,
            )
            return
        self.logger.warning(
            "outbox_publish_failed message_id=%s correlation_id=%s operation_id=%s "
            "attempt=%s exhausted=%s retry_at=%s error=%s",
            message.message_id,
            message.correlation_id,
            message.operation_id,
            message.attempt_count,
            message.attempt_count >= self.policy.max_attempts,
            next_attempt_at.isoformat(),
            safe_error,
        )

    def _retry_delay(self, attempts: int) -> float:
        cap = min(
            self.policy.max_backoff_seconds,
            self.policy.initial_backoff_seconds * (2 ** max(attempts - 1, 0)),
        )
        return self.jitter(0.0, cap)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(
            exc,
            (MessageContractError, UnknownMessageRouteError, MessagePublicationError),
        ):
            return f"{type(exc).__name__}: {str(exc).strip()}"[:1000]
        return type(exc).__name__


class StaticFinanceiroMessageRouter:
    _ROUTED_TYPES = frozenset(
        {
            "QuoteCommunicated.v1",
            "QuoteApproved.v1",
            "QuoteRejected.v1",
            "PaymentConfirmed.v1",
        }
    )

    def __init__(self, queue_url: str) -> None:
        if not queue_url.strip():
            raise ValueError("SQS_FINANCEIRO_TO_ORDENS_URL deve ser informado")
        self.queue_url = queue_url

    def resolve(self, message: OutboxRecord) -> MessageRoute:
        if message.event_type not in self._ROUTED_TYPES or message.event_version != 1:
            raise UnknownMessageRouteError(
                f"rota ausente para {message.event_type} (versão {message.event_version})"
            )
        order_id = message.payload.get("order_id")
        if not isinstance(order_id, str):
            raise MessageContractError("payload.order_id deve ser UUID textual")
        try:
            group_id = str(UUID(order_id))
        except ValueError as exc:
            raise MessageContractError("payload.order_id deve ser UUID textual") from exc
        return MessageRoute(queue_url=self.queue_url, group_id=group_id)
