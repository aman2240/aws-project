"""
Owns persistence for DecisionRecords: an abstract DecisionStore
interface (get_recent, create, update_status) and PostgresDecisionStore,
the real implementation backed by the `decisions` table (database.py)
via async SQLAlchemy/asyncpg. Postgres handles concurrent
create()/update_status() calls — decision creation and the Slack
webhook's status updates can both hit the DB at once — natively, no
journal-mode workaround needed the way SQLite required.

Postgres is the source of truth. OpenSearch (opensearch_client.py) is a
best-effort search index kept alongside it: create()/update_status()
call opensearch_client.index_decision() after the Postgres write
succeeds, so callers of this interface never talk to OpenSearch
directly — that composite behavior lives here, the one place the
interface is implemented.
"""

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import timezone

from sqlalchemy import select

import database
import opensearch_client
from decision_detector import DecisionRecord, MentionType

logger = logging.getLogger("ghost.decision_store")


class DecisionStore(ABC):
    @abstractmethod
    async def get_recent(self, meeting_id: str, limit: int) -> list[DecisionRecord]:
        """Most recent decisions for a meeting, newest first."""

    @abstractmethod
    async def create(self, decision: DecisionRecord) -> None:
        """Persist a newly detected decision."""

    # NEVER call update_status with status="approved"/"rejected" except
    # from slack_webhook.py's button handler, after verifying the Slack
    # request signature. That is the only legitimate trigger for either
    # status — this is a structural rule, not just policy.
    @abstractmethod
    async def update_status(
        self, decision_id: uuid.UUID, status: str, approved_by: str | None
    ) -> "DecisionRecord | None":
        """Update a decision's status (e.g. "approved", "rejected",
        "denied_by_policy") and return the updated record (None if
        decision_id is unknown) — Phase 6's slack_webhook.py needs the
        record's meeting_id to push the update to the right side panel
        connection. approved_by is logged for audit purposes; the
        schema has a column for it but DecisionRecord (Phase 3's shape,
        consumed as-is by everything upstream) doesn't — it's never
        round-tripped back out through get_recent."""


def _row_to_record(row: "database.Decision") -> DecisionRecord:
    # A plain (non-timezone) DateTime column strips tzinfo on the way
    # back out, regardless of backend. Every timestamp written here was
    # already UTC (TranscriptEvent/DecisionRecord both use
    # datetime.now(timezone.utc)), so reattaching it is safe and keeps
    # DecisionRecord's contract (tz-aware) consistent for callers.
    timestamp = row.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return DecisionRecord(
        id=row.id,
        meeting_id=row.meeting_id,
        decision_text=row.decision_text,
        speaker=row.speaker,
        requires_action_from=row.requires_action_from,
        context=row.context,
        mention_type=MentionType(row.mention_type),
        mention_quote=row.mention_quote,
        confidence=row.confidence,
        urgency=row.urgency,
        timestamp=timestamp,
        status=row.status,
    )


class PostgresDecisionStore(DecisionStore):
    async def create(self, decision: DecisionRecord) -> None:
        async with database.async_session_maker() as session:
            session.add(
                database.Decision(
                    id=decision.id,
                    meeting_id=decision.meeting_id,
                    decision_text=decision.decision_text,
                    speaker=decision.speaker,
                    requires_action_from=decision.requires_action_from,
                    context=decision.context,
                    mention_type=decision.mention_type.value,
                    mention_quote=decision.mention_quote,
                    confidence=decision.confidence,
                    urgency=decision.urgency,
                    # asyncpg is strict where aiosqlite was lenient: it
                    # rejects a tz-aware datetime outright against the
                    # plain (non-timezone) TIMESTAMP column rather than
                    # silently dropping the tzinfo. Every timestamp
                    # here is already UTC (see _row_to_record), so
                    # stripping it before the write is safe and keeps
                    # the column's existing (non-tz) type unchanged.
                    timestamp=decision.timestamp.replace(tzinfo=None),
                    status=decision.status,
                    approved_by=None,
                )
            )
            await session.commit()
        await opensearch_client.index_decision(decision)

    async def get_recent(self, meeting_id: str, limit: int) -> list[DecisionRecord]:
        async with database.async_session_maker() as session:
            result = await session.execute(
                select(database.Decision)
                .where(database.Decision.meeting_id == meeting_id)
                .order_by(database.Decision.timestamp.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return [_row_to_record(row) for row in rows]

    async def update_status(
        self, decision_id: uuid.UUID, status: str, approved_by: str | None
    ) -> DecisionRecord | None:
        async with database.async_session_maker() as session:
            result = await session.execute(
                select(database.Decision).where(database.Decision.id == decision_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                logger.warning("update_status: unknown decision_id %s", decision_id)
                return None
            row.status = status
            row.approved_by = approved_by
            await session.commit()
            # expire_on_commit=False on the sessionmaker keeps row's
            # attributes readable here without a refresh query.
            updated_record = _row_to_record(row)
        await opensearch_client.index_decision(updated_record, approved_by)
        logger.info(
            "decision %s status -> %s (approved_by=%s)", decision_id, status, approved_by
        )
        return updated_record


decision_store = PostgresDecisionStore()
