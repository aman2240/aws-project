"""
Owns decision detection: DecisionRecord (the shape Phase 4/5 consume),
SpeakerMapper (self-intro name inference, plus manual overrides),
and DecisionPipeline — the per-session buffering/two-tier-trigger/
dedup/debounce state machine that turns a stream of final
TranscriptEvents into batched decisions.

Still does not call Slack or Cedar directly. DecisionPipeline persists
each accepted decision via an injected DecisionStoreLike (structural
typing — no import of decision_store.py, to avoid a cycle) and hands
finished batches to an injected on_decision_batch callback; both are
wired in by main.py at startup. default_on_decision_batch remains the
placeholder default (console + decisions_log.jsonl) for anything that
doesn't wire in the real notification pipeline.

Also owns per-session identity: set_watch_names/set_slack_target are
populated from the session_init WebSocket handshake (Part A, in
main.py), and handle_control_message() routes later speaker_override
messages to SpeakerMapper — both /ws/transcribe and /ws/demo call it.
"""

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from asr_base import TranscriptEvent
from config import settings
from groq_llm import GROQ_MODEL, GroqMessagesClient

logger = logging.getLogger("ghost.decisions")

# --- Data model -------------------------------------------------------

DecisionBatchCallback = Callable[[str, "list[DecisionRecord]"], Awaitable[None]]


class MentionType(str, Enum):
    """How a watched-name mention classifies. Replaces the old
    is_decision boolean (see is_actionable below) — this is the one
    signal that determines both whether a mention is actionable and,
    via its human-readable form, what shows on the Slack card."""

    DIRECT_REQUEST = "DIRECT_REQUEST"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    INFORMATIONAL = "INFORMATIONAL"
    REFERENCE = "REFERENCE"
    NO_ACTION = "NO_ACTION"


def is_actionable(mention_type: MentionType) -> bool:
    """True for mention types that proceed to the debounce/batch window
    and notification pipeline; False for ones that are still persisted
    via decision_store.create() (for search/history) but never notified.
    This is the sole actionability signal now — there is no separate
    is_decision field to potentially disagree with it."""
    return mention_type in (MentionType.DIRECT_REQUEST, MentionType.ACTION_REQUIRED)


