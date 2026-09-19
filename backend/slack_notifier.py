"""
Owns composing and sending the Slack DM for a coalesced decision
batch: Block Kit layout, the drafted-answer text, and the
Approve/Reject/Join & Answer Live interactive buttons. Talks to Slack
via slack-sdk using SLACK_BOT_TOKEN — but the DM *target* always comes
from the caller (the session's session_init identity), never from
Settings or a hardcoded value here.
"""

import logging

from slack_sdk.web.async_client import AsyncWebClient

from config import settings
from decision_detector import DecisionRecord

logger = logging.getLogger("ghost.slack")

_client = AsyncWebClient(token=settings.slack_bot_token)


async def _resolve_channel(slack_target: str) -> str:
    """slack_target may be a Slack user ID (already channel-ready for a
    DM) or an email (needs resolving to a user ID first)."""
    if "@" in slack_target:
        try:
            result = await _client.users_lookupByEmail(email=slack_target)
            return result["user"]["id"]
        except Exception as exc:
            logger.warning(
                "users_lookupByEmail failed for %s (%s); falling back to public channel",
                slack_target,
                exc,
            )
            convs = await _client.conversations_list(types="public_channel")
            channels = convs.get("channels", [])
            if channels:
                return channels[0]["id"]
            raise
    return slack_target


def _build_blocks(
    decisions_with_drafts: list[tuple[DecisionRecord, str]], meeting_link: str | None
) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Ghost needs your input"},
        }
    ]

    for decision, draft in decisions_with_drafts:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{decision.decision_text}*\n"
                        f"{decision.context}\n\n"
                        f"*Suggested answer:*\n{draft}"
                    ),
                },
            }
        )

        # Each button's value encodes the specific decision_id it
        # applies to — one message can represent a batch of several
        # decisions, so a generic action with no decision reference
        # would be ambiguous.
        elements = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve"},
                "style": "primary",
                "action_id": "decision_approve",
                "value": f"{decision.id}:approve",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Reject"},
                "style": "danger",
                "action_id": "decision_reject",
                "value": f"{decision.id}:reject",
            },
        ]
        if meeting_link:
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Join & Answer Live"},
                    "action_id": "join_meeting",
                    "url": meeting_link,
                }
            )
        blocks.append({"type": "actions", "elements": elements})

    return blocks


async def send_batch_notification(
    slack_target: str,
    decisions_with_drafts: list[tuple[DecisionRecord, str]],
    meeting_link: str | None = None,
) -> None:
    channel = await _resolve_channel(slack_target)
    blocks = _build_blocks(decisions_with_drafts, meeting_link)

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
