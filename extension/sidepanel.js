// Side panel logic. Owns: the one-time identity setup screen (gates
// Start until a name + Slack target are saved), the Start/Stop
// controls, the "Ghost is listening" consent indicator, and — this
// phase — the live/historical decision list, the triggered/answered
// expanded view, and search (P2).
//
// Everything after setup is driven by one `panelState` object and one
// `render(state)` function: every message handler and user interaction
// updates panelState then calls render — no direct DOM mutation
// scattered through handlers. Decision text/speaker/context is always
// inserted via textContent, never innerHTML — it originates from
// meeting transcripts and LLM output and must be treated as untrusted.

import {
  START_CAPTURE,
  STOP_CAPTURE,
  CAPTURE_STARTED,
  CAPTURE_STOPPED,
  CAPTURE_ERROR,
  CONNECTION_STATUS,
  SPEAKER_OVERRIDE,
  SESSION_ID_ASSIGNED,
  DECISION_BATCH,
  DECISION_STATUS_UPDATE,
} from './messages.js';

const SESSION_STATE_KEY = 'ghostSession';
const IDENTITY_STORAGE_KEY = 'ghostIdentity';
// Distinct from background.js's `ghostSession.meetingSessionId` — that
// one is generated client-side and never reaches the backend as the
// real session id (offscreen.js doesn't pass it on connect). This key
// holds the id the backend itself confirmed (SESSION_ID_ASSIGNED), the
// only id GET /decisions and /search can be scoped by.
const PANEL_SESSION_ID_KEY = 'ghostPanelSessionId';
const DEFAULT_BACKEND_WS_URL = 'ws://localhost:8000/ws/transcribe';
const SEARCH_DEBOUNCE_MS = 300;
const ACTIVE_VIEW_STATUSES = new Set(['approved', 'answered_live']);

const setupScreen = document.getElementById('setup-screen');
const setupNameInput = document.getElementById('setup-name');
const setupNameVariantsInput = document.getElementById('setup-name-variants');
const setupSlackTargetInput = document.getElementById('setup-slack-target');
const setupSaveBtn = document.getElementById('setup-save-btn');
const setupError = document.getElementById('setup-error');

const mainScreen = document.getElementById('main-screen');
const startBtn = document.getElementById('start-btn');
const stopBtn = document.getElementById('stop-btn');
const statusText = document.getElementById('status-text');

const overrideLabelInput = document.getElementById('override-label');
const overrideNameInput = document.getElementById('override-name');
const overrideApplyBtn = document.getElementById('override-apply-btn');

const listeningIndicator = document.getElementById('listening-indicator');
const meetingMeta = document.getElementById('meeting-meta');

const activeDecisionEl = document.getElementById('active-decision');
const backendUnreachableEl = document.getElementById('backend-unreachable');
const retryBtn = document.getElementById('retry-btn');
const searchInput = document.getElementById('search-input');
const decisionListEl = document.getElementById('decision-list');
const decisionListEmptyEl = document.getElementById('decision-list-empty');

// --- panelState -------------------------------------------------------

let panelState = {
  captureStatus: 'idle', // 'idle' | 'connecting' | 'capturing' | 'reconnecting' | 'error'
  captureStartedAt: null,
  sessionId: null,
  decisions: [],
  activeDecisionId: null,
  searchQuery: '',
  searchResults: null, // null = showing full list, array = showing search results
  backendReachable: null, // null = not yet checked
};

function upsertDecision(state, decision) {
  if (!decision || !decision.id) return state;
  const idx = state.decisions.findIndex((d) => d.id === decision.id);
  if (idx === -1) {
    state.decisions = [...state.decisions, decision];
  } else {
    state.decisions = [
      ...state.decisions.slice(0, idx),
      { ...state.decisions[idx], ...decision },
      ...state.decisions.slice(idx + 1),
    ];
  }
  return state;
}

// --- rendering ----------------------------------------------------------

function statusLabel(status) {
  switch (status) {
    case 'pending':
      return 'Pending';
    case 'approved':
      return 'Approved';
    case 'rejected':
      return 'Rejected';
    case 'denied_by_policy':
      return 'Denied by policy';
    case 'answered_live':
      return 'Answered live';
    default:
      return status || 'Unknown';
  }
}

