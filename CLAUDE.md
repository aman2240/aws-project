# AI Meeting Ghost

A Chrome extension that listens to a Google Meet call the user has open, detects when a decision or question needs their input, and notifies them on Slack with a drafted answer they can approve, edit, or override.

Built for a 48-hour hackathon. Optimize for a working, rehearsed demo over completeness.

## Scope (locked — do not expand without asking)

- **Platform:** Google Meet only. No Zoom, no Teams.
- **Notifications:** Slack DMs only. No Chrome desktop alerts.
- **ASR:** AWS Transcribe Streaming — sole provider, no fallback.
- **Demo strategy:** Text-injection (pre-scripted fake transcripts via `/ws/demo`) is the primary demo path. Live audio through a real Meet call is a bonus secondary path — if it's flaky, fall back to text-injection without hesitation.
- **Confidence threshold:** Hardcoded at `0.7`. No user-facing "trust dial" slider in v1.
- **Audio processing:** `ScriptProcessorNode`, not `AudioWorklet` — simpler for the timeline, fine for a demo.
- **Speaker mapping:** Infer names from self-introductions ("I'm ___" / "This is ___"), fall back to generic labels (`spk_0`, `spk_1`) otherwise.
- **Diarization:** Use Transcribe's speaker-label diarization for a single mixed stream. Do **not** use channel identification — that requires true stereo with one speaker per channel, which tab audio isn't.

## Key assumption to hold consistently

Ghost does not dial into a meeting unattended. "Can't attend" means a muted browser tab stays open with the extension running — the extension makes that tab useful, it doesn't join a call on its own. Don't write code or copy that implies autonomous meeting-joining.

## Non-negotiable behaviors

- **No echo/feedback:** processed audio must route to a silent sink, never `audioContext.destination`.
- **Debounce before notifying:** hold the first detected decision for a 10–15s coalescing window; batch anything else that lands in it into one Slack DM, not one per detection.
- **Drafted answer required:** before sending the Slack DM, query past decisions (OpenSearch, or Postgres early on) for relevant context and draft a suggested answer via a second LLM call. This is the core differentiator — don't ship a plain notifier.
- **Cedar policy check gates every notification.** "Deny" means log the decision, send nothing.
- **Stop condition:** manual "Stop Ghost" control in the side panel, plus detecting the Meet tab closing.
- **Consent indicator:** a persistent "Ghost is listening" element in the side panel whenever capture is active.
- Only DIRECT_REQUEST and ACTION_REQUIRED mention types reach Slack; INFORMATIONAL, REFERENCE, and NO_ACTION mentions are stored but never notified.

## Repo structure

```
ghost/
├── extension/          # Chrome extension (Manifest V3)
│   ├── manifest.json
│   ├── background.js
│   ├── offscreen.html
│   ├── offscreen.js
│   ├── sidepanel.html
│   └── sidepanel.js
├── backend/             # FastAPI server
│   ├── main.py
│   ├── transcribe_handler.py
│   ├── decision_detector.py
│   ├── cedar_policy.py
│   ├── slack_notifier.py
│   ├── slack_webhook.py
│   ├── opensearch_client.py
│   ├── database.py
│   └── demo_mode.py
└── slack-app/           # Slack app configuration
```

## Tech stack

| Layer | Technology |
|---|---|
| Extension | Chrome Manifest V3, tabCapture, offscreen, sidePanel |
| Audio processing | ScriptProcessorNode, 16kHz, 16-bit PCM |
| Backend | FastAPI, WebSockets, Uvicorn |
| ASR | AWS Transcribe Streaming — sole provider, no fallback |
| LLM | Strands Agent (or Claude 3 Haiku for latency) |
| Policy | Cedar (cedarpy) |
| Notifications | Slack SDK (Block Kit, interactive buttons) |
| Database | PostgreSQL (async, via asyncpg) — build first, P0 |
| Search | OpenSearch — add after core loop works, P1 |
| Deployment | Local + ngrok for the Slack webhook |

## Build order and priority

Build phase by phase, in this order. Each phase has its own exit criteria — treat those as the definition of done, not "looks like it works."

0. Architecture lock + repo scaffold
1. Audio capture pipeline (extension side)
2. Streaming relay + ASR (backend side)
3. Decision detection (LLM, two-tier trigger, debounce)
4. Cedar policy + Slack (incl. drafted answer)
5. Database + OpenSearch (Postgres first, OpenSearch second)
6. Side panel UI (listening state, triggered/answered state, search — search is P2, cut first if short on time)
7. Demo rehearsal — run this in a clean context, full end-to-end, twice in a row