class DecisionRecord(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    meeting_id: str
    decision_text: str
    speaker: str
    requires_action_from: str | None
    context: str
    # The exact verbatim sentence containing the mention — distinct
    # from decision_text (a one-sentence summary) and context
    # (surrounding detail); required, since Tier 2 always produces one
    # alongside mention_type.
    mention_type: MentionType
    mention_quote: str
    confidence: float
    urgency: Literal["low", "medium", "high"]
    timestamp: datetime
    status: str = "pending"


# --- Speaker mapping (self-introductions) ------------------------------

_I_AM_RE = re.compile(r"\bI['’]?m\s+([A-Z][a-zA-Z'-]*(?:\s+[A-Z][a-zA-Z'-]*)?)")
# Scoped (?i:) on "this is" only — a bare re.IGNORECASE would also
# weaken the [A-Z] capital-letter check in the capture group, letting
# it swallow a following lowercase word (e.g. "This is Raj from infra"
# capturing "Raj from" instead of just "Raj").
_THIS_IS_RE = re.compile(r"(?i:this is)\s+([A-Z][a-zA-Z'-]*(?:\s+[A-Z][a-zA-Z'-]*)?)")


def _extract_self_intro_name(text: str) -> str | None:
    for pattern in (_I_AM_RE, _THIS_IS_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


class SpeakerMapper:
    """Maps raw speaker labels (spk_0, ...) to names inferred from
    self-introductions in the first ~10 transcript events of a
    session. Falls back to the raw label when no introduction was
    found for that speaker."""

    SCAN_LIMIT = 10

    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}
        self._events_scanned = 0

    def observe(self, event: TranscriptEvent) -> None:
        if self._events_scanned >= self.SCAN_LIMIT:
            return
        self._events_scanned += 1
        if event.speaker in self._mapping:
            return
        name = _extract_self_intro_name(event.text)
        if name:
            self._mapping[event.speaker] = name

    def resolve(self, speaker_label: str) -> str:
        return self._mapping.get(speaker_label, speaker_label)

    def set_mapping(self, speaker_label: str, name: str) -> None:
        """Manual override — for a future side-panel manual-entry option."""
        self._mapping[speaker_label] = name


def _build_name_pattern(names: list[str]) -> re.Pattern | None:
    escaped = [re.escape(n.strip()) for n in names if n.strip()]
    if not escaped:
        return None
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


# --- Tier 2: LLM classification ----------------------------------------

# Claude 3 Haiku (named in CLAUDE.md) is retired; calls now go through
# Groq (groq_llm.py) — AWS Bedrock was tried and blocked at the AWS
# account level (see groq_llm.py's docstring), and the direct Anthropic
# API before that. LLM_MODEL holds Groq's own model name. Public (no
# leading underscore) — answer_drafter.py's second LLM call reuses both
# this and LLM_TIMEOUT_SECONDS.
LLM_MODEL = GROQ_MODEL
LLM_TIMEOUT_SECONDS = 4.0  # Phase 0 latency benchmark; 4s default per spec

_SYSTEM_PROMPT = """You are analyzing a short window of a live meeting transcript to classify how a watched person was mentioned.

Respond with a single JSON object:
- mention_type (string): exactly one of "DIRECT_REQUEST", "ACTION_REQUIRED", "INFORMATIONAL", "REFERENCE", "NO_ACTION".
  - DIRECT_REQUEST: the person is directly asked a question or asked to decide something, needing their input right now.
  - ACTION_REQUIRED: the person is asked or expected to do something (a task, a review, a follow-up), even if not phrased as a question.
  - INFORMATIONAL: the person is mentioned only to be kept informed — no action or input is actually needed from them.
  - REFERENCE: the person's name comes up in passing (e.g. attributing a past decision to them, or someone else referring to something they said) — they are not being addressed directly.
  - NO_ACTION: small talk, a greeting, or any other mention with nothing decision-relevant attached.
- mention_quote (string): the exact verbatim sentence from the transcript containing the mention — copy it exactly, do not paraphrase.
- requires_action_from (string or null): the name of the person whose input/action is needed, if identifiable from the transcript; null otherwise.
- decision_text (string): a one-sentence statement of the decision, question, or request — your best-effort summary even for non-actionable mention types.
- context (string): 2-3 sentences of surrounding context useful for drafting an answer later.
- confidence (number 0.0-1.0): how confident you are in this classification.
- urgency (string): "low", "medium", or "high"."""

_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "mention_type": {
            "type": "string",
            "enum": ["DIRECT_REQUEST", "ACTION_REQUIRED", "INFORMATIONAL", "REFERENCE", "NO_ACTION"],
        },
        "mention_quote": {"type": "string"},
        "requires_action_from": {"type": ["string", "null"]},
        "decision_text": {"type": "string"},
        "context": {"type": "string"},
        "confidence": {"type": "number"},
        "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "mention_type",
        "mention_quote",
        "requires_action_from",
        "decision_text",
        "context",
        "confidence",
        "urgency",
    ],
    "additionalProperties": False,
}


class _LLMClassification(BaseModel):
    # Pydantic rejects an unrecognized mention_type string with a
    # ValidationError automatically (it's an Enum-typed field) — caught
    # by _classify_window's existing (json.JSONDecodeError,
    # ValidationError) handler, same fail-soft log-and-skip pattern
    # already used for any other malformed LLM output here.
    mention_type: MentionType
    mention_quote: str
    requires_action_from: str | None
    decision_text: str
    context: str
    confidence: float
    urgency: Literal["low", "medium", "high"]


# --- Deduplication -------------------------------------------------------

_DEDUP_LEDGER_SIZE = 10
_DEDUP_OVERLAP_THRESHOLD = 0.6


