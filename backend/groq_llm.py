"""
Minimal Groq client that mimics just enough of the `anthropic` SDK's
`.messages.create(...)` surface — the
(model, max_tokens, system, messages, ...) -> response.content[i].type/
.text shape — that decision_detector.py and answer_drafter.py don't
need to change their calling code at all, and existing tests' fake LLM
clients (which already mimic this exact shape to avoid real network
calls in tests) keep working completely unchanged. Only the concrete
class constructed by default changes. Replaces bedrock_llm.py (AWS
Bedrock invocation was blocked at the AWS account level — confirmed via
direct empirical testing with every combination of model ID, inference
profile, and auth mechanism (bearer-token API key and SigV4-signed
credentials both got an identical `ValidationException: Operation not
allowed`) — not fixable from application code, so this project moved to
Groq instead rather than chase an AWS Marketplace/billing issue).

Groq's Chat Completions API is OpenAI-compatible: a flat `messages`
array with `system`/`user`/`assistant` roles (no separate top-level
`system` field the way Anthropic's Messages API has one), and a
response shaped as `choices[0].message.content` (a plain string, not a
list of typed content blocks). This client translates between Anthropic's
shape (what the calling code still uses) and Groq's on every call.
"""

import logging
from types import SimpleNamespace

import httpx

from config import settings

logger = logging.getLogger("ghost.groq")

# Confirmed available on this project's own Groq account via GET
# /openai/v1/models (Groq's catalog changes over time — re-check
# console.groq.com/docs/models or that endpoint if this ever 404s
# "model_not_found" again). openai/gpt-oss-20b is a good balance of
# quality and Groq's signature low latency for classification/drafting.
GROQ_MODEL = "openai/gpt-oss-20b"

_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# Backstop only — the real timeout is the asyncio.wait_for(...) wrapper
# already applied at every call site (decision_detector.py,
# answer_drafter.py).
_HTTP_TIMEOUT_SECONDS = 15.0


class GroqMessagesClient:
    """Drop-in stand-in for `anthropic.AsyncAnthropic` from the calling
    code's point of view — implements only the one method/shape this
    project actually uses, nothing more."""

    class _Messages:
        async def create(
            self, *, model: str = GROQ_MODEL, max_tokens, system, messages, output_config=None, **_ignored
        ):
            # Anthropic shape (system as its own top-level field) ->
            # OpenAI/Groq shape (system folded into the messages array
            # as its own role, first).
            groq_messages = [{"role": "system", "content": system}, *messages]

            body = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": groq_messages,
            }
            # Only requested when the caller asked for structured JSON
            # (decision_detector.py's Tier 2 classification passes
            # output_config; answer_drafter.py's plain-prose drafting
            # call doesn't). Groq's json_object mode requires the word
            # "JSON" to appear somewhere in the prompt, which Tier 2's
            # _SYSTEM_PROMPT already does ("Respond with a single JSON
            # object").
            if output_config is not None:
                body["response_format"] = {"type": "json_object"}

            headers = {
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(_GROQ_ENDPOINT, json=body, headers=headers)
            if response.status_code != 200:
                logger.warning(
                    "Groq chat completion returned %s: %s", response.status_code, response.text[:500]
                )
            response.raise_for_status()
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    def __init__(self) -> None:
        self.messages = self._Messages()