## Settled decisions from completed phases

**Phase 1 (extension audio capture):**

- **Message protocol:** every `chrome.runtime.sendMessage` payload is `{ type, target, ...data }`.
  `type` is always one of the named constants in `extension/messages.js`
  (`START_CAPTURE`, `STOP_CAPTURE`, `CAPTURE_STARTED`, `CAPTURE_STOPPED`, `CAPTURE_ERROR`,
  `CONNECTION_STATUS`), never a raw string. `target` is `'background'`, `'offscreen'`, or
  `'sidepanel'` and exists because Chrome's messaging is a broadcast bus — every context with a
  listener receives every message, so each listener filters on `target` to avoid mis-handling or
  self-looping on messages it sent itself.
- **Capture state:** background.js keeps state in `chrome.storage.session` under the key
  `ghostSession`, shape `{ status: 'idle'|'capturing'|'error', meetingSessionId: string|null,
  tabId: number|null }`. Session storage (not in-memory globals) so a service-worker restart
  mid-call doesn't lose track of an in-progress session.
- **Backend URL:** stored in `chrome.storage.local` key `backendUrl`, default
  `ws://localhost:8000/ws/transcribe`. Unset/empty falls back to the default. Read by
  background.js (not offscreen.js — `chrome.storage` was found to be unavailable inside the
  offscreen document's own execution context on at least one real install, for reasons that don't
  affect the service worker or side panel contexts of the same extension) and passed to
  offscreen.js as a field on the `START_CAPTURE` message, the same way `watchedUserNameVariants`/
  `slackTarget` already were. offscreen.js does not call `chrome.storage` at all as of this fix.
- **PCM wire format:** raw binary WebSocket frames (not JSON-wrapped), Int16 PCM, mono, 16kHz,
  4096 samples (8192 bytes) per frame. Phase 2's backend WS handler needs to expect exactly this,
  not a JSON envelope.
- **Reconnection:** offscreen.js retries a dropped backend connection up to 3 times with a 3s
  delay between attempts; the 3rd failure broadcasts `CAPTURE_ERROR` ("Connection lost") and
  tears down the whole capture pipeline (mic, audio graph, socket) rather than leaving it running
  with nowhere to send audio.

**Phase 2 (backend streaming relay + ASR):**

- **TranscriptEvent shape** (`backend/asr_base.py`): `text: str`,
  `speaker: str` (`"spk_N"` from Transcribe's diarization, or `"unknown"`; name inference from
  self-intros is not done here — that's Phase 3's job), `timestamp: datetime` (wall-clock UTC at
  the moment the event was finalized, not audio-relative seconds), `confidence: float`,
  `is_partial: bool`, `meeting_id: str`. Still its own `ASRProvider` interface (not collapsed into
  `AWSTranscribeProvider`) even though AWS Transcribe is the only implementation, so a provider
  is still mockable for tests.
- **What reaches the client:** only final (`is_partial=False`) events, sent over `/ws/transcribe`
  as JSON via `websocket.send_json(event.model_dump(mode="json"))`. Partials are produced
  internally but dropped before the WebSocket — Phase 3 only ever sees finals.
