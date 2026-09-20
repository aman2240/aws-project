"""
Owns drafting a suggested answer for an allowed decision: pulls recent
context from decision_store (keyword-filtered — a stand-in for real
relevance search; Phase 5 swaps this for OpenSearch), then asks a
second LLM call for a short suggested answer with a source reference.
Never blocks the Slack notification — on timeout or any failure,
returns a fixed fallback string instead of raising.
"""

import asyncio
import logging
import re

from decision_detector import LLM_MODEL, LLM_TIMEOUT_SECONDS, DecisionRecord
from decision_store import decision_store
from groq_llm import GroqMessagesClient

logger = logging.getLogger("ghost.answers")

_llm_client = GroqMessagesClient()

_FALLBACK_TEXT = "No draft available — this one needs a live answer."

_SYSTEM_PROMPT = """You are drafting a short suggested answer for a decision that just came up in a live meeting, using relevant past decisions from the same meeting as context.

Respond in 2-3 sentences with a suggested answer, followed by a one-line source reference (which past decision, if any, informed this answer — or "no prior context" if none applied)."""

_KEYWORD_MIN_LENGTH = 4


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) >= _KEYWORD_MIN_LENGTH}


def _keyword_search(decision_text: str, candidates: list[DecisionRecord], limit: int = 5) -> list[DecisionRecord]:
    """Simple keyword-overlap ranking over already-fetched candidates —
    a basic substring/keyword match, not real relevance search. Swap
    for OpenSearch in Phase 5."""
    target_words = _keywords(decision_text)
    if not target_words:
        return []
    scored = []
    for record in candidates:
        overlap = len(target_words & _keywords(record.decision_text))
        if overlap:
            scored.append((overlap, record))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _, record in scored[:limit]]


async def draft_answer(decision: DecisionRecord, meeting_id: str) -> str:
    try:
        recent = await decision_store.get_recent(meeting_id, limit=10)
    except Exception:
        logger.exception("[meeting_id=%s] failed to fetch recent decisions for context", meeting_id)
        recent = []

    relevant = _keyword_search(decision.decision_text, recent)
    if relevant:
        context_lines = "\n".join(
            f'- ({record.status}) "{record.decision_text}" — {record.context}' for record in relevant
        )
    else:
        context_lines = "No related past decisions found."

    user_content = (
        f"Current decision/question: {decision.decision_text}\n"
        f"Context: {decision.context}\n\n"
        f"Relevant past decisions from this meeting:\n{context_lines}"
    )

    try:
        response = await asyncio.wait_for(
            _llm_client.messages.create(
                model=LLM_MODEL,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[meeting_id=%s] draft_answer timed out after %.1fs for decision %s",
            meeting_id,
            LLM_TIMEOUT_SECONDS,
            decision.id,
        )
        return _FALLBACK_TEXT
    except Exception:
        logger.exception("[meeting_id=%s] draft_answer LLM call failed for decision %s", meeting_id, decision.id)
        return _FALLBACK_TEXT

    text = next((block.text for block in response.content if block.type == "text"), "")
    return text.strip() or _FALLBACK_TEXT
