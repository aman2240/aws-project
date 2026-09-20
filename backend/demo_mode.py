"""
Owns the /ws/demo text-injection path: streams a pre-scripted list of
TranscriptEvent objects to the client, one every ~2 seconds, using the
same shared TranscriptEvent model the real ASR path produces. This is
the primary demo path (CLAUDE.md > Demo strategy) — it skips
ASRProvider entirely, so it works even if AWS Transcribe setup is
broken. The session_init handshake and route registration
live in main.py (identical to /ws/transcribe); this module owns the
scripted-send loop, the concurrent control-message loop, and the
scripted data.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from asr_base import TranscriptEvent
from decision_detector import decision_pipeline, handle_control_message

logger = logging.getLogger("ghost.demo")

DEMO_MEETING_ID = "demo-meeting"
SEND_INTERVAL_SECONDS = 2

# A decision triggered by the script's last few lines can still be
# mid-flight (Tier 2 LLM call, Cedar check, answer drafting, then the
# debounce window itself) well after the scripted transcript finishes
# sending — observed ~20s of tail latency in practice. Without this
# grace period, the connection unregisters itself the instant the
# script ends (see the FIRST_COMPLETED wait below), so a decision_batch
# push for anything detected near the end of the script has nowhere to
# go by the time it's ready. Real /ws/transcribe sessions don't have
# this problem since the panel's connection stays open for the whole
# capture, not just a fixed scripted duration.
POST_SCRIPT_GRACE_SECONDS = 30

# Scripted lines live as their own JSON fixture, not inline in this
# module, so the demo script is easy to edit without touching session
# logic. Note fixtures/demo_transcript.json has "Sarah" self-introduce
# and then get asked two decision-shaped questions by name — for the
# demo to visibly detect anything, the session_init identity sent by
# the client needs to include "Sarah" among its watched name variants.
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "demo_transcript.json"


def _load_script() -> list[dict]:
    with _FIXTURE_PATH.open() as f:
        return json.load(f)


async def run_demo_session(websocket: WebSocket, log) -> None:
    log.info("demo session started")
    script = _load_script()

    async def send_script() -> None:
        for line in script:
            event = TranscriptEvent(
                text=line["text"],
                speaker=line["speaker"],
                timestamp=datetime.now(timezone.utc),
                confidence=line.get("confidence", 0.95),
                is_partial=False,
                meeting_id=DEMO_MEETING_ID,
            )
            await websocket.send_json(event.model_dump(mode="json"))
            log.info("sent demo event: %s: %r", event.speaker, event.text)
            await decision_pipeline.process_transcript_event(event)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
        # Keeps this task (and therefore the WebSocket registration)
        # alive past the last scripted line so background decision
        # processing triggered by it has a real chance to reach the
        # panel — see POST_SCRIPT_GRACE_SECONDS above.
        log.info("scripted transcript finished; holding connection open %ss for in-flight decisions", POST_SCRIPT_GRACE_SECONDS)
        await asyncio.sleep(POST_SCRIPT_GRACE_SECONDS)

    async def receive_control_messages() -> None:
        # Lets a speaker_override submitted mid-demo take effect, same
        # as a real /ws/transcribe session — see main.py's receive_loop.
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("ignoring malformed control message: %r", text)
                    continue
                handle_control_message(DEMO_MEETING_ID, payload, log)
        except WebSocketDisconnect:
            log.info("client disconnected")

    send_task = asyncio.create_task(send_script())
    receive_task = asyncio.create_task(receive_control_messages())

    try:
        # Same pattern as main.py's /ws/transcribe: run both
        # concurrently, and whichever finishes first (the scripted
        # transcript ending, or the client disconnecting) cancels the
        # other rather than leaving it running.
        done, pending = await asyncio.wait(
            {send_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        log.info("client disconnected")
    except Exception:
        log.exception("error in demo session")
    finally:
        log.info("demo session ended")