def _is_duplicate(candidate_text: str, recent_texts: Iterable[str]) -> bool:
    candidate_words = set(re.findall(r"\w+", candidate_text.lower()))
    if not candidate_words:
        return False
    for prior_text in recent_texts:
        prior_words = set(re.findall(r"\w+", prior_text.lower()))
        if not prior_words:
            continue
        overlap = len(candidate_words & prior_words) / len(candidate_words | prior_words)
        if overlap >= _DEDUP_OVERLAP_THRESHOLD:
            return True
    return False


# --- Default output callback (Phase 4 replaces this) --------------------

_DEFAULT_LOG_PATH = Path(__file__).parent / "decisions_log.jsonl"


async def default_on_decision_batch(meeting_id: str, decisions: "list[DecisionRecord]") -> None:
    logger.info("[meeting_id=%s] decision batch ready: %d decision(s)", meeting_id, len(decisions))
    for record in decisions:
        logger.info(
            "[meeting_id=%s] - %s (requires_action_from=%s, urgency=%s, confidence=%.2f)",
            meeting_id,
            record.decision_text,
            record.requires_action_from,
            record.urgency,
            record.confidence,
        )
    # Placeholder persistence — Phase 4 replaces this callback with the
    # real Slack/Cedar integration; nothing else in this file changes.
    with _DEFAULT_LOG_PATH.open("a") as f:
        for record in decisions:
            f.write(record.model_dump_json() + "\n")


def _log_task_exception(task: "asyncio.Task") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("decision pipeline background task failed: %r", exc)


# --- Decision store (structural typing — no import of decision_store.py,
# to avoid a cycle; decision_store.py needs DecisionRecord from here) ------


class DecisionStoreLike(Protocol):
    async def create(self, decision: "DecisionRecord") -> None: ...


# --- Per-session state ----------------------------------------------------

_WINDOW_SECONDS = 30
_MAX_WINDOW_SEGMENTS = 10
_CARRY_FORWARD_LINES = 3


@dataclass
class _SessionState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    buffer: list = field(default_factory=list)
    window_start: datetime | None = None
    speaker_mapper: SpeakerMapper = field(default_factory=SpeakerMapper)
    watch_names_pattern: re.Pattern | None = None
    recent_decisions: deque = field(default_factory=lambda: deque(maxlen=_DEDUP_LEDGER_SIZE))
    pending_batch: list = field(default_factory=list)
    batch_timer_task: "asyncio.Task | None" = None
    # Set from the session_init WebSocket handshake (Part A) — never
    # from the process-wide Settings object, which would only support
    # one watched user per backend instance.
    slack_target: str | None = None


# --- Pipeline --------------------------------------------------------------


