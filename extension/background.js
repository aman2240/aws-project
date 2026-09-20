// Service worker. Owns: opening the side panel on icon click,
// starting/stopping tabCapture on the active Meet tab, creating and
// closing the offscreen document, relaying start/stop (plus the
// stored identity and later speaker-override submissions) to
// offscreen.js, tracking capture state in chrome.storage.session (so a
// service-worker restart doesn't lose track of an in-progress
// session), and auto-stopping capture if the captured tab closes or
// navigates away from meet.google.com.

import {
  START_CAPTURE,
  STOP_CAPTURE,
  CAPTURE_STARTED,
  CAPTURE_STOPPED,
  CAPTURE_ERROR,
  SPEAKER_OVERRIDE,
} from './messages.js';

const SESSION_STATE_KEY = 'ghostSession';
const IDLE_STATE = { status: 'idle', meetingSessionId: null, tabId: null };
// Written by sidepanel.js's one-time setup screen: { name, nameVariants,
// slackTarget }. Read here so it can ride along on START_CAPTURE —
// offscreen.js owns the WebSocket and sends it on as session_init.
const IDENTITY_STORAGE_KEY = 'ghostIdentity';

async function getSessionState() {
  const { [SESSION_STATE_KEY]: state } = await chrome.storage.session.get(SESSION_STATE_KEY);
  return state ?? IDLE_STATE;
}

async function setSessionState(patch) {
  const current = await getSessionState();
  const next = { ...current, ...patch };
  await chrome.storage.session.set({ [SESSION_STATE_KEY]: next });
  return next;
}

chrome.action.onClicked.addListener(async (tab) => {
  await chrome.sidePanel.open({ tabId: tab.id });
});

async function ensureOffscreenDocument() {
  const hasDoc = await chrome.offscreen.hasDocument();
  if (hasDoc) return;
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['USER_MEDIA'],
    justification: 'Capture and process Google Meet tab audio for transcription.',
  });
}

async function closeOffscreenDocument() {
  const hasDoc = await chrome.offscreen.hasDocument();
  if (hasDoc) await chrome.offscreen.closeDocument();
}

function tabHostname(url) {
  try {
    return url ? new URL(url).hostname : '';
  } catch {
    return '';
  }
}

function broadcastToSidepanel(message) {
  chrome.runtime.sendMessage({ ...message, target: 'sidepanel' });
}

async function startCapture() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  if (!tab || tabHostname(tab.url) !== 'meet.google.com') {
    broadcastToSidepanel({ type: CAPTURE_ERROR, message: 'Open a Google Meet tab first' });
    return;
  }

  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });

  await ensureOffscreenDocument();

  const meetingSessionId = crypto.randomUUID();
  await setSessionState({ status: 'capturing', meetingSessionId, tabId: tab.id });

  const { [IDENTITY_STORAGE_KEY]: identity } = await chrome.storage.local.get(IDENTITY_STORAGE_KEY);
  // Read here (proven to work in this service-worker context) and
  // passed along, rather than offscreen.js reading chrome.storage.local
  // itself — chrome.storage is unavailable inside that specific
  // offscreen document context on this install, for reasons that don't
  // affect background.js or sidepanel.js, so offscreen.js no longer
  // touches chrome.storage at all.
  const { backendUrl } = await chrome.storage.local.get('backendUrl');

  chrome.runtime.sendMessage({
    type: START_CAPTURE,
    target: 'offscreen',
    streamId,
    meetingSessionId,
    watchedUserNameVariants: identity?.nameVariants ?? [],
    slackTarget: identity?.slackTarget ?? null,
    backendUrl: backendUrl || null,
  });
}

function stopCapture() {
  chrome.runtime.sendMessage({ type: STOP_CAPTURE, target: 'offscreen' });
}

async function stopIfCapturingTab(tabId) {
  const state = await getSessionState();
  if (state.status === 'capturing' && state.tabId === tabId) {
    stopCapture();
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (!message || typeof message.type !== 'string') return;

  switch (message.type) {
    case START_CAPTURE:
      if (message.target === 'background') startCapture();
      break;
    case STOP_CAPTURE:
      if (message.target === 'background') stopCapture();
      break;
    case SPEAKER_OVERRIDE:
      if (message.target === 'background') {
        chrome.runtime.sendMessage({
          type: SPEAKER_OVERRIDE,
          target: 'offscreen',
          label: message.label,
          name: message.name,
        });
      }
      break;
    case CAPTURE_STARTED:
      if (message.target === 'sidepanel') setSessionState({ status: 'capturing' });
      break;
    case CAPTURE_STOPPED:
      if (message.target === 'sidepanel') {
        setSessionState(IDLE_STATE);
        closeOffscreenDocument();
      }
      break;
    case CAPTURE_ERROR:
      if (message.target === 'sidepanel') {
        setSessionState({ status: 'error' });
        closeOffscreenDocument();
      }
      break;
    default:
      break;
  }
});

// Auto-stop: captured tab closed.
chrome.tabs.onRemoved.addListener((tabId) => {
  stopIfCapturingTab(tabId);
});

// Auto-stop: captured tab navigated away from meet.google.com.
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!changeInfo.url) return;
  if (tabHostname(changeInfo.url) !== 'meet.google.com') {
    stopIfCapturingTab(tabId);
  }
});
