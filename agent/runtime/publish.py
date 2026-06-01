"""Helpers for idempotent GitLab publishing backed by the runtime store."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from agent.config import settings
from agent.controlplane import get_tracking_service
from agent.runtime.models import PublishChannel, PublishReceipt, PublishStatus
from agent.runtime.store import _publish_receipt_can_be_reclaimed


class GitLabPublishService:
    """Record publish receipts so repeated runs do not duplicate GitLab output."""

    def __init__(self, *, store=None, actor_key: str | None = None, tracking_run_id: str | None = None) -> None:
        self.store = store
        self.actor_key = actor_key
        self.tracking_run_id = tracking_run_id

    def _make_receipt(
        self,
        *,
        op_key: str,
        channel: PublishChannel,
        external_id: int | str | None,
        status: PublishStatus,
    ) -> PublishReceipt:
        return PublishReceipt(
            actor_key=self.actor_key or "",
            op_key=op_key,
            channel=channel,
            external_id=str(external_id) if external_id is not None else None,
            status=status,
        )

    async def _claim(self, *, op_key: str, channel: PublishChannel) -> tuple[PublishReceipt, bool]:
        receipt = self._make_receipt(
            op_key=op_key,
            channel=channel,
            external_id=None,
            status="claimed",
        )
        if not self.store or not self.actor_key:
            return receipt, True
        if hasattr(self.store, "claim_publish_receipt"):
            return await self.store.claim_publish_receipt(
                receipt,
                stale_after_seconds=int(settings.RUNTIME_PUBLISH_CLAIM_TTL_SECONDS or 0),
            )
        existing = await self.store.get_publish_receipt(self.actor_key, op_key)
        if existing is not None and not _publish_receipt_can_be_reclaimed(
            existing,
            stale_after_seconds=int(settings.RUNTIME_PUBLISH_CLAIM_TTL_SECONDS or 0),
        ):
            return existing, False
        await self.store.record_publish_receipt(receipt)
        return receipt, True

    async def _record(
        self,
        *,
        op_key: str,
        channel: PublishChannel,
        external_id: int | str | None,
        record: dict[str, Any] | None = None,
    ) -> PublishReceipt:
        receipt = self._make_receipt(
            op_key=op_key,
            channel=channel,
            external_id=external_id,
            status="completed",
        )
        if self.store and self.actor_key:
            await self.store.record_publish_receipt(receipt)
        if self.tracking_run_id and record is not None:
            get_tracking_service().append_published_object(
                self.tracking_run_id,
                {
                    "channel": channel,
                    "external_id": str(external_id) if external_id is not None else None,
                    "created_at": receipt.created_at,
                    **record,
                },
            )
        return receipt

    async def _publish(
        self,
        *,
        op_key: str,
        channel: PublishChannel,
        publisher: Callable[[], int | str | None],
        record: dict[str, Any] | None = None,
    ) -> PublishReceipt:
        claim, claimed = await self._claim(op_key=op_key, channel=channel)
        if not claimed:
            return claim

        try:
            result = publisher()
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            if self.store and self.actor_key:
                await self.store.record_publish_receipt(
                    self._make_receipt(
                        op_key=op_key,
                        channel=channel,
                        external_id=None,
                        status="failed",
                    )
                )
            raise
        return await self._record(op_key=op_key, channel=channel, external_id=result, record=record)

    async def publish_mr_note(
        self,
        *,
        op_key: str,
        publisher: Callable[[], int | str | None],
        record: dict[str, Any] | None = None,
    ) -> PublishReceipt:
        return await self._publish(op_key=op_key, channel="mr_note", publisher=publisher, record=record)

    async def publish_discussion_reply(
        self,
        *,
        op_key: str,
        publisher: Callable[[], int | str | None],
        record: dict[str, Any] | None = None,
    ) -> PublishReceipt:
        return await self._publish(op_key=op_key, channel="discussion_reply", publisher=publisher, record=record)

    async def publish_inline_comment(
        self,
        *,
        op_key: str,
        publisher: Callable[[], int | str | None],
        record: dict[str, Any] | None = None,
    ) -> PublishReceipt:
        return await self._publish(op_key=op_key, channel="inline_comment", publisher=publisher, record=record)