class DecisionPipeline:
    """Holds per-meeting_session_id state — never global/module-level
    state, so concurrent meetings never share a buffer, batch, or
    speaker map. process_transcript_event is the entry point, called
    once per final TranscriptEvent."""

    def __init__(
        self,
        on_decision_batch: DecisionBatchCallback = default_on_decision_batch,
        llm_client: "GroqMessagesClient | None" = None,
        decision_store: "DecisionStoreLike | None" = None,
    ) -> None:
        self.on_decision_batch = on_decision_batch
        self._llm_client = llm_client or GroqMessagesClient()
        self._decision_store = decision_store
        self._sessions: dict[str, _SessionState] = {}
        # Keeps references to in-flight Tier-2/batch tasks so they
        # aren't garbage-collected mid-flight, and so failures surface
        # via _log_task_exception instead of vanishing silently.
        self._background_tasks: set[asyncio.Task] = set()

    def _get_or_create_session(self, meeting_id: str) -> _SessionState:
        # No `await` anywhere in this lookup/insert, so it's atomic
        # w.r.t. the event loop — safe without an extra lock even
        # under concurrent meetings.
        state = self._sessions.get(meeting_id)
        if state is None:
            state = _SessionState()
            self._sessions[meeting_id] = state
        return state

    def set_watch_names(self, meeting_id: str, names: list[str]) -> None:
        """Configures the Tier-1 name-variant list (e.g. full name +
        first name) for this session. Without this, Tier 1 never
        matches and no decisions are ever detected for that meeting."""
        state = self._get_or_create_session(meeting_id)
        state.watch_names_pattern = _build_name_pattern(names)

    def set_speaker_name(self, meeting_id: str, speaker_label: str, name: str) -> None:
        """Manual speaker-mapping override — driven by the side panel's
        speaker-override input (Part A), routed here via
        handle_control_message()."""
        state = self._get_or_create_session(meeting_id)
        state.speaker_mapper.set_mapping(speaker_label, name)

    def set_slack_target(self, meeting_id: str, slack_target: str) -> None:
        """Set from the session_init handshake — the Slack user ID or
        email this session's notifications should go to."""
        state = self._get_or_create_session(meeting_id)
        state.slack_target = slack_target

    def get_slack_target(self, meeting_id: str) -> str | None:
        state = self._sessions.get(meeting_id)
        return state.slack_target if state else None

    def set_decision_store(self, decision_store: "DecisionStoreLike") -> None:
        """Wired in by main.py at startup, not imported here directly —
        decision_store.py needs DecisionRecord from this module, so this
        module stays independent of decision_store.py to avoid a cycle."""
        self._decision_store = decision_store

    def _track(self, coro: "Awaitable") -> "asyncio.Task":
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_log_task_exception)
        return task

    async def process_transcript_event(self, event: TranscriptEvent) -> None:
        if event.is_partial:
            return

        state = self._get_or_create_session(event.meeting_id)

        async with state.lock:
            state.speaker_mapper.observe(event)
            state.buffer.append(event)
            if state.window_start is None:
                state.window_start = event.timestamp

            elapsed = (event.timestamp - state.window_start).total_seconds()
            window_ready = elapsed >= _WINDOW_SECONDS or len(state.buffer) >= _MAX_WINDOW_SEGMENTS
            if not window_ready:
                return

            window_events = state.buffer
            # Carry the tail forward into the next window's buffer
            # (not a side context list) so a name mention or question
            # right at the boundary is still visible to Tier 1/Tier 2
            # next time, instead of being lost.
            state.buffer = window_events[-_CARRY_FORWARD_LINES:]
            state.window_start = None

        # Tier 1: fast regex check on a local snapshot, outside the
        # lock — pure computation, no need to hold up other events.
        window_text = " ".join(e.text for e in window_events)
        if not state.watch_names_pattern or not state.watch_names_pattern.search(window_text):
            return

        # Tier 2 (the LLM call) can take up to LLM_TIMEOUT_SECONDS to
        # resolve. Run it in the background rather than awaiting it
        # here, so the transcript stream is never held up waiting on
        # a slow LLM response.
        self._track(self._classify_and_batch(event.meeting_id, state, window_events))

    async def _classify_window(
        self, meeting_id: str, window_events: list
    ) -> "_LLMClassification | None":
        transcript_text = "\n".join(f"{e.speaker}: {e.text}" for e in window_events)

        try:
            response = await asyncio.wait_for(
                self._llm_client.messages.create(
                    model=LLM_MODEL,
                    max_tokens=1024,
                    system=_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": transcript_text}],
                    output_config={"format": {"type": "json_schema", "schema": _CLASSIFICATION_SCHEMA}},
                ),
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[meeting_id=%s] LLM classification timed out after %.1fs; treating window as not actionable",
                meeting_id,
                LLM_TIMEOUT_SECONDS,
            )
            return None
        except Exception:
            logger.exception(
                "[meeting_id=%s] LLM classification call failed; treating window as not actionable",
                meeting_id,
            )
            return None

        raw_text = next((block.text for block in response.content if block.type == "text"), "")

        try:
            data = json.loads(raw_text)
            classification = _LLMClassification.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            # A single malformed response — including an unrecognized
            # mention_type string, which Pydantic rejects as a
            # ValidationError since it's Enum-typed — must never crash
            # the pipeline. Log the raw output and move on.
            logger.warning(
                "[meeting_id=%s] failed to parse LLM output (%s); raw output: %r",
                meeting_id,
                exc,
                raw_text,
            )
            return None

        # No more is_decision gate here — mention_type is always
        # present (NO_ACTION covers "nothing decision-relevant"), and
        # every above-threshold classification is stored regardless of
        # type; only actionability (checked in _classify_and_batch)
        # gates notification.
        return classification

    async def _classify_and_batch(
        self, meeting_id: str, state: _SessionState, window_events: list
    ) -> None:
        classification = await self._classify_window(meeting_id, window_events)
        if classification is None:
            return

        if classification.confidence < settings.confidence_threshold:
            logger.info(
                "[meeting_id=%s] mention below confidence threshold (%.2f < %.2f): %r",
                meeting_id,
                classification.confidence,
                settings.confidence_threshold,
                classification.decision_text,
            )
            return

        matched_event = next(
            (e for e in reversed(window_events) if state.watch_names_pattern.search(e.text)),
            window_events[-1],
        )

        record = DecisionRecord(
            meeting_id=meeting_id,
            decision_text=classification.decision_text,
            speaker=state.speaker_mapper.resolve(matched_event.speaker),
            requires_action_from=classification.requires_action_from,
            context=classification.context,
            mention_type=classification.mention_type,
            mention_quote=classification.mention_quote,
            confidence=classification.confidence,
            urgency=classification.urgency,
            timestamp=matched_event.timestamp,
        )

        # Storage is unconditional once above the confidence threshold,
        # regardless of mention_type — actionability and dedup (below)
        # only gate the notification path, not persistence, so every
        # mention stays searchable/historical.
        if self._decision_store is not None:
            try:
                await self._decision_store.create(record)
            except Exception:
                logger.exception("[meeting_id=%s] failed to persist decision %s", meeting_id, record.id)

        if not is_actionable(record.mention_type):
            logger.info(
                "[meeting_id=%s] stored, not notified: %s (%r)",
                meeting_id,
                record.mention_type.value,
                record.decision_text,
            )
            return

        async with state.lock:
            if _is_duplicate(record.decision_text, state.recent_decisions):
                logger.info("[meeting_id=%s] deduped decision: %r", meeting_id, record.decision_text)
                return

            state.recent_decisions.append(record.decision_text)
            state.pending_batch.append(record)

            # Fixed window, not resettable: only start the timer if one
            # isn't already running for this session. A steady stream
            # of qualifying decisions must not be able to keep pushing
            # the notification back indefinitely.
            if state.batch_timer_task is None or state.batch_timer_task.done():
                state.batch_timer_task = self._track(self._fire_batch_after_delay(meeting_id, state))

    async def _fire_batch_after_delay(self, meeting_id: str, state: _SessionState) -> None:
        await asyncio.sleep(settings.debounce_window_seconds)

        async with state.lock:
            batch = state.pending_batch
            state.pending_batch = []
            state.batch_timer_task = None

        if batch:
            await self.on_decision_batch(meeting_id, batch)


decision_pipeline = DecisionPipeline()


def handle_control_message(meeting_id: str, payload: dict, log: logging.Logger) -> None:
    """Dispatches a parsed WebSocket control-message payload for this
    session — currently just speaker_override. session_init itself is
    handled by the handshake in main.py before the session starts, not
    here. Shared by /ws/transcribe and /ws/demo (both call this — see
    main.py and demo_mode.py) so the routing logic lives in one place."""
    msg_type = payload.get("type")
    if msg_type == "speaker_override":
        label = payload.get("label")
        name = payload.get("name")
        if label and name:
            decision_pipeline.set_speaker_name(meeting_id, label, name)
            log.info("speaker override applied: %s -> %s", label, name)
        else:
            log.warning("malformed speaker_override message: %r", payload)
    else:
        log.warning("unknown control message type: %r", payload)
