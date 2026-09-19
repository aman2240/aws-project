"""
Extends Phase 3's fixture-based tests through the full notification
pipeline (Cedar -> draft -> Slack): the shared decision_pipeline
singleton — the same one main.py wires up in production — gets its
LLM client, callback, and decision_store monkeypatched for the
duration of each test, so this exercises the real production wiring
path rather than a parallel one. No real Anthropic/Slack network calls
happen anywhere in this file.

Uses the real shared SQLiteDecisionStore (backed by tests/conftest.py's
isolated test_ghost.db) rather than a per-test store — SQLiteDecisionStore
has no per-instance path to isolate the way Phase 4's JSONLDecisionStore
did, since every instance talks to the same database.py engine. Each
test's decisions stay disambiguated by meeting_id, same as production.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

import answer_drafter
import cedar_policy
import session_connections
import slack_notifier
from config import settings
from decision_detector import decision_pipeline
from decision_store import decision_store as shared_decision_store
from notification_pipeline import make_notification_pipeline
from tests.fixtures.decision_fixtures import (
    MEETING_ID,
    one_clear_decision,
    three_decisions_one_batch_window,
)


class _FakeCompletions:
    def __init__(self, classify_fn):
        self._classify_fn = classify_fn

    async def create(self, **kwargs):
        content = next(
            (m["content"] for m in kwargs["messages"] if m["role"] == "user"), ""
        )
        result = self._classify_fn(content)
        msg = SimpleNamespace(content=json.dumps(result))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _fake_detection_client(classify_fn):
    completions = _FakeCompletions(classify_fn)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


async def _fast_draft_create(**kwargs):
    msg = SimpleNamespace(content="Go with Friday. Source: no prior context.")
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@contextlib.contextmanager
def _short_debounce(seconds: float):
    original = settings.debounce_window_seconds
    settings.debounce_window_seconds = seconds
    try:
        yield
    finally:
        settings.debounce_window_seconds = original


@pytest.fixture(autouse=True)
def isolate_session():
    decision_pipeline._sessions.pop(MEETING_ID, None)
    yield
    decision_pipeline._sessions.pop(MEETING_ID, None)


@pytest.fixture(autouse=True)
def fast_draft_llm(monkeypatch):
    """Default fast/successful answer_drafter LLM — individual tests
    override this (e.g. the timeout test) via their own monkeypatch."""
    monkeypatch.setattr(answer_drafter, "_llm_client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_fast_draft_create))))


@pytest.fixture
def fake_decision_store():
    return shared_decision_store


@pytest.fixture
def captured_slack_calls(monkeypatch):
    calls = []

    async def fake_post_message(**kwargs):
        calls.append(kwargs)
        return {"ts": "123.456"}

    async def fake_lookup_email(**kwargs):
        return {"user": {"id": "U_FAKE"}}

    monkeypatch.setattr(slack_notifier._client, "chat_postMessage", fake_post_message)
    monkeypatch.setattr(slack_notifier._client, "users_lookupByEmail", fake_lookup_email)
    return calls


@pytest.fixture
def captured_panel_pushes(monkeypatch):
    pushes = []

    async def fake_push(meeting_id, message):
        pushes.append((meeting_id, message))

    monkeypatch.setattr(session_connections, "push", fake_push)
    return pushes


def _wire_pipeline(monkeypatch, classify_fn, decision_store):
    monkeypatch.setattr(decision_pipeline, "_llm_client", _fake_detection_client(classify_fn))
    monkeypatch.setattr(decision_pipeline, "on_decision_batch", make_notification_pipeline(decision_store))
    monkeypatch.setattr(decision_pipeline, "_decision_store", decision_store)
    decision_pipeline.set_watch_names(MEETING_ID, ["Sarah"])
    decision_pipeline.set_slack_target(MEETING_ID, "sarah@example.com")


@pytest.mark.asyncio
async def test_one_clear_decision_sends_one_slack_message(monkeypatch, fake_decision_store, captured_slack_calls):
    expected_text = "Whether to ship on Friday or wait until Monday"

    def classify_fn(content):
        return {
            "is_decision": True,
            "requires_action_from": "Sarah",
            "decision_text": expected_text,
            "context": "The team is deciding on a ship date.",
            "confidence": 0.9,
            "urgency": "medium",
        }

    _wire_pipeline(monkeypatch, classify_fn, fake_decision_store)

    with _short_debounce(0.1):
        for event in one_clear_decision():
            await decision_pipeline.process_transcript_event(event)
        await asyncio.sleep(0.5)

    assert len(captured_slack_calls) == 1
    call = captured_slack_calls[0]
    assert call["channel"] == "U_FAKE"  # resolved from the sarah@example.com target

    blocks = call["blocks"]
    section_blocks = [b for b in blocks if b["type"] == "section"]
    actions_blocks = [b for b in blocks if b["type"] == "actions"]
    assert len(section_blocks) == 1
    assert expected_text in section_blocks[0]["text"]["text"]
    assert len(actions_blocks) == 1
    buttons = actions_blocks[0]["elements"]
    assert len(buttons) == 2
    assert buttons[0]["value"].endswith(":approve")
    assert buttons[1]["value"].endswith(":reject")


@pytest.mark.asyncio
async def test_three_decisions_send_one_slack_message_with_three_blocks(
    monkeypatch, fake_decision_store, captured_slack_calls
):
    def classify_fn(content):
        last_line = content.strip().splitlines()[-1]
        if "rate limit" in last_line:
            text = "Whether to raise the rate limit to 1000 requests per minute"
        elif "feature flag" in last_line:
            text = "Whether to ship the auth flow behind a feature flag"
        else:
            text = "Whether to delay the release to next Tuesday"
        return {
            "is_decision": True,
            "requires_action_from": "Sarah",
            "decision_text": text,
            "context": "Discussion during the review.",
            "confidence": 0.85,
            "urgency": "medium",
        }

    _wire_pipeline(monkeypatch, classify_fn, fake_decision_store)

    with _short_debounce(0.3):
        for event in three_decisions_one_batch_window():
            await decision_pipeline.process_transcript_event(event)
        await asyncio.sleep(0.6)

    assert len(captured_slack_calls) == 1
    blocks = captured_slack_calls[0]["blocks"]
    section_blocks = [b for b in blocks if b["type"] == "section"]
    actions_blocks = [b for b in blocks if b["type"] == "actions"]
    assert len(section_blocks) == 3
    assert len(actions_blocks) == 3
    for actions in actions_blocks:
        assert len(actions["elements"]) == 2


@pytest.mark.asyncio
async def test_policy_check_failure_sends_no_slack_message(monkeypatch, fake_decision_store, captured_slack_calls):
    # Simulates a malformed decisions.cedar: is_authorized() parses a
    # raw string internally, so swapping the pre-parsed PolicySet for
    # an invalid string exercises the exact same failure path a
    # corrupt policy file would trigger at check time.
    monkeypatch.setattr(cedar_policy, "_POLICY_SET", "this is not valid cedar")

    def classify_fn(content):
        return {
            "is_decision": True,
            "requires_action_from": "Sarah",
            "decision_text": "Whether to ship on Friday or wait until Monday",
            "context": "ctx",
            "confidence": 0.9,
            "urgency": "medium",
        }

    _wire_pipeline(monkeypatch, classify_fn, fake_decision_store)

    with _short_debounce(0.1):
        for event in one_clear_decision():
            await decision_pipeline.process_transcript_event(event)
        await asyncio.sleep(0.5)

    assert captured_slack_calls == []

    recent = await fake_decision_store.get_recent(MEETING_ID, limit=10)
    assert any(r.status == "denied_by_policy" for r in recent)


@pytest.mark.asyncio
async def test_draft_timeout_still_sends_with_fallback_text(monkeypatch, fake_decision_store, captured_slack_calls):
    async def slow_create(**kwargs):
        await asyncio.sleep(100)  # never resolves within the timeout below

    monkeypatch.setattr(answer_drafter, "_llm_client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=slow_create))))
    monkeypatch.setattr(answer_drafter, "LLM_TIMEOUT_SECONDS", 0.2)

    def classify_fn(content):
        return {
            "is_decision": True,
            "requires_action_from": "Sarah",
            "decision_text": "Whether to ship on Friday or wait until Monday",
            "context": "ctx",
            "confidence": 0.9,
            "urgency": "medium",
        }

    _wire_pipeline(monkeypatch, classify_fn, fake_decision_store)

    with _short_debounce(0.1):
        for event in one_clear_decision():
            await decision_pipeline.process_transcript_event(event)
        await asyncio.sleep(0.6)

    assert len(captured_slack_calls) == 1
    blocks = captured_slack_calls[0]["blocks"]
    section = next(b for b in blocks if b["type"] == "section")
    assert "No draft available" in section["text"]["text"]


@pytest.mark.asyncio
async def test_allowed_decision_pushes_decision_batch_with_drafted_answer(
    monkeypatch, fake_decision_store, captured_slack_calls, captured_panel_pushes
):
    expected_text = "Whether to ship on Friday or wait until Monday"

    def classify_fn(content):
        return {
            "is_decision": True,
            "requires_action_from": "Sarah",
            "decision_text": expected_text,
            "context": "ctx",
            "confidence": 0.9,
            "urgency": "medium",
        }

    _wire_pipeline(monkeypatch, classify_fn, fake_decision_store)

    with _short_debounce(0.1):
        for event in one_clear_decision():
            await decision_pipeline.process_transcript_event(event)
        await asyncio.sleep(0.5)

    batch_pushes = [p for p in captured_panel_pushes if p[1]["type"] == "decision_batch"]
    assert len(batch_pushes) == 1
    meeting_id, message = batch_pushes[0]
    assert meeting_id == MEETING_ID
    assert len(message["decisions"]) == 1
    pushed = message["decisions"][0]
    assert pushed["decision_text"] == expected_text
    assert pushed["status"] == "pending"
    assert pushed["drafted_answer"] == "Go with Friday. Source: no prior context."


@pytest.mark.asyncio
async def test_denied_decision_pushes_decision_batch_without_drafted_answer(
    monkeypatch, fake_decision_store, captured_slack_calls, captured_panel_pushes
):
    monkeypatch.setattr(cedar_policy, "_POLICY_SET", "this is not valid cedar")

    def classify_fn(content):
        return {
            "is_decision": True,
            "requires_action_from": "Sarah",
            "decision_text": "Whether to ship on Friday or wait until Monday",
            "context": "ctx",
            "confidence": 0.9,
            "urgency": "medium",
        }

    _wire_pipeline(monkeypatch, classify_fn, fake_decision_store)

    with _short_debounce(0.1):
        for event in one_clear_decision():
            await decision_pipeline.process_transcript_event(event)
        await asyncio.sleep(0.5)

    # No Slack message (already covered elsewhere), but the panel
    # still gets the batch so it can show the denied_by_policy badge.
    assert captured_slack_calls == []
    batch_pushes = [p for p in captured_panel_pushes if p[1]["type"] == "decision_batch"]
    assert len(batch_pushes) == 1
    pushed = batch_pushes[0][1]["decisions"][0]
    assert pushed["drafted_answer"] is None
    assert pushed["status"] == "denied_by_policy"