function buildDecisionCard(decision) {
  const card = document.createElement('div');
  card.className = `decision-card status-${decision.status || 'pending'}`;

  const meta = document.createElement('div');
  meta.className = 'decision-card-meta';

  const speaker = document.createElement('span');
  speaker.className = 'decision-card-speaker';
  speaker.textContent = decision.speaker || 'Unknown speaker';

  const badge = document.createElement('span');
  badge.className = `status-badge status-${decision.status || 'pending'}`;
  badge.textContent = statusLabel(decision.status);

  meta.appendChild(speaker);
  meta.appendChild(badge);

  const timestampEl = document.createElement('div');
  timestampEl.className = 'decision-card-meta';
  timestampEl.textContent = decision.timestamp ? new Date(decision.timestamp).toLocaleTimeString() : '';

  const text = document.createElement('div');
  text.className = 'decision-card-text';
  text.textContent = decision.decision_text || '';

  card.appendChild(meta);
  card.appendChild(timestampEl);
  card.appendChild(text);
  return card;
}

function renderDecisionList(state) {
  decisionListEl.innerHTML = '';

  if (state.backendReachable === false) {
    decisionListEl.classList.add('hidden');
    decisionListEmptyEl.classList.add('hidden');
    return;
  }

  const source = state.searchResults !== null ? state.searchResults : state.decisions;
  const sorted = [...source].sort((a, b) => {
    const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
    const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
    return tb - ta;
  });

  if (sorted.length === 0) {
    decisionListEl.classList.add('hidden');
    decisionListEmptyEl.classList.remove('hidden');
    decisionListEmptyEl.textContent =
      state.searchResults !== null ? 'No decisions match your search.' : 'No decisions yet.';
    return;
  }

  decisionListEl.classList.remove('hidden');
  decisionListEmptyEl.classList.add('hidden');
  for (const decision of sorted) {
    decisionListEl.appendChild(buildDecisionCard(decision));
  }
}

function renderActiveDecision(state) {
  const active = state.activeDecisionId
    ? state.decisions.find((d) => d.id === state.activeDecisionId)
    : null;

  if (!active || !ACTIVE_VIEW_STATUSES.has(active.status)) {
    activeDecisionEl.classList.add('hidden');
    activeDecisionEl.innerHTML = '';
    return;
  }

  activeDecisionEl.classList.remove('hidden');
  activeDecisionEl.innerHTML = '';

  const label = document.createElement('div');
  label.className = 'active-decision-label';
  label.textContent = active.status === 'answered_live' ? 'Answered live' : 'Triggered';

  const question = document.createElement('div');
  question.className = 'active-decision-question';
  question.textContent = active.decision_text || '';

  const answer = document.createElement('div');
  answer.className = 'active-decision-answer';
  answer.textContent = active.drafted_answer || 'No drafted answer available.';

  const confirmation = document.createElement('div');
  confirmation.className = 'active-decision-confirmation';
  confirmation.textContent =
    active.status === 'answered_live' ? 'Take over live.' : 'Answer sent.';

  activeDecisionEl.appendChild(label);
  activeDecisionEl.appendChild(question);
  activeDecisionEl.appendChild(answer);
  activeDecisionEl.appendChild(confirmation);
}

function renderBackendUnreachable(state) {
  if (state.backendReachable === false) {
    backendUnreachableEl.classList.remove('hidden');
  } else {
    backendUnreachableEl.classList.add('hidden');
  }
}

function renderListeningIndicator(state) {
  const isActive = state.captureStatus === 'capturing' || state.captureStatus === 'reconnecting';
  if (!isActive) {
    listeningIndicator.classList.add('hidden');
    return;
  }
  listeningIndicator.classList.remove('hidden');

  const parts = [];
  if (state.sessionId) parts.push(`Session ${state.sessionId.slice(0, 8)}`);
  if (state.captureStartedAt) {
    parts.push(`since ${new Date(state.captureStartedAt).toLocaleTimeString()}`);
  }
  meetingMeta.textContent = parts.join(' · ');
}

function render(state) {
  renderListeningIndicator(state);
  renderActiveDecision(state);
  renderBackendUnreachable(state);
  renderDecisionList(state);
}

// --- capture status (start/stop button + status text) -----------------

function showIdleControls() {
  startBtn.classList.remove('hidden');
  stopBtn.classList.add('hidden');
}

function showCapturingControls() {
  startBtn.classList.add('hidden');
  stopBtn.classList.remove('hidden');
}

function setStatus(text) {
  statusText.textContent = text;
}

