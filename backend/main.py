"""
FastAPI app entrypoint. Importing `config` here first means a missing
required credential raises immediately on startup, not on first use.
Also where the notification pipeline (Cedar + drafting + Slack) is
wired into Phase 3's DecisionPipeline, replacing its placeholder
console-logging callback.
"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import database
import opensearch_client
import session_connections
from asr_base import MeetingLoggerAdapter
from config import settings  # noqa: F401  (import triggers validation)
from decision_detector import decision_pipeline, handle_control_message
from decision_store import decision_store
from demo_mode import DEMO_MEETING_ID, run_demo_session
from notification_pipeline import make_notification_pipeline
from slack_webhook import router as slack_webhook_router
from transcribe_handler import get_asr_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ghost.main")

# Real notification pipeline (Cedar-gate -> draft -> Slack) replaces
# Phase 3's placeholder console logger; every accepted decision is
# also persisted via decision_store regardless of the callback.
decision_pipeline.on_decision_batch = make_notification_pipeline(decision_store)
decision_pipeline.set_decision_store(decision_store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # `async with engine.begin()` needs a running event loop, so schema
    # creation happens here rather than at plain module-import time.
    # No Alembic/migrations — hackathon schema, create once and move on.
    await database.create_all()
    # No-ops (logs and returns) if OPENSEARCH_HOST isn't set, or if
    # OpenSearch isn't reachable — never blocks app startup on it.
    await opensearch_client.ensure_index()
    yield
    await opensearch_client.close()


app = FastAPI(title="AI Meeting Ghost", lifespan=lifespan)
app.include_router(slack_webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/decisions")
async def list_decisions(meeting_id: str) -> dict:
    # Hydrates the side panel's decision list on open/reopen — live
    # updates after that arrive via the decision_batch/
    # decision_status_update WebSocket pushes (notification_pipeline.py,
    # slack_webhook.py), not by polling this. Historical decisions
    # here never carry drafted_answer (never persisted — see Phase 4);
    # only decisions pushed live while this panel is open do.
    decisions = await decision_store.get_recent(meeting_id, limit=200)
    return {"decisions": [d.model_dump(mode="json") for d in decisions]}


@app.get("/search")
async def search(q: str, session: AsyncSession = Depends(database.get_session)) -> dict:
    try:
        results = await opensearch_client.search_decisions(q)
        return {"source": "opensearch", "results": results}
    except Exception:
        logger.warning(
            "OpenSearch search failed or unavailable; falling back to Postgres", exc_info=True
        )

    pattern = f"%{q}%"
    result = await session.execute(
        select(database.Decision).where(
            or_(
                database.Decision.decision_text.like(pattern),
                database.Decision.approved_by.like(pattern),
                database.Decision.speaker.like(pattern),
            )
        )
    )
    rows = result.scalars().all()
    results = [
        {
            "id": row.id,
            "meeting_id": row.meeting_id,
            "decision_text": row.decision_text,
            "context": row.context,
            "speaker": row.speaker,
            "requires_action_from": row.requires_action_from,
            "urgency": row.urgency,
            "status": row.status,
            "approved_by": row.approved_by,
            "confidence": row.confidence,
        }
        for row in rows
    ]
    return {"source": "sqlite_fallback", "results": results}


async def _reject_missing_session_init(websocket: WebSocket) -> None:
    await websocket.send_json(
        {
            "type": "error",
            "message": (
                "First message must be a session_init control message with "
                "watched_user_name_variants and slack_target"
            ),
        }
    )
    await websocket.close(code=1008)


async def _handshake_session_init(websocket: WebSocket, log) -> dict | None:
    """Reads the mandatory session_init control message that must be the
    first frame from the client — before any audio bytes on
    /ws/transcribe, before the scripted transcript on /ws/demo. Returns
    its data, or None after already sending an error and closing the
    socket. No silent fallback to a hardcoded name: an old/mismatched
    client is a bug that must fail loudly, not quietly misattribute
    every decision to nobody in particular."""
    try:
        init_data = await websocket.receive_json()
    except Exception:
        log.warning("no session_init received as first message; closing")
        await _reject_missing_session_init(websocket)
        return None

    if not isinstance(init_data, dict) or init_data.get("type") != "session_init":
        log.warning("first message was not session_init: %r", init_data)
        await _reject_missing_session_init(websocket)
        return None

    names = init_data.get("watched_user_name_variants")
    slack_target = init_data.get("slack_target")
    if not names or not isinstance(names, list) or not slack_target:
        log.warning("session_init missing required fields: %r", init_data)
        await _reject_missing_session_init(websocket)
        return None

    return {"watched_user_name_variants": names, "slack_target": slack_target}


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket, session_id: str | None = None):
    await websocket.accept()

    # A supplied session_id is trusted as-is: there's no server-side
    # session registry yet (that's Phase 5's job), so "recognized" here
    # just means "the client gave us one to reuse". Reusing it always
    # starts a NEW provider stream under that SAME meeting_session_id —
    # an AWS Transcribe streaming connection can't literally be resumed
    # once dropped, only re-opened under the same logical session.
    meeting_session_id = session_id or str(uuid.uuid4())
    if not session_id:
        await websocket.send_json({"meeting_session_id": meeting_session_id})

    log = MeetingLoggerAdapter(logger, {"meeting_session_id": meeting_session_id})

    init = await _handshake_session_init(websocket, log)
    if init is None:
        return

    decision_pipeline.set_watch_names(meeting_session_id, init["watched_user_name_variants"])
    decision_pipeline.set_slack_target(meeting_session_id, init["slack_target"])
    log.info(
        "session_init received: names=%s slack_target=%s",
        init["watched_user_name_variants"],
        init["slack_target"],
    )

    # Registered so notification_pipeline.py/slack_webhook.py can push
    # decision_batch/decision_status_update down this same connection —
    # see session_connections.py.
    session_connections.register(meeting_session_id, websocket)

    try:
        provider = await get_asr_provider(meeting_session_id, log)
    except Exception:
        log.exception("failed to start any ASR provider")
        session_connections.unregister(meeting_session_id, websocket)
        await websocket.close(code=1011)
        return

    log.info("ASR provider started (%s)", type(provider).__name__)

    async def receive_loop() -> None:
        # Audio (binary) and control messages like speaker_override
        # (text/JSON) can now arrive interleaved on the same socket, so
        # this inspects each frame's type via the raw receive() rather
        # than assuming every frame is audio (Phase 2's simpler
        # receive_bytes()-only loop, now obsolete).
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    await provider.send_audio(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("ignoring malformed control message: %r", text)
                    continue
                handle_control_message(meeting_session_id, payload, log)
        except WebSocketDisconnect:
            log.info("client disconnected")

    async def transcript_loop() -> None:
        async for event in provider.events():
            if event.is_partial:
                # Partial results are dropped in this phase — only
                # final results continue downstream.
                continue
            # Logged at INFO specifically so a live session's server
            # log gives a direct answer to "is Transcribe actually
            # hearing anything, and does it contain the watched name" —
            # without this, a session that never triggers a decision is
            # indistinguishable between "nothing was transcribed" and
            # "things were transcribed but never matched Tier 1/Tier 2".
            log.info("transcript: %s: %r (confidence=%.2f)", event.speaker, event.text, event.confidence)
            await websocket.send_json(event.model_dump(mode="json"))
            # process_transcript_event only ever blocks synchronously
            # on cheap buffering/regex work — the LLM call it may
            # trigger runs as an internally-managed background task,
            # so this never delays the transcript stream above.
            await decision_pipeline.process_transcript_event(event)

    receive_task = asyncio.create_task(receive_loop())
    transcript_task = asyncio.create_task(transcript_loop())

    try:
        # Client->provider and provider->client are independent,
        # simultaneous streams; run them concurrently so neither blocks
        # the other. asyncio.gather alone won't cancel a still-running
        # sibling when the other finishes first, so whichever side
        # finishes first (client disconnect, or the provider stream
        # ending/erroring) explicitly cancels the other rather than
        # leaving it waiting on a session nobody's using anymore.
        done, pending = await asyncio.wait(
            {receive_task, transcript_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    except Exception:
        log.exception("error in transcribe session")
    finally:
        # Guaranteed even on exception or abrupt disconnect, so the
        # AWS Transcribe connection never leaks.
        await provider.stop()
        session_connections.unregister(meeting_session_id, websocket)
        log.info("ASR provider stopped")


@app.websocket("/ws/demo")
async def ws_demo(websocket: WebSocket):
    await websocket.accept()
    log = MeetingLoggerAdapter(logger, {"meeting_session_id": DEMO_MEETING_ID})

    init = await _handshake_session_init(websocket, log)
    if init is None:
        return

    decision_pipeline.set_watch_names(DEMO_MEETING_ID, init["watched_user_name_variants"])
    decision_pipeline.set_slack_target(DEMO_MEETING_ID, init["slack_target"])
    log.info(
        "session_init received: names=%s slack_target=%s",
        init["watched_user_name_variants"],
        init["slack_target"],
    )

    session_connections.register(DEMO_MEETING_ID, websocket)
    try:
        await run_demo_session(websocket, log)
    finally:
        session_connections.unregister(DEMO_MEETING_ID, websocket)
