"""
Tests decision_detector.DecisionPipeline against the hand-written
fixtures in tests/fixtures/decision_fixtures.py. The LLM (Tier 2) is
replaced with a fake LLM client so these run fast, free, and
deterministically — no real network call, no real API key needed.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from config import settings
from decision_detector import DecisionPipeline, MentionType
from tests.fixtures.decision_fixtures import (
    MEETING_ID,
    one_clear_decision,
    overlapping_speakers,
    restated_question_dedup,
    three_decisions_one_batch_window,
    throwaway_name_mention,
)


class _FakeMessages:
    def __init__(self, classify_fn):
        self._classify_fn = classify_fn
        self.calls: list[str] = []

    async def create(self, **kwargs):
        content = kwargs["messages"][0]["content"]
        self.calls.append(content)
        result = self._classify_fn(content)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(result))])


class _FakeLLMClient:
    def __init__(self, classify_fn):
        self.messages = _FakeMessages(classify_fn)


def make_fake_client(classify_fn):
    return _FakeLLMClient(classify_fn)


class RecordingCallback:
    """Stands in for Phase 4's real Slack/Cedar callback."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, meeting_id, decisions):
        self.calls.append((meeting_id, list(decisions)))


@contextlib.contextmanager
def _short_debounce(seconds: float):
    """Shortens DEBOUNCE_WINDOW_SECONDS for the duration of a test so
    we don't actually wait 12 real seconds per test."""
    original = settings.debounce_window_seconds
    settings.debounce_window_seconds = seconds
    try:
        yield
    finally:
        settings.debounce_window_seconds = original


def _last_line(content: str) -> str:
    return content.strip().splitlines()[-1]


@pytest.mark.asyncio
async def test_a_no_action_mention_produces_no_notification():
    # Confidence is deliberately high (above threshold) so this test
    # exercises the actionability gate specifically — mention_type
    # alone must block notification, not the pre-existing confidence
    # gate (which is covered elsewhere).
    def classify_fn(content):
        return {
            "mention_type": "NO_ACTION",
            "mention_quote": "Great to have you Sarah, hope the weather's nice where you are.",
            "requires_action_from": None,
            "decision_text": "Small talk mentioning Sarah's name.",
            "context": "Just a greeting, nothing decision-relevant.",
            "confidence": 0.95,
            "urgency": "low",
        }

    callback = RecordingCallback()
    pipeline = DecisionPipeline(on_decision_batch=callback, llm_client=make_fake_client(classify_fn))
    pipeline.set_watch_names(MEETING_ID, ["Sarah"])

    with _short_debounce(0.1):
        for event in throwaway_name_mention():
            await pipeline.process_transcript_event(event)
        await asyncio.sleep(0.3)

    assert callback.calls == []


@pytest.mark.asyncio
async def test_b_one_clear_decision_is_detected():
    expected_text = "Whether to ship on Friday or wait until Monday"
    expected_quote = "Sarah, should we ship on Friday or wait until Monday?"

    def classify_fn(content):
        return {
            "mention_type": "DIRECT_REQUEST",
            "mention_quote": expected_quote,
            "requires_action_from": "Sarah",
            "decision_text": expected_text,
            "context": "The team is deciding on a ship date.",
            "confidence": 0.9,
            "urgency": "medium",
        }

    callback = RecordingCallback()
    pipeline = DecisionPipeline(on_decision_batch=callback, llm_client=make_fake_client(classify_fn))
    pipeline.set_watch_names(MEETING_ID, ["Sarah"])

    with _short_debounce(0.1):
        for event in one_clear_decision():
            await pipeline.process_transcript_event(event)
        await asyncio.sleep(0.3)

    assert len(callback.calls) == 1
    meeting_id, records = callback.calls[0]
    assert meeting_id == MEETING_ID
    assert len(records) == 1
    record = records[0]
    assert record.decision_text == expected_text
    assert record.requires_action_from == "Sarah"
    assert record.urgency == "medium"
    assert record.meeting_id == MEETING_ID
    assert record.status == "pending"
    assert record.mention_type == MentionType.DIRECT_REQUEST
    assert record.mention_quote == expected_quote


@pytest.mark.asyncio
async def test_c_three_decisions_land_in_one_batch():
    def classify_fn(content):
        last_line = _last_line(content)
        if "rate limit" in last_line:
            text = "Whether to raise the rate limit to 1000 requests per minute"
        elif "feature flag" in last_line:
            text = "Whether to ship the auth flow behind a feature flag"
        else:
            text = "Whether to delay the release to next Tuesday"
        return {
            "mention_type": "DIRECT_REQUEST",
            "mention_quote": last_line,
            "requires_action_from": "Sarah",
            "decision_text": text,
            "context": "Discussion during the review.",
            "confidence": 0.85,
            "urgency": "medium",
        }

    callback = RecordingCallback()
    pipeline = DecisionPipeline(on_decision_batch=callback, llm_client=make_fake_client(classify_fn))
    pipeline.set_watch_names(MEETING_ID, ["Sarah"])

    with _short_debounce(0.3):
        for event in three_decisions_one_batch_window():
            await pipeline.process_transcript_event(event)
        await asyncio.sleep(0.6)

    assert len(callback.calls) == 1
    meeting_id, records = callback.calls[0]
    assert meeting_id == MEETING_ID
    assert len(records) == 3
    assert len({r.decision_text for r in records}) == 3


@pytest.mark.asyncio
async def test_d_overlapping_speakers_attributes_correct_speaker():
    def classify_fn(content):
        return {
            "mention_type": "DIRECT_REQUEST",
            "mention_quote": "Sarah, should we go with option A or option B for the rollout?",
            "requires_action_from": "Sarah",
            "decision_text": "Whether to go with option A or option B for the rollout",
            "context": "Raj asked Sarah to pick a rollout option.",
            "confidence": 0.88,
            "urgency": "medium",
        }

    callback = RecordingCallback()
    pipeline = DecisionPipeline(on_decision_batch=callback, llm_client=make_fake_client(classify_fn))
    pipeline.set_watch_names(MEETING_ID, ["Sarah"])

    with _short_debounce(0.1):
        for event in overlapping_speakers():
            await pipeline.process_transcript_event(event)
        await asyncio.sleep(0.3)

    assert len(callback.calls) == 1
    _, records = callback.calls[0]
    assert len(records) == 1
    # Raj (spk_1) asked the question — must not be attributed to
    # Sarah (spk_0) or Mike (spk_2, the last speaker in the window).
    assert records[0].speaker == "Raj"


@pytest.mark.asyncio
async def test_e_restated_question_dedups_to_one_record():
    def classify_fn(content):
        return {
            "mention_type": "DIRECT_REQUEST",
            "mention_quote": "Sarah, should we bump the rate limit to 1000 requests per minute?",
            "requires_action_from": "Sarah",
            "decision_text": "Whether to bump the rate limit to 1000 requests per minute",
            "context": "Asked twice about the rate limit change.",
            "confidence": 0.9,
            "urgency": "medium",
        }

    callback = RecordingCallback()
    pipeline = DecisionPipeline(on_decision_batch=callback, llm_client=make_fake_client(classify_fn))
    pipeline.set_watch_names(MEETING_ID, ["Sarah"])

    with _short_debounce(0.1):
        for event in restated_question_dedup():
            await pipeline.process_transcript_event(event)
        await asyncio.sleep(0.3)

    assert len(callback.calls) == 1
    _, records = callback.calls[0]
    assert len(records) == 1