// --- Identity setup -------------------------------------------------

function showSetupScreen() {
  setupScreen.classList.remove('hidden');
  mainScreen.classList.add('hidden');
}

function showMainScreen() {
  setupScreen.classList.add('hidden');
  mainScreen.classList.remove('hidden');
}

setupSaveBtn.addEventListener('click', async () => {
  const name = setupNameInput.value.trim();
  const slackTarget = setupSlackTargetInput.value.trim();
  const extraVariants = setupNameVariantsInput.value
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean);

  if (!name || !slackTarget) {
    setupError.textContent = 'Both your name and a Slack user ID or email are required.';
    setupError.classList.remove('hidden');
    return;
  }
  setupError.classList.add('hidden');

  // Dedup while preserving order, primary name first — Tier 1's regex
  // (Phase 3) takes this whole list, not just one exact string.
  const nameVariants = [...new Set([name, ...extraVariants])];

  await chrome.storage.local.set({
    [IDENTITY_STORAGE_KEY]: { name, nameVariants, slackTarget },
  });

  showMainScreen();
});

// --- Speaker override -------------------------------------------------

overrideApplyBtn.addEventListener('click', () => {
  const label = overrideLabelInput.value.trim();
  const name = overrideNameInput.value.trim();
  if (!label || !name) return;

  // Sent the moment it's submitted, not held until session end —
  // background.js relays it to offscreen.js, which sends it as a
  // control message on the already-open WebSocket.
  chrome.runtime.sendMessage({ type: SPEAKER_OVERRIDE, target: 'background', label, name });

  overrideLabelInput.value = '';
  overrideNameInput.value = '';
});

// --- Start/Stop + status -------------------------------------------------

startBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: START_CAPTURE, target: 'background' });
  setStatus('Starting…');
});

stopBtn.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: STOP_CAPTURE, target: 'background' });
  setStatus('Stopping…');
});

// --- Backend HTTP base (for GET /decisions, GET /search) --------------

async function getBackendHttpBase() {
  const { backendUrl } = await chrome.storage.local.get('backendUrl');
  const wsUrl = backendUrl || DEFAULT_BACKEND_WS_URL;
  try {
    const parsed = new URL(wsUrl);
    const protocol = parsed.protocol === 'wss:' ? 'https:' : 'http:';
    return `${protocol}//${parsed.host}`;
  } catch {
    return 'http://localhost:8000';
  }
}

// --- Decision hydration -------------------------------------------------

async function hydrateDecisions(sessionId) {
  if (!sessionId) return;
  try {
    const base = await getBackendHttpBase();
    const response = await fetch(
      `${base}/decisions?meeting_id=${encodeURIComponent(sessionId)}`
    );
    if (!response.ok) throw new Error(`GET /decisions failed: ${response.status}`);
    const data = await response.json();
    panelState.decisions = Array.isArray(data.decisions) ? data.decisions : [];
    panelState.backendReachable = true;
  } catch {
    panelState.backendReachable = false;
  }
  render(panelState);
}

retryBtn.addEventListener('click', () => {
  hydrateDecisions(panelState.sessionId);
});

// --- Search (P2) --------------------------------------------------------

let searchDebounceHandle = null;

searchInput.addEventListener('input', () => {
  const query = searchInput.value;
  panelState.searchQuery = query;

  if (searchDebounceHandle) clearTimeout(searchDebounceHandle);

  const trimmed = query.trim();
  if (trimmed.length < 2) {
    // Clearing (or under the 2-char threshold) restores the full list
    // from panelState.decisions without refetching.
    panelState.searchResults = null;
    render(panelState);
    return;
  }

  searchDebounceHandle = setTimeout(() => {
    runSearch(trimmed);
  }, SEARCH_DEBOUNCE_MS);
});