- **`/ws/transcribe` session handling:** optional `?session_id=` query param; omit it and the
  server generates one and sends `{"meeting_session_id": "..."}` as the first message so the
  client can persist it for reconnects. Supplying one is trusted as-is (no server-side session
  registry yet — that's Phase 5) and always opens a NEW provider stream under that meeting_id.
- **`/ws/demo`:** streams `backend/fixtures/demo_transcript.json` verbatim as TranscriptEvents,
  one every 2s, `meeting_id="demo-meeting"`, `is_partial` always `False`. Edit that JSON file to
  change the scripted demo, not the endpoint code.
- **ASR provider:** `get_asr_provider()` constructs and starts `AWSTranscribeProvider` directly and
  unconditionally — no provider switch, no fallback (Deepgram was removed; see the migration note
  under Phase 6). Real AWS credentials are required to actually produce transcripts — without them
  `get_asr_provider` raises and `/ws/transcribe` closes with code 1011 after announcing the
  session id.
- **Logging:** every log line for a `/ws/transcribe` or `/ws/demo` connection goes through
  `MeetingLoggerAdapter` (`backend/asr_base.py`), which prefixes `[meeting_session_id=...]` to the
  message text — not a `%(meeting_session_id)s` field in the global formatter, which would
  `KeyError` on any other logger (uvicorn's, the AWS Transcribe SDK's) that doesn't carry it.

**Phase 3 (decision detection):**

- **LLM model:** CLAUDE.md's "Claude 3 Haiku" is retired — Tier 2 originally used `claude-haiku-4-5`
  via the direct Anthropic API (`backend/decision_detector.py`, constant `LLM_MODEL`, public —
  Phase 4's answer_drafter.py reuses it and `LLM_TIMEOUT_SECONDS`), wrapped in
  `asyncio.wait_for(..., timeout=4.0)`. **Post-launch switch to AWS Bedrock** (see the migration
  note after Phase 6's own migration note below): `LLM_MODEL` now holds a Bedrock model ID
  (`bedrock_llm.BEDROCK_MODEL_ID`, `anthropic.claude-haiku-4-5-20251001-v1:0` — this project's own
  confirmed Bedrock Model catalog entry; re-verify there if it ever needs to change) rather than
  Anthropic's direct-API model name, and calls go through `backend/bedrock_llm.py`'s
  `BedrockMessagesClient` — a minimal
  httpx-based client hitting Bedrock's `invoke_model` REST endpoint directly with a Bedrock API
  key (bearer token from the Bedrock console's "API keys" page) as `Authorization: Bearer <key>` —
  not the `anthropic` SDK/package, which was removed from `requirements.txt` entirely. This shim
  deliberately mimics the exact `.messages.create(...) -> response.content[i].type/.text` shape the
  `anthropic` SDK had, so `decision_detector.py`/`answer_drafter.py`'s calling code, and every
  existing test's fake LLM client (which already duck-typed that same shape), needed no changes —
  only the concrete class `DecisionPipeline`/`answer_drafter.py` construct by default changed.
  Structured JSON-schema output (`output_config=...`) is dropped in the Bedrock path — unverified
  whether Bedrock's `invoke_model` accepts that exact parameter — relying instead on the existing
  prompt-plus-parse-and-validate-with-graceful-fallback path already in `_classify_window`.
- **Per-session name watching is not automatic:** `DecisionPipeline.set_watch_names(meeting_id,
  names)` must be called before Tier 1 will ever match anything. Phase 4's session_init handshake
  (see below) is what actually calls this for real sessions now — see that section, not this one,
  for how identity reaches a session.
- **Buffering window:** carry-forward is literally prepended into the next window's buffer (not a
  separate context list) — `state.buffer = window_events[-3:]` after each close — so both Tier 1's
  regex and Tier 2's LLM call see carried lines alongside new ones. Window closes at 30s elapsed
  (by event timestamp, not wall clock) or 10 segments, whichever first.
- **Debounce timer is real wall-clock**, `asyncio.sleep(settings.debounce_window_seconds)` (12s
  default), separate from the window-closing logic above. Tests shorten
  `settings.debounce_window_seconds` directly (it's a mutable pydantic-settings singleton) rather
  than waiting 12 real seconds.
- **Never blocks the transcript stream:** `DecisionPipeline.process_transcript_event` only ever
  blocks synchronously on cheap buffering + a regex check; Tier 2's LLM call and everything after
  it (dedup, batching) runs as an internally-tracked background `asyncio.Task`. `main.py`'s
  `transcript_loop` awaits it inline — that's safe precisely because it never blocks meaningfully.
- **DecisionRecord** (`backend/decision_detector.py`) is the shape Phase 4/5 consume: `id`
  (server-generated uuid4), `meeting_id`, `decision_text`, `speaker` (resolved via SpeakerMapper —
  a name if self-introduced, else the raw `spk_N` label), `requires_action_from`, `context`,
  `confidence`, `urgency`, `timestamp`, `status` (default `"pending"`). No Slack/DB-specific
  fields — don't add any when wiring Phase 4.
- **Output callback:** `DecisionPipeline(on_decision_batch=...)` — default
  (`default_on_decision_batch`) logs each batch and appends to `backend/decisions_log.jsonl`
  (gitignored, placeholder only). Phase 4 replaces the callback the shared `decision_pipeline`
  singleton (`backend/decision_detector.py`) is constructed with; nothing else in this file needs
  to change.

**Phase 4 (session identity + Cedar/Slack notification):**

- **session_init handshake:** the first message on every `/ws/transcribe` and `/ws/demo`
  connection (after the server's own `{"meeting_session_id": ...}` announcement on
  `/ws/transcribe`) must be
  `{"type": "session_init", "watched_user_name_variants": [...], "slack_target": "..."}` from the
  client. Missing/malformed/absent → the server sends `{"type": "error", "message": "..."}` and
  closes with code 1008. No silent fallback to a hardcoded name, ever — see
  `main.py`'s `_handshake_session_init`. This is what actually calls
  `decision_pipeline.set_watch_names`/`set_slack_target` for a session now.
- **Per-session identity lives in `DecisionPipeline`'s `_SessionState`** (`slack_target` field,
  alongside the existing `watch_names_pattern`), never in the process-wide `Settings` object —
  Settings is shared across every concurrent meeting, so it can't hold a per-meeting value.
  `decision_pipeline.get_slack_target(meeting_id)` is how the notification pipeline looks it up.
- **Control messages after the handshake:** `/ws/transcribe`'s receive loop now inspects each
  frame's raw type (`websocket.receive()`, not `receive_bytes()` — Phase 2's assumption that every
  frame is audio is obsolete) so binary audio and JSON control messages (currently just
  `{"type": "speaker_override", "label": "spk_0", "name": "Priya"}`) can interleave.
  `decision_detector.handle_control_message(meeting_id, payload, log)` routes these — both
  `/ws/transcribe` and `/ws/demo` call it, so routing logic lives in one place.
- **Extension side:** `chrome.storage.local` key `ghostIdentity` = `{ name, nameVariants,
  slackTarget }`, written by the sidepanel's one-time setup screen (gates the Start button).
  `background.js` reads it and includes `watchedUserNameVariants`/`slackTarget` on the
  `START_CAPTURE` message to offscreen.js, which sends `session_init` as the first WS frame on
  every (re)connection — including reconnects, since the backend expects it as the first frame of
  every new connection, not just the session's first. A new `SPEAKER_OVERRIDE` message type
  (`extension/messages.js`) carries a speaker-override submission from sidepanel through
  background to offscreen, which sends it as a `speaker_override` control message immediately.
- **DecisionStoreLike (structural typing, not an import):** `decision_detector.py` defines a
  `Protocol` with just `create()` rather than importing `decision_store.py`, because
  `decision_store.py` needs `DecisionRecord` from `decision_detector.py` — importing both
  directions would cycle. `main.py` is what actually connects them at startup
  (`decision_pipeline.set_decision_store(decision_store)`), not either module importing the other.
  The same reasoning is why `main.py` (not `decision_detector.py`) constructs
  `make_notification_pipeline(decision_store)` and assigns it to
  `decision_pipeline.on_decision_batch`.
- **JSONLDecisionStore is append-only, "latest line wins":** `update_status` doesn't edit
  `decisions_log.jsonl` in place — it appends the record again with the new status. The in-memory
  index (rebuilt from the file at startup) keeps only the latest line per `id`, so this gives
  correct current-state semantics without needing in-place file edits. `approved_by` is accepted
  and logged but not persisted on the record itself — Phase 3's `DecisionRecord` intentionally has
  no field for it; Phase 5's real schema can add one.
- **Cedar policy set is a plain reassignable module global** (`cedar_policy._POLICY_SET`), parsed
  once at import — not wrapped in a function — specifically so tests can simulate a corrupt
  `decisions.cedar` by monkeypatching it to a malformed string (`cedarpy.is_authorized` accepts a
  raw string and parses it internally, so this hits the exact same failure path a bad file would).
  `check_decision_policy` fails closed on literally any exception.
  `policies/decisions.cedar` forbids `resource.decision_type == "hiring"` as the one example rule;
  everything else is permitted by default — edit freely.
- **Decision persistence timing:** `decision_store.create()` is called as soon as a decision is
  accepted (in `_classify_and_batch`, right after the dedup check passes) — not delayed until the
  debounce timer fires. The timer is purely a notification-batching concern; persistence
  shouldn't wait on it. A persistence failure is logged but doesn't block batching/notification.
- **`answer_drafter.draft_answer`'s "search"** is keyword-overlap re-ranking of
  `decision_store.get_recent(meeting_id, limit=10)`'s results in memory — not a second store
  method. Keeps the store interface to exactly the 3 methods in the spec; Phase 5 swaps the
  ranking step for real OpenSearch relevance search without changing the interface.
- **`python-multipart` added to `requirements.txt`** (pinned `0.0.20`) — required transitively by
  Starlette's `Request.form()`, which `/slack/interaction` needs to parse Slack's
  `application/x-www-form-urlencoded` payload.
- **Slack scopes:** `slack-app/manifest.yaml` needs `users:read.email` in addition to
  `chat:write`/`im:write` — `slack_notifier.py` resolves an email `slack_target` via
  `users.lookupByEmail` before DMing.

**Phase 5 (persistence + OpenSearch search) — originally built on SQLite, migrated to PostgreSQL;
see the migration note after Phase 6 for the swap itself:**

- **`decision_store.DecisionStore` interface is unchanged** — `get_recent`/`create`/`update_status`
  keep their exact Phase 4 signatures. `PostgresDecisionStore` (backed by `database.py`'s async
  SQLAlchemy/asyncpg setup) replaced `JSONLDecisionStore`; nothing upstream (`decision_detector.py`,
  `notification_pipeline.py`, `answer_drafter.py`, `slack_webhook.py`) needed to change.
- **Schema creation happens in `main.py`'s `lifespan` handler**, not at module import —
  `async with engine.begin()` needs a running event loop. No Alembic/migrations —
  `create_all()` only creates tables/types that don't exist yet, so a schema change means dropping
  and recreating the dev database, not migrating it in place.
- **Postgres handles concurrent `create()`/`update_status()` calls** (decision creation vs. the
  Slack webhook's status updates) natively via MVCC — no journal-mode workaround needed.
- **A plain (non-timezone) DateTime column strips tzinfo on the way back out**, regardless of
  backend — a `datetime.now(timezone.utc)` value written comes back with `tzinfo=None`.
  `decision_store._row_to_record` reattaches `timezone.utc` on every read, since every timestamp
  written here was already UTC and `DecisionRecord`'s contract elsewhere assumes tz-aware. Don't
  remove this — a naive datetime slipping into code that compares it against a tz-aware one raises
  `TypeError`. asyncpg is stricter than aiosqlite was on the write side, though: it rejects a
  tz-aware datetime outright against this column type instead of silently dropping the tzinfo, so
  `PostgresDecisionStore.create()` strips it explicitly (`.replace(tzinfo=None)`) before the insert.
- **Tests use an isolated database**, not the real dev `ghost` database: `config.Settings.database_url`
  reads `DATABASE_URL`, and `tests/conftest.py` sets it to a separate `ghost_test` database (same
  Postgres server, same `ghost` credentials) before any test module imports `config.py`/`database.py`
  (conftest.py is collected first). The same fixture drops and recreates the schema directly —
  `TestClient(app)` used without an explicit `with` block never fires the FastAPI lifespan, so
  nothing else would create it for tests. **`database.engine` uses `NullPool`** — asyncpg binds a
  pooled connection to the event loop that created it, and reusing one from a different loop raises
  "attached to a different loop"; that never came up under aiosqlite, but does here since
  conftest.py's session-scoped schema setup and pytest-asyncio's function-scoped per-test loops are
  different loops sharing this one module-level engine. Don't remove `NullPool` without
  re-verifying the full suite against real Postgres, not just SQLite-era assumptions.
- **`opensearch_client.py` is fully optional at every layer:** `_client` is `None` whenever
  `OPENSEARCH_HOST` is unset, and every function treats that as "not part of this deployment," not
  an error. `ensure_index()`/`index_decision()` never raise; `search_decisions()` is the one
  function allowed to raise (unconfigured or unreachable), specifically so `main.py`'s `/search`
  route can catch it and fall back to Postgres — the raising is deliberate, not a bug.
- **`opensearch_client.index_decision(decision, approved_by=None)` takes `approved_by` as a
  separate parameter**, not part of `DecisionRecord` — the record intentionally has no field for it
  (Phase 4). Without this, `approved_by` could never become searchable despite
  `search_decisions`'s `multi_match` querying it. `PostgresDecisionStore.update_status` passes it
  through after building a fresh record from the just-updated row.
- **`GET /search?q=`**: tries OpenSearch first, falls back to a Postgres `LIKE '%q%'` scan across
  `decision_text`/`approved_by`/`speaker` on any exception (unconfigured, unreachable, or a query
  error) — never hard-fails just because OpenSearch is down. Uses `Depends(database.get_session)`
  for its direct DB access, per the Depends-for-routes convention; `PostgresDecisionStore` itself
  isn't a route, so it opens its own sessions via `database.async_session_maker()` directly. Its
  response still labels this path `"sqlite_fallback"` — a naming leftover from before the Postgres
  migration, kept as-is deliberately since `tests/test_search_endpoint.py` asserts on that exact
  string and the migration's test harness explicitly re-ran Phase 5's tests unmodified.
- **`aiohttp` pinned explicitly** in `requirements.txt` — `opensearch-py`'s declared dependencies
  are only `certifi`/`Events`/`python-dateutil`/`requests`/`urllib3`; `AsyncOpenSearch`'s transport
  needs `aiohttp` but opensearch-py doesn't declare it as a hard dependency, so it's pinned
  explicitly here so it doesn't silently disappear.
- **`docker-compose.yml`** (repo root) is the only place Docker is used in this project, per
  CLAUDE.md scope — a single-node OpenSearch with `plugins.security.disabled=true` for local-dev
  simplicity (no TLS/auth setup needed; matches `OPENSEARCH_HOST=http://localhost:9200` with no
  user/password), alongside the `postgres` service added by the Postgres migration (see the
  migration note after Phase 6). **OpenSearch itself is still not verified against a real running
  instance in this sandbox** — Docker's daemon needs privileges this environment doesn't grant, and
  image pulls from Docker Hub's CDN are blocked by this session's egress policy (403, not a
  transient failure — did not retry or route around it, per the agent proxy's own instructions).
  `opensearch_client.py`'s logic is covered by tests against a fake client instead
  (`tests/test_opensearch_client.py`); the fallback path is proven for real by pointing a genuine
  `AsyncOpenSearch` at an unreachable port (`tests/test_search_endpoint.py`) rather than mocked,
  since that needs no Docker at all. That
  file's `test_search_uses_opensearch_when_available` is a real, non-mocked integration check
  against `docker-compose`'s OpenSearch — it skips itself when unreachable rather than failing the
  suite, so it'll actually run and verify the real thing for anyone with Docker available.

**Phase 6 (side panel UI):**

- **`backend/session_connections.py` is a new module owning the registry** mapping
  `meeting_session_id` -> the live WebSocket for that session on `/ws/transcribe`/`/ws/demo`, so
  code that doesn't hold the WebSocket directly (`notification_pipeline.py`, `slack_webhook.py`)
  can still push a message down it. `main.py` registers on successful `session_init` and
  unregisters in every exit path (`finally`), including the ASR-provider-startup-failure path.
  Unregister only removes the connection if it's still the caller's own — a reconnect may already
  have registered a newer one under the same `meeting_id` by the time an old connection's cleanup
  runs. Pushing to a `meeting_id` with no registered connection (panel never opened, or the
  connection already ended) is not an error — it's logged and dropped; `GET /decisions` is what
  backfills a panel that (re)opens later, live pushes are additive on top of that.
- **`decision_store.DecisionStore.update_status` now returns the updated record (or `None` for an
  unknown id)**, not `None` unconditionally — `slack_webhook.py` needs the record's `meeting_id` to
  know which session to push `decision_status_update` to, and it isn't otherwise available at the
  call site (only `decision_id`/`status`/`approved_by` are).
- **`notification_pipeline.py` pushes one `decision_batch` message per batch**, after Cedar +
  drafting finish, containing every decision in the batch — allowed *and* denied, so the panel can
  show the correct status badge for both — as full `DecisionRecord` dicts plus an extra
  `drafted_answer` key (allowed decisions only; `None` for denied). This push happens before the
  `if not allowed: return` early exit, so a denied-only batch still reaches the panel.
  `drafted_answer` is deliberately not part of `DecisionRecord` itself (Phase 3/4 keep drafts
  un-persisted) — it's added only on the pushed JSON, reusing the same draft already computed for
  Slack rather than drafting twice. A denied decision's in-memory `.status` is mutated to
  `"denied_by_policy"` right after its `update_status()` DB write succeeds (only on success, same
  as the existing try/except) — without this the pushed payload would show the pre-denial
  `"pending"` status even though the DB and Slack-side state both correctly show denied.
- **`slack_webhook.py` pushes `decision_status_update`** via a new
  `_update_status_and_notify_panel` wrapper (what `BackgroundTasks` now calls instead of
  `decision_store.update_status` directly): updates the DB, then — only if the id was recognized —
  pushes `{"type": "decision_status_update", "decision_id", "status", "approved_by"}` to that
  decision's `meeting_id` via `session_connections.push`.
- **`GET /decisions?meeting_id=` is a new endpoint** (thin wrapper over Phase 5's
  `decision_store.get_recent(meeting_id, limit=200)`) added specifically to hydrate the panel's
  list on open/reopen, since live pushes alone can't backfill state from before the panel was
  open. Not called out explicitly in earlier phases' specs, but required by this one's "fetch GET
  /decisions on load" requirement — same minimal-addition-when-justified precedent as `httpx`/
  `python-multipart`/`aiohttp` in prior phases. Historical decisions from it never carry
  `drafted_answer` (never persisted); only ones pushed live while the panel is open do.
- **CORS is handled via `extension/manifest.json`'s `host_permissions`** (`http://localhost:8000/*`
  added), not `CORSMiddleware` in `main.py` — Chrome MV3 extension pages with a matching host
  permission bypass CORS entirely for `fetch()`, so this stays entirely on the extension side and
  doesn't touch backend logic beyond the push wiring above.
- **`extension/messages.js` gains three constants** (`SESSION_ID_ASSIGNED`, `DECISION_BATCH`,
  `DECISION_STATUS_UPDATE`) for `offscreen.js` -> `sidepanel.js` relays of backend WS pushes — kept
  distinct from the backend's own wire-format strings (`"decision_batch"`, etc.) so the
  offscreen<->sidepanel contract doesn't depend on the backend's JSON shape.
- **`offscreen.js` gained a `ws.onmessage` handler** (there was none before this phase — Phase 2-5
  only ever sent audio, never read anything back except via the message types already covered).
  Every server->client frame is JSON; it's told apart by shape: no `type` field but a
  `meeting_session_id` string means the session-id announcement (relayed as
  `SESSION_ID_ASSIGNED`), `type: "decision_batch"`/`"decision_status_update"` are relayed as their
  matching constants, and anything else (transcript events, `type: "error"`) is left alone — not
  this phase's concern.
- **The side panel tracks the backend-confirmed session id under a new `chrome.storage.session`
  key, `ghostPanelSessionId`** — deliberately distinct from `background.js`'s own `ghostSession`
  key. `ghostSession.meetingSessionId` is generated client-side (`crypto.randomUUID()`) purely for
  background.js's own capturing/idle/error state tracking; it's never actually sent to the backend
  (`offscreen.js`'s `startCapture` doesn't take or forward it, and the WS connection carries no
  `?session_id=` query param), so it is *not* the id `GET /decisions`/`GET /search` need to be
  scoped by. `ghostPanelSessionId` is set only from the backend's own `SESSION_ID_ASSIGNED`
  announcement (or, for demo mode, opportunistically from the first `decision_batch` push's
  `meeting_id`, since `/ws/demo` never sends that announcement — only `/ws/transcribe` does).
- **`sidepanel.js` is one `panelState` object + one `render(state)` function.** Every message
  handler and user interaction mutates `panelState` then calls `render(panelState)` — no ad-hoc DOM
  writes inside handlers. `upsertDecision(state, decision)` (replace in place by `id`, else append)
  is the only way `panelState.decisions` is ever modified, for both `DECISION_BATCH` (upsert each
  decision) and `DECISION_STATUS_UPDATE` (upsert a merged `{...existing, status, approved_by}`
  record — falls back to a partial `{id, status, approved_by}` if the id isn't already known,
  which is what "upsert" naturally does rather than a special case). A decision landing on
  `approved`/`answered_live` sets it as `activeDecisionId`, which is what drives the
  triggered/answered expanded view above the list.
- **All decision-derived text (`decision_text`, `speaker`, drafted answers) is inserted via
  `textContent`**, never `innerHTML` string interpolation — this content originates from meeting
  transcripts and LLM output and must be treated as untrusted (XSS). `sidepanel.js`'s only
  `innerHTML` uses are `= ''` clears before rebuilding a container from scratch.
- **`sidepanel.css` is a new file** (previously inline `<style>` in `sidepanel.html`, now moved
  out) — near-black background, cyan for pending/listening, green for approved, plus muted
  gray/red/violet for rejected/denied_by_policy/answered_live so all five status badges stay
  visually distinct.
- **Search (P2) is 300ms-debounced**, fires `GET /search?q=&meeting_id=` only at 2+ characters
  after the debounce settles, and stores results in `panelState.searchResults` (`null` = show the
  full list; an array, even empty, = show search results instead). Clearing the query sets
  `searchResults` back to `null`, restoring the full list from `panelState.decisions` without a
  network call. Note `main.py`'s `/search` route doesn't actually filter by `meeting_id` (Phase 5
  didn't add that parameter, and this phase doesn't touch backend search logic) — the panel sends
  it per spec, but search results may span meetings until a later phase adds real filtering.

**Migration (Deepgram removed, SQLite migrated to PostgreSQL) — after Phase 6:**

- **Deepgram is gone entirely, not just de-emphasized.** `DeepgramProvider`, its config
  (`DEEPGRAM_API_KEY`, `ASR_PROVIDER`), and the fallback-on-AWS-failure logic are all deleted, not
  commented out. `transcribe_handler.get_asr_provider()` constructs and starts
  `AWSTranscribeProvider` directly and unconditionally; a failure to start now propagates straight
  to the caller (`main.py`'s `ws_transcribe`, which still closes the socket with code 1011 on any
  `get_asr_provider` exception — that catch-all was never Deepgram-specific). The `ASRProvider`
  abstract interface itself (`asr_base.py`) stays, on purpose, so a provider is still mockable for
  tests even with only one real implementation.
- **Postgres is the sole database, no SQLite anywhere** — `config.Settings.database_url` is
  required with no default (a missing value fails loudly at startup, same principle as every other
  required credential in that file), read from `DATABASE_URL`. Local dev value:
  `postgresql+asyncpg://ghost:ghost@localhost:5432/ghost`, matching `docker-compose.yml`'s new
  `postgres` service (`postgres:16`, a named volume so data survives a container restart).
- **`database.Decision.id` is a native Postgres `UUID` column** (`sqlalchemy.dialects.postgresql.UUID(as_uuid=True)`),
  not a string — `PostgresDecisionStore` passes `decision.id`/`decision_id` straight through as
  `uuid.UUID` objects everywhere, no `str()`/`uuid.UUID()` conversion at either end anymore.
- **`database.Decision.status` is a native Postgres `ENUM`** (`sqlalchemy.dialects.postgresql.ENUM`,
  named `decision_status`), covering exactly `pending`/`approved`/`rejected`/`denied_by_policy`/
  `answered_live` — the same five values already in use everywhere else. Writing anything outside
  that set is now a database-level error at write time, verified directly against a real Postgres
  instance (a raw `INSERT ... status = 'bogus_status'` raises `invalid input value for enum
  decision_status`).
- **asyncpg needs `NullPool` on the async engine** — see the `NullPool` bullet under Phase 5 above;
  this is the one genuinely new gotcha the migration surfaced, not just a driver swap.
- **asyncpg is strict about tz-aware datetimes against a non-timezone column** — see the tz-aware
  bullet under Phase 5 above; `PostgresDecisionStore.create()` strips tzinfo explicitly before
  insert, which aiosqlite never required.
- **Old SQLite data was NOT migrated** — `ghost.db`/`test_ghost.db` (Phase 5-era, gitignored) held
  only development/test data. Postgres starts empty; this was the intended behavior for a
  hackathon-scope migration, not an oversight, and no data-migration script was written.
- **Unlike OpenSearch, Postgres genuinely runs in this sandbox** (`postgresql-16` was already
  installed at the OS level here, unrelated to Docker) — `tests/test_postgres_connectivity.py` and
  every test in `test_database.py` ran against a real local Postgres instance (databases `ghost`
  and `ghost_test`, both owned by a `ghost` role with password `ghost`), not a fake/mocked one.
  This is a stronger verification story than Phase 5's original SQLite-to-OpenSearch migration had,
  where Docker/OpenSearch couldn't be verified for real in-sandbox at all.
- **`main.py`'s `/search` route and its `"sqlite_fallback"` response label were deliberately left
  untouched** — the migration's own test harness re-ran Phase 5's tests (including
  `test_search_endpoint.py`, which asserts on that exact string) unmodified, so renaming it would
  have contradicted that requirement. The label is now a harmless historical misnomer, not a bug.
- **Every other test suite (Phase 3, 4, 5, 6) re-ran unmodified against Postgres and all pass** —
  this is the actual proof the store-interface swap didn't break anything upstream, same principle
  Phase 5's original SQLite migration used.

## Conventions

- One commit per completed phase, not mid-phase.
- When a phase settles a decision that affects later phases (e.g. the final diarization implementation, the debounce window length), update this file so later phases inherit it instead of re-deciding it.
- Manual, human-only steps — Slack app creation/install, AWS IAM and budget alarms — are done outside of Claude Code. Ask for the credentials/config rather than trying to provision them.
- If a proposed change would expand scope (a platform beyond Meet, a notification channel beyond Slack, a UI control beyond what's listed), flag it before building instead of adding it silently.
- **Push progress automatically.** The canonical remote is `https://github.com/aman2240/aws-project.git`. After every commit made in a Claude Code session (per the one-commit-per-completed-phase convention above), push it to that remote on the current working branch without waiting for separate per-push confirmation — this note is the standing authorization for that. Never force-push; if a push is rejected because the remote has diverged, pull/rebase (or ask the user) rather than overwriting remote history.
