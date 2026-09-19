"""
Owns the OpenSearch integration for decision search: index setup with
an explicit mapping (never relying on auto-inferred field types),
indexing/updating a decision document, and the multi_match search
query. Every call here is best-effort from the caller's point of
view — index_decision never raises (SQLiteDecisionStore calls it
after every write and must not fail decision creation/status updates
if OpenSearch is down), while search_decisions is allowed to raise so
main.py's /search endpoint can catch it and fall back to SQLite.
"""

import logging

from opensearchpy import AsyncOpenSearch

from config import settings
from decision_detector import DecisionRecord

logger = logging.getLogger("ghost.opensearch")

_INDEX_NAME = "decisions"

_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "meeting_id": {"type": "keyword"},
            "decision_text": {"type": "text"},
            "context": {"type": "text"},
            "speaker": {"type": "keyword"},
            "requires_action_from": {"type": "keyword"},
            "urgency": {"type": "keyword"},
            "status": {"type": "keyword"},
            "approved_by": {"type": "keyword"},
            "timestamp": {"type": "date"},
            "confidence": {"type": "float"},
        }
    }
}

# None when OPENSEARCH_HOST isn't set — every function below treats
# that as "OpenSearch isn't part of this deployment", not an error.
_client: "AsyncOpenSearch | None" = None
if settings.opensearch_host:
    _auth = None
    if settings.opensearch_user and settings.opensearch_password:
        _auth = (settings.opensearch_user, settings.opensearch_password)
    # Short timeout so a misconfigured/unreachable host fails fast
    # rather than hanging app startup or a request.
    _client = AsyncOpenSearch(
        hosts=[settings.opensearch_host],
        http_auth=_auth,
        verify_certs=False,
        ssl_show_warn=False,
        timeout=5,
    )


async def ensure_index() -> None:
    """Creates the decisions index with its explicit mapping if it
    doesn't exist. Called from main.py's lifespan at startup — never
    blocks/fails app startup if OpenSearch isn't reachable."""
    if _client is None:
        logger.info("OPENSEARCH_HOST not set; skipping OpenSearch index setup")
        return
    try:
        if not await _client.indices.exists(index=_INDEX_NAME):
            await _client.indices.create(index=_INDEX_NAME, body=_INDEX_MAPPING)
            logger.info("created OpenSearch index %r", _INDEX_NAME)
    except Exception:
        logger.warning(
            "OpenSearch unreachable or index setup failed; continuing without it", exc_info=True
        )


async def close() -> None:
    if _client is not None:
        await _client.close()


def _document(decision: DecisionRecord, approved_by: str | None) -> dict:
    return {
        "id": str(decision.id),
        "meeting_id": decision.meeting_id,
        "decision_text": decision.decision_text,
        "context": decision.context,
        "speaker": decision.speaker,
        "requires_action_from": decision.requires_action_from,
        "urgency": decision.urgency,
        "status": decision.status,
        "approved_by": approved_by,
        "timestamp": decision.timestamp.isoformat(),
        "confidence": decision.confidence,
    }


async def index_decision(decision: DecisionRecord, approved_by: str | None = None) -> None:
    """Indexes or updates (same doc id -> upsert) a decision. Takes
    approved_by separately since DecisionRecord itself has no field for
    it (Phase 4's SQLiteDecisionStore.update_status gets it as a
    parameter, not on the record) — without this, approved_by could
    never actually become searchable despite search_decisions querying
    it. Never raises: an indexing failure must not break decision
    creation or a status update succeeding in SQLite."""
    if _client is None:
        return
    try:
        await _client.index(
            index=_INDEX_NAME,
            id=str(decision.id),
            body=_document(decision, approved_by),
        )
    except Exception:
        logger.exception("failed to index decision %s in OpenSearch", decision.id)


async def search_decisions(query: str) -> list[dict]:
    """multi_match across decision_text, approved_by, speaker. Raises
    if OpenSearch isn't configured or the call fails — main.py's
    /search route is what catches this and falls back to SQLite, so
    this function doesn't swallow errors itself."""
    if _client is None:
        raise RuntimeError("OpenSearch is not configured (OPENSEARCH_HOST unset)")

    response = await _client.search(
        index=_INDEX_NAME,
        body={
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["decision_text", "approved_by", "speaker"],
                }
            }
        },
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]