async function runSearch(query) {
  try {
    const base = await getBackendHttpBase();
    const url = `${base}/search?q=${encodeURIComponent(query)}&meeting_id=${encodeURIComponent(
      panelState.sessionId || ''
    )}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`GET /search failed: ${response.status}`);
    const data = await response.json();
    panelState.searchResults = Array.isArray(data.results) ? data.results : [];
  } catch {
    panelState.searchResults = [];
  }
  render(panelState);
}

// --- Message handling ---------------------------------------------------

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.target !== 'sidepanel') return;

  switch (message.type) {
    case CAPTURE_STARTED:
      panelState.captureStatus = 'capturing';
      panelState.captureStartedAt = Date.now();
      showCapturingControls();
      setStatus('Ghost is listening.');
      render(panelState);
      break;
    case CAPTURE_STOPPED:
      panelState.captureStatus = 'idle';
      panelState.captureStartedAt = null;
      showIdleControls();
      setStatus('Ghost is idle.');
      render(panelState);
      break;
    case CAPTURE_ERROR:
      // The offscreen document that actually threw this closes itself
      // right after broadcasting, so its own console is a near-
      // impossible window to catch — the side panel's console (this
      // one, already open and stable) is where the real stack trace
      // is actually visible for debugging.
      console.error('Capture error:', message.message, message.stack || '(no stack forwarded)');
      panelState.captureStatus = 'error';
      showIdleControls();
      setStatus(message.message || 'Something went wrong.');
      render(panelState);
      break;
    case CONNECTION_STATUS:
      if (message.status === 'connected') {
        panelState.captureStatus = 'capturing';
        setStatus('Ghost is listening.');
      } else if (message.status === 'connecting') {
        panelState.captureStatus = 'connecting';
        setStatus('Ghost is listening. Connecting to backend…');
      } else if (message.status === 'reconnecting') {
        panelState.captureStatus = 'reconnecting';
        setStatus('Ghost is listening. Connection dropped, reconnecting…');
      }
      render(panelState);
      break;
    case SESSION_ID_ASSIGNED: {
      const newSessionId = message.sessionId;
      if (newSessionId && newSessionId !== panelState.sessionId) {
        panelState.sessionId = newSessionId;
        panelState.decisions = [];
        panelState.activeDecisionId = null;
        panelState.searchResults = null;
        panelState.backendReachable = null;
        chrome.storage.session.set({ [PANEL_SESSION_ID_KEY]: newSessionId });
        render(panelState);
        hydrateDecisions(newSessionId);
      }
      break;
    }
    case DECISION_BATCH: {
      // Demo mode (/ws/demo) never sends SESSION_ID_ASSIGNED (the
      // backend only announces a generated id on /ws/transcribe), so
      // pick the meeting id up opportunistically from the first batch
      // if the panel doesn't already know one.
      if (message.meetingId && !panelState.sessionId) {
        panelState.sessionId = message.meetingId;
        chrome.storage.session.set({ [PANEL_SESSION_ID_KEY]: message.meetingId });
      }
      for (const decision of message.decisions || []) {
        upsertDecision(panelState, decision);
      }
      render(panelState);
      break;
    }
    case DECISION_STATUS_UPDATE: {
      const existing = panelState.decisions.find((d) => d.id === message.decisionId);
      const merged = {
        ...(existing || { id: message.decisionId }),
        status: message.status,
        approved_by: message.approvedBy,
      };
      upsertDecision(panelState, merged);
      if (ACTIVE_VIEW_STATUSES.has(message.status)) {
        panelState.activeDecisionId = merged.id;
      }
      render(panelState);
      break;
    }
    default:
      break;
  }
});

// Restore whatever state background.js already has on open, so
// reopening the panel mid-capture (or after a service-worker restart)
// shows the right controls instead of always defaulting to idle.
async function restoreState() {
  const { [SESSION_STATE_KEY]: state } = await chrome.storage.session.get(SESSION_STATE_KEY);

  if (state && state.status === 'capturing') {
    panelState.captureStatus = 'capturing';
    showCapturingControls();
    setStatus('Ghost is listening.');
  } else if (state && state.status === 'error') {
    panelState.captureStatus = 'error';
    showIdleControls();
    setStatus('Ghost hit an error. Try starting again.');
  } else {
    panelState.captureStatus = 'idle';
    showIdleControls();
    setStatus('Ghost is idle.');
  }

  const { [PANEL_SESSION_ID_KEY]: storedSessionId } = await chrome.storage.session.get(
    PANEL_SESSION_ID_KEY
  );
  if (storedSessionId) {
    panelState.sessionId = storedSessionId;
  }

  render(panelState);

  if (panelState.sessionId) {
    await hydrateDecisions(panelState.sessionId);
  }
}

async function init() {
  const { [IDENTITY_STORAGE_KEY]: identity } = await chrome.storage.local.get(IDENTITY_STORAGE_KEY);
  if (identity && identity.name && identity.slackTarget) {
    showMainScreen();
    await restoreState();
  } else {
    showSetupScreen();
  }
}

init();
