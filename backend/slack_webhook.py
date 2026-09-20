"""
Owns the inbound side of Slack: the FastAPI route that receives
interactivity payloads (button clicks — just "✓ Done" now, see
CLAUDE.md's mention-type migration note) from Slack's request URL,
verifies the request signature against SLACK_SIGNING_SECRET using the
raw body, and responds immediately — the actual decision_store update
runs afterward via BackgroundTasks so Slack never times out waiting and
retries the same click. That same background task also pushes the
update to the side panel's WebSocket connection, so clicking Done from
Slack is reflected live there too, not just in Slack.
"""

import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Request, Response
from slack_sdk.signature import SignatureVerifier

import session_connections
from config import settings
from decision_store import decision_store

logger = logging.getLogger("ghost.slack_webhook")

router = APIRouter()
_verifier = SignatureVerifier(settings.slack_signing_secret)


@router.post("/slack/interaction")
async def slack_interaction(request: Request, background_tasks: BackgroundTasks) -> Response:
    # Signature verification needs the exact raw bytes Slack signed —
    # parsing as form data first would alter what request.body() later
    # returns, breaking verification. Read and verify before touching
    # the parsed form.
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verifier.is_valid(body=raw_body, timestamp=timestamp, signature=signature):
        logger.warning("rejected Slack interaction: invalid request signature")
        return Response(status_code=401)

    form = await request.form()
    payload_raw = form.get("payload")
    if not payload_raw:
        logger.warning("rejected Slack interaction: missing payload field")
        return Response(status_code=400)

    try:
        payload = json.loads(payload_raw)
        action = payload["actions"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        logger.warning("malformed Slack interaction payload: %r", payload_raw)
        return Response(status_code=400)

    action_id = action.get("action_id")
    value = action.get("value", "")
    user_id = payload.get("user", {}).get("id", "unknown")

    if action_id != "decision_done" or ":" not in value:
        logger.warning("unrecognized Slack interaction: action_id=%r value=%r", action_id, value)
        return Response(status_code=200)

    decision_id_str, status_word = value.split(":", 1)
    try:
        decision_id = uuid.UUID(decision_id_str)
    except ValueError:
        logger.warning("malformed decision id in button value: %r", value)
        return Response(status_code=200)

    if status_word != "done":
        logger.warning("unrecognized status word in button value: %r", value)
        return Response(status_code=200)

    # "done" reuses the existing "approved" status value rather than
    # adding a new one — the Slack card's own text/emoji already
    # communicates "Done" to the human, so there's no real benefit to
    # an ALTER TYPE migration on the status enum for this.
    status = "approved"

    # Respond to Slack first; update afterward so the click is never
    # held up on (or retried because of) a slow store write.
    background_tasks.add_task(_update_status_and_notify_panel, decision_id, status, user_id)

    return Response(status_code=200)


async def _update_status_and_notify_panel(decision_id: uuid.UUID, status: str, user_id: str) -> None:
    updated = await decision_store.update_status(decision_id, status, user_id)
    if updated is None:
        return
    await session_connections.push(
        updated.meeting_id,
        {
            "type": "decision_status_update",
            "decision_id": str(decision_id),
            "status": status,
            "approved_by": user_id,
        },
    )
