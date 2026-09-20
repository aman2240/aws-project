"""
Owns composing and sending the Slack DM for a coalesced decision
batch: Block Kit layout (header, mention type, context, the verbatim
mention as a blockquote, the drafted-answer text, a status line, and a
single "✓ Done" button) for each actionable decision in the batch.
Talks to Slack via slack-sdk using SLACK_BOT_TOKEN — but the DM
*target* always comes from the caller (the session's session_init
identity), never from Settings or a hardcoded value here.

The old three-button (Approve/Reject/Join & Answer Live) layout is
gone entirely — see CLAUDE.md's mention-type migration note. Only
DIRECT_REQUEST/ACTION_REQUIRED decisions (decision_detector.py's
is_actionable) ever reach this module; notification_pipeline.py is
what filters to that set before calling send_batch_notification.
"""

import logging

from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from decision_detector import DecisionRecord

logger = logging.getLogger("ghost.slack")

_client = AsyncWebClient(token=settings.slack_bot_token)

# Status -> the label shown on each card's "Status: ..." line. A plain
# dict, not inline conditionals scattered through the block-builder, so
# adding/adjusting a status display is a one-line change in one place.
# rejected/denied_by_policy/answered_live are kept for completeness —
# the new "done"-only button flow (Part D) never itself produces
# "rejected" anymore, but a record could still carry it from elsewhere
# (e.g. a status set before this migration), and denied_by_policy/
# answered_live remain real states other parts of the system set.
STATUS_DISPLAY = {
    "pending": "🟡 Pending",
    "approved": "✅ Done",
    "rejected": "❌ Rejected",
    "denied_by_policy": "🚫 Denied by policy",
    "answered_live": "🎙️ Answered live",
}


def _humanize_mention_type(mention_type: str) -> str:
    """"DIRECT_REQUEST" -> "Direct Request"."""
    return mention_type.replace("_", " ").title()


def _escape_mrkdwn(text: str) -> str:
    """Slack's mrkdwn escaping for the three characters it treats
    specially (&, <, >). context/mention_quote/the drafted answer all
    originate from meeting transcripts and LLM output and must be
    treated as untrusted — the same principle the side panel applies
    (Phase 6: render as literal text, never let it be interpreted as
    markup) — e.g. a literal "<@U123>" in a transcript must render as
    that literal string, not ping a Slack user."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _resolve_channel(slack_target: str) -> str:
    """slack_target may be a Slack user ID (already channel-ready for a
    DM) or an email (needs resolving to a user ID first)."""
    if "@" in slack_target:
        result = await _client.users_lookupByEmail(email=slack_target)
        return result["user"]["id"]
    return slack_target


def _build_blocks(decisions_with_drafts: list[tuple[DecisionRecord, str]]) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "👻 Meeting Ghost — Action Required", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "You were mentioned in the meeting."},
        },
    ]

    for decision, draft in decisions_with_drafts:
        blocks.append({"type": "divider"})

        status_label = STATUS_DISPLAY.get(decision.status, decision.status)
        text = (
            f"🏷️ Type: {_humanize_mention_type(decision.mention_type.value)}\n\n"
            f"📌 *Context*\n{_escape_mrkdwn(decision.context)}\n\n"
            f'🗣️ *Mention*\n> "{_escape_mrkdwn(decision.mention_quote)}"\n\n'
            f"💡 *Suggested reply*\n{_escape_mrkdwn(draft)}\n\n"
            f"Status: {status_label}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

        # The button's value encodes the specific decision_id it
        # applies to — one message can represent a batch of several
        # decisions, so a generic action with no decision reference
        # would be ambiguous.
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✓ Done"},
                        "style": "primary",
                        "action_id": "decision_done",
                        "value": f"{decision.id}:done",
                    }
                ],
            }
        )

    return blocks


async def send_batch_notification(
    slack_target: str,
    decisions_with_drafts: list[tuple[DecisionRecord, str]],
) -> None:
    channel = await _resolve_channel(slack_target)
    blocks = _build_blocks(decisions_with_drafts)

    response = await _client.chat_postMessage(
        channel=channel,
        text=f"Ghost needs your input on {len(decisions_with_drafts)} decision(s).",
        blocks=blocks,
    )
    logger.info(
        "sent Slack notification to %s: %d decision(s), ts=%s",
        slack_target,
        len(decisions_with_drafts),
        response.get("ts"),
    )
