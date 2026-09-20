"""
Hand-written TranscriptEvent fixtures (not real audio) for
decision_detector tests. Each function returns a fresh list of
TranscriptEvent objects with fabricated timestamps spaced far enough
apart that DecisionPipeline's window-closing logic (elapsed >= 30s)
triggers deterministically, without the test needing to wait in real
wall-clock time for it.
"""

from datetime import datetime, timedelta, timezone

from asr_base import TranscriptEvent

MEETING_ID = "test-meeting"


def _spaced_events(base: datetime, meeting_id: str, lines: list[tuple[str, str]], span: float = 32.0):
    """lines: list of (speaker, text). Spreads timestamps evenly across
    `span` seconds so the last event's elapsed-since-window-start is
    >= 30s, forcing the window to close on it."""
    n = len(lines)
    events = []
    for i, (speaker, text) in enumerate(lines):
        offset = span * i / max(n - 1, 1)
        events.append(
            TranscriptEvent(
                text=text,
                speaker=speaker,
                timestamp=base + timedelta(seconds=offset),
                confidence=0.95,
                is_partial=False,
                meeting_id=meeting_id,
            )
        )
    return events


def throwaway_name_mention() -> list[TranscriptEvent]:
    """(a) Sarah is mentioned, but nothing decision-shaped follows."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base,
        MEETING_ID,
        [
            ("spk_0", "I'm Sarah, thanks for joining everyone."),
            ("spk_1", "Great to have you Sarah, hope the weather's nice where you are."),
            ("spk_0", "Yeah, it's been sunny all week here."),
        ],
    )


def one_clear_decision() -> list[TranscriptEvent]:
    """(b) A single, clear decision-shaped question directed at Sarah."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base,
        MEETING_ID,
        [
            ("spk_0", "I'm Sarah, I'll run today's review."),
            ("spk_1", "Sarah, should we ship on Friday or wait until Monday?"),
        ],
    )


def three_decisions_one_batch_window() -> list[TranscriptEvent]:
    """(c) Three separate windows, each with its own Sarah-directed
    decision, all landing inside one 12s debounce window — should
    produce a single combined batch of 3 records."""
    events = []
    for i, question in enumerate(
        [
            "Sarah, should we raise the rate limit to 1000 requests per minute?",
            "Sarah, should we ship the new auth flow behind a feature flag?",
            "Sarah, should we delay the release to next Tuesday?",
        ]
    ):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i)
        events += _spaced_events(
            base,
            MEETING_ID,
            [
                ("spk_0", "I'm Sarah, let's keep moving."),
                ("spk_1", question),
            ],
        )
    return events


def overlapping_speakers() -> list[TranscriptEvent]:
    """(d) Three distinct speakers in one window; the name-mention +
    question is said by Raj (spk_1), not the last speaker in the
    window (Mike, spk_2) — tests that the DecisionRecord's speaker is
    attributed to whoever raised it, not just the last line."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base,
        MEETING_ID,
        [
            ("spk_0", "I'm Sarah, let's get started."),
            ("spk_1", "This is Raj from infra."),
            ("spk_2", "Hey, I'm Mike, joining a bit late."),
            ("spk_1", "Sarah, should we go with option A or option B for the rollout?"),
            ("spk_2", "I don't have a strong opinion either way."),
        ],
    )


def direct_request_mention() -> list[TranscriptEvent]:
    """(f) A direct question to Sarah needing her input now —
    DIRECT_REQUEST."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base,
        MEETING_ID,
        [
            ("spk_0", "I'm Sarah, go ahead."),
            ("spk_1", "Sarah, should we ship on Friday or wait until Monday?"),
        ],
    )


def action_required_mention() -> list[TranscriptEvent]:
    """(g) Sarah is asked to do something (a task), not asked a direct
    question — ACTION_REQUIRED."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base,
        MEETING_ID,
        [
            ("spk_0", "I'm Sarah, let's keep moving."),
            ("spk_1", "Sarah, can you review the PR before end of day?"),
        ],
    )


def informational_mention() -> list[TranscriptEvent]:
    """(h) Sarah is mentioned only to be kept informed, no input/action
    needed — INFORMATIONAL."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base,
        MEETING_ID,
        [
            ("spk_0", "I'm Sarah, go ahead."),
            ("spk_1", "Just a heads up for Sarah, we finished the migration last night."),
        ],
    )


def reference_mention() -> list[TranscriptEvent]:
    """(i) Sarah's name comes up in passing, attributing a past
    decision to her rather than addressing her directly — REFERENCE."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base,
        MEETING_ID,
        [
            ("spk_0", "I'm Raj from infra."),
            ("spk_1", "Sarah already approved this approach last week, so we're good to go."),
        ],
    )


def restated_question_dedup() -> list[TranscriptEvent]:
    """(e) The same question asked twice, in two separate windows —
    should dedup to a single DecisionRecord."""
    base1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    base2 = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    return _spaced_events(
        base1,
        MEETING_ID,
        [
            ("spk_0", "I'm Sarah, go ahead."),
            ("spk_1", "Sarah, should we bump the rate limit to 1000 requests per minute?"),
        ],
    ) + _spaced_events(
        base2,
        MEETING_ID,
        [
            ("spk_1", "Sarah, just to confirm — should we bump the rate limit to 1000 requests per minute?"),
            ("spk_0", "Let me circle back on that one."),
        ],
    )
