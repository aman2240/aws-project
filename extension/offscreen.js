// Runs in the offscreen document. Owns the actual audio pipeline:
// getUserMedia on the tab-capture stream, the Web Audio graph (mono
// downmix -> ScriptProcessorNode -> silent sink, never
// audioContext.destination — that would play the meeting audio out
// loud a second time), PCM16 encoding of each buffer, and the
// WebSocket relay to the backend, including bounded reconnection. Also
// owns sending the session_init identity handshake as the first
// message on every (re)connection, and relaying speaker_override
// submissions as control messages on the same socket.

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

const DEFAULT_BACKEND_URL = 'ws://localhost:8000/ws/transcribe';
const SAMPLE_RATE = 16000;
const BUFFER_SIZE = 4096;
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 3000;

// Holds every live piece of the current capture session, or null when
// idle. Reassigned wholesale on start/stop rather than mutated field by
// field piecemeal, so there's never a "half-torn-down" pipeline lying
// around for a stray event to touch.
let pipeline = null;

function broadcast(message) {
  chrome.runtime.sendMessage({ ...message, target: 'sidepanel' });
}

function floatTo16BitPCM(input) {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const clamped = Math.max(-1, Math.min(1, input[i]));
    output[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
  }
  return output;
}

function connectWebSocket(backendUrl) {
  const ws = new WebSocket(backendUrl);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    if (!pipeline) return;
    pipeline.retryCount = 0;
    // Must be the very first thing sent on this socket, before any
    // audio bytes — onaudioprocess only sends once readyState is OPEN,
    // and that can't happen before this synchronous onopen body (which
    // just made it OPEN) finishes running, so ordering is guaranteed.
    // Sent on every (re)connection, not just the first, since the
    // backend expects it as the first frame of every new connection.
    ws.send(
      JSON.stringify({
        type: 'session_init',
        watched_user_name_variants: pipeline.watchedUserNameVariants,
        slack_target: pipeline.slackTarget,
      })
    );
    broadcast({ type: CONNECTION_STATUS, status: 'connected' });
  };

  // Every server->client frame is JSON here (audio only ever flows
  // client->server): the session-id announcement (no "type" field, just
  // {meeting_session_id}), decision_batch/decision_status_update pushes,
  // transcript events, and error messages all arrive on this same
  // handler and are told apart by shape/"type" rather than needing
  // separate sockets or endpoints.
  ws.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!payload || typeof payload !== 'object') return;

    if (!payload.type && typeof payload.meeting_session_id === 'string') {
      broadcast({ type: SESSION_ID_ASSIGNED, sessionId: payload.meeting_session_id });
      return;
    }

    switch (payload.type) {
      case 'decision_batch':
        broadcast({
          type: DECISION_BATCH,
          meetingId: payload.meeting_id,
          decisions: payload.decisions,
        });
        break;
      case 'decision_status_update':
        broadcast({
          type: DECISION_STATUS_UPDATE,
          decisionId: payload.decision_id,
          status: payload.status,
          approvedBy: payload.approved_by,
        });
        break;
      default:
        // Transcript events and "error" messages aren't this phase's
        // concern — left for whichever future phase relays them.
        break;
    }
  };

  ws.onclose = () => {
    if (!pipeline || pipeline.deliberateClose) return;

    if (pipeline.retryCount >= MAX_RETRIES) {
      broadcast({ type: CAPTURE_ERROR, message: 'Connection lost' });
      teardownPipeline();
      return;
    }

    pipeline.retryCount += 1;
    broadcast({ type: CONNECTION_STATUS, status: 'reconnecting' });
    setTimeout(() => {
      if (!pipeline || pipeline.deliberateClose) return;
      pipeline.ws = connectWebSocket(pipeline.backendUrl);
    }, RETRY_DELAY_MS);
  };

  return ws;
}

async function startCapture(streamId, watchedUserNameVariants, slackTarget, backendUrl) {
  // Passed in from background.js (chrome.storage.local read there,
  // not here — see the START_CAPTURE handler below for why), with the
  // same default-fallback this used to apply itself.
  backendUrl = backendUrl || DEFAULT_BACKEND_URL;

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId,
      },
    },
    video: false,
  });

  const audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
  const source = audioContext.createMediaStreamSource(stream);
  // Force mono downmix explicitly rather than relying on default
  // Web Audio channel-interpretation behavior.
  source.channelCount = 1;
  source.channelCountMode = 'explicit';

  const processor = audioContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
  // Silent sink: processed audio is routed here, never to
  // audioContext.destination, so the meeting audio never plays out
  // loud a second time.
  const sink = audioContext.createMediaStreamDestination();

  pipeline = {
    stream,
    audioContext,
    source,
    processor,
    sink,
    ws: null,
    deliberateClose: false,
    retryCount: 0,
    backendUrl,
    watchedUserNameVariants: watchedUserNameVariants ?? [],
    slackTarget: slackTarget ?? null,
  };

  processor.onaudioprocess = (event) => {
    if (!pipeline || !pipeline.ws || pipeline.ws.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    const pcm16 = floatTo16BitPCM(input);
    pipeline.ws.send(pcm16.buffer);
  };

  source.connect(processor);
  processor.connect(sink);

  broadcast({ type: CONNECTION_STATUS, status: 'connecting' });
  pipeline.ws = connectWebSocket(backendUrl);

  broadcast({ type: CAPTURE_STARTED });
}

function teardownPipeline() {
  if (!pipeline) return;

  pipeline.deliberateClose = true;

  pipeline.processor.onaudioprocess = null;
  pipeline.processor.disconnect();
  pipeline.source.disconnect();
  pipeline.sink.disconnect();
  pipeline.audioContext.close();
  pipeline.stream.getTracks().forEach((track) => track.stop());
  if (pipeline.ws) pipeline.ws.close();

  pipeline = null;
}

function stopCapture() {
  if (!pipeline) return;
  teardownPipeline();
  broadcast({ type: CAPTURE_STOPPED });
}

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.target !== 'offscreen') return;

  switch (message.type) {
    case START_CAPTURE:
      startCapture(
        message.streamId,
        message.watchedUserNameVariants,
        message.slackTarget,
        message.backendUrl
      ).catch(
        (error) => {
          // Logged here (not just relayed) since this .catch() would
          // otherwise silently swallow the exception — the side panel
          // only sees error.message as text, never a stack trace, so
          // without this line the offscreen document's own console
          // (chrome://extensions -> Inspect views -> offscreen.html)
          // is the only place the real cause is ever visible.
          console.error('startCapture failed:', error);
          // The offscreen document closes itself moments after this
          // broadcast (background.js's CAPTURE_ERROR handler calls
          // closeOffscreenDocument()), so its own console — and this
          // console.error above — is only inspectable in a brief
          // window that's easy to miss. Forwarding the stack alongside
          // the message means the side panel's own (stable, already
          // open) console can show the full detail instead.
          broadcast({
            type: CAPTURE_ERROR,
            message: error.message || 'Failed to start capture',
            stack: error.stack || null,
          });
        }
      );
      break;
    case STOP_CAPTURE:
      stopCapture();
      break;
    case SPEAKER_OVERRIDE:
      if (pipeline && pipeline.ws && pipeline.ws.readyState === WebSocket.OPEN) {
        pipeline.ws.send(
          JSON.stringify({ type: 'speaker_override', label: message.label, name: message.name })
        );
      }
      break;
    default:
      break;
  }
});
