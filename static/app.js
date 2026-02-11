// ── State ──
let ws = null;
let mediaRecorder = null;
let audioContext = null;
let scriptProcessor = null;
let mediaStream = null;
let webmChunks = [];
let meetingId = null;
let timerInterval = null;
let recordStartTime = null;

// ── DOM References ──
const btnRecord = document.getElementById('btn-record');
const btnStop = document.getElementById('btn-stop');
const meetingTitleInput = document.getElementById('meeting-title');
const languageSelect = document.getElementById('language-select');
const recordingStatus = document.getElementById('recording-status');
const recordingTimer = document.getElementById('recording-timer');
const micError = document.getElementById('mic-error');
const transcriptArea = document.getElementById('transcript-area');
const committedSegments = document.getElementById('committed-segments');
const partialText = document.getElementById('partial-text');
const meetingsList = document.getElementById('meetings-list');
const noMeetings = document.getElementById('no-meetings');
const btnBack = document.getElementById('btn-back');
const btnDelete = document.getElementById('btn-delete');
const detailTitle = document.getElementById('detail-title');
const detailDate = document.getElementById('detail-date');
const detailDuration = document.getElementById('detail-duration');
const detailStatus = document.getElementById('detail-status');
const audioPlayerContainer = document.getElementById('audio-player-container');
const audioPlayer = document.getElementById('audio-player');
const detailSegments = document.getElementById('detail-segments');

// ── Navigation ──
document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));

  const view = document.getElementById(`view-${name}`);
  if (view) view.classList.add('active');

  const navBtn = document.querySelector(`.nav-btn[data-view="${name}"]`);
  if (navBtn) navBtn.classList.add('active');

  if (name === 'history') loadMeetings();
}

// ── Recording ──
btnRecord.addEventListener('click', startRecording);
btnStop.addEventListener('click', stopRecording);

async function startRecording() {
  micError.classList.add('hidden');
  micError.textContent = '';

  // Get mic access
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    micError.textContent = getMicErrorMessage(err);
    micError.classList.remove('hidden');
    return;
  }

  // UI state
  btnRecord.disabled = true;
  btnStop.disabled = false;
  meetingTitleInput.disabled = true;
  languageSelect.disabled = true;
  recordingStatus.classList.remove('hidden');
  transcriptArea.classList.remove('hidden');
  committedSegments.innerHTML = '';
  partialText.textContent = '';
  webmChunks = [];
  meetingId = null;

  // Start timer
  recordStartTime = Date.now();
  timerInterval = setInterval(updateTimer, 1000);

  // 1) MediaRecorder for webm (playback file)
  mediaRecorder = new MediaRecorder(mediaStream, { mimeType: getWebmMimeType() });
  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) webmChunks.push(e.data);
  };
  mediaRecorder.start(1000); // collect chunks every second

  // 2) AudioContext + ScriptProcessor for PCM streaming
  audioContext = new AudioContext();
  const source = audioContext.createMediaStreamSource(mediaStream);

  // Buffer size 4096 gives ~85ms at 48kHz, good balance
  scriptProcessor = audioContext.createScriptProcessor(4096, 1, 1);
  const nativeSampleRate = audioContext.sampleRate;

  scriptProcessor.onaudioprocess = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const inputData = e.inputBuffer.getChannelData(0); // Float32 mono

    // Downsample to 16kHz if needed
    let pcmFloat;
    if (nativeSampleRate !== 16000) {
      pcmFloat = downsample(inputData, nativeSampleRate, 16000);
    } else {
      pcmFloat = inputData;
    }

    // Convert Float32 -> Int16
    const pcmInt16 = float32ToInt16(pcmFloat);

    // Base64 encode (chunked for large arrays)
    const base64 = int16ToBase64(pcmInt16);

    ws.send(JSON.stringify({ type: 'audio', data: base64 }));
  };

  source.connect(scriptProcessor);
  scriptProcessor.connect(audioContext.destination); // required for processing to work

  // 3) WebSocket connection
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws/transcribe`);

  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: 'start',
      title: meetingTitleInput.value.trim() || undefined,
      language: languageSelect.value || undefined,
    }));
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    handleServerMessage(msg);
  };

  ws.onerror = () => {
    showError('WebSocket connection error.');
  };

  ws.onclose = () => {
    // Normal close after stop, no action needed
  };
}

async function stopRecording() {
  btnStop.disabled = true;

  // Stop timer
  clearInterval(timerInterval);
  timerInterval = null;

  // Stop ScriptProcessor and AudioContext
  if (scriptProcessor) {
    scriptProcessor.disconnect();
    scriptProcessor = null;
  }
  if (audioContext) {
    await audioContext.close();
    audioContext = null;
  }

  // Send stop to server
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'stop' }));
  }

  // Stop MediaRecorder and upload webm
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.onstop = async () => {
      const blob = new Blob(webmChunks, { type: mediaRecorder.mimeType });
      webmChunks = [];

      if (meetingId && blob.size > 0) {
        try {
          await fetch(`/api/meetings/${meetingId}/audio`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/octet-stream' },
            body: blob,
          });
        } catch (err) {
          console.error('Failed to upload audio:', err);
        }
      }

      // Close WebSocket after upload
      if (ws) {
        // Give server a moment to finalize
        setTimeout(() => {
          if (ws && ws.readyState === WebSocket.OPEN) ws.close();
          ws = null;
        }, 1500);
      }
    };
    mediaRecorder.stop();
  }

  // Stop mic
  if (mediaStream) {
    mediaStream.getTracks().forEach(t => t.stop());
    mediaStream = null;
  }

  // UI reset
  btnRecord.disabled = false;
  meetingTitleInput.disabled = false;
  languageSelect.disabled = false;
  recordingStatus.classList.add('hidden');
}

// ── Server Message Handling ──
function handleServerMessage(msg) {
  switch (msg.type) {
    case 'session_started':
      meetingId = msg.meeting_id;
      break;

    case 'partial':
      partialText.textContent = msg.text || '';
      autoScrollTranscript();
      break;

    case 'committed':
      if (msg.text && msg.text.trim()) {
        const div = document.createElement('div');
        div.className = 'segment';
        div.textContent = msg.text;
        committedSegments.appendChild(div);
        partialText.textContent = '';
        autoScrollTranscript();
      }
      break;

    case 'error':
      showError(msg.message || 'Unknown server error');
      break;
  }
}

function autoScrollTranscript() {
  const container = document.getElementById('transcript-content');
  container.scrollTop = container.scrollHeight;
}

// ── Audio Processing Helpers ──

function downsample(buffer, fromRate, toRate) {
  if (fromRate === toRate) return buffer;
  const ratio = fromRate / toRate;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);
  for (let i = 0; i < newLength; i++) {
    const srcIndex = i * ratio;
    const srcIndexFloor = Math.floor(srcIndex);
    const srcIndexCeil = Math.min(srcIndexFloor + 1, buffer.length - 1);
    const frac = srcIndex - srcIndexFloor;
    result[i] = buffer[srcIndexFloor] * (1 - frac) + buffer[srcIndexCeil] * frac;
  }
  return result;
}

function float32ToInt16(float32Array) {
  const int16 = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return int16;
}

function int16ToBase64(int16Array) {
  const bytes = new Uint8Array(int16Array.buffer);
  // Chunked btoa to avoid call stack overflow on large arrays
  const CHUNK_SIZE = 8192;
  let binary = '';
  for (let i = 0; i < bytes.length; i += CHUNK_SIZE) {
    const slice = bytes.subarray(i, i + CHUNK_SIZE);
    for (let j = 0; j < slice.length; j++) {
      binary += String.fromCharCode(slice[j]);
    }
  }
  return btoa(binary);
}

// ── Timer ──
function updateTimer() {
  if (!recordStartTime) return;
  const elapsed = Math.floor((Date.now() - recordStartTime) / 1000);
  const m = String(Math.floor(elapsed / 60)).padStart(2, '0');
  const s = String(elapsed % 60).padStart(2, '0');
  recordingTimer.textContent = `${m}:${s}`;
}

// ── Mic Error Messages ──
function getMicErrorMessage(err) {
  if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
    return 'Microphone access denied. Please allow microphone permission in your browser settings and reload the page.';
  }
  if (err.name === 'NotFoundError') {
    return 'No microphone found. Please connect a microphone and try again.';
  }
  if (err.name === 'NotReadableError') {
    return 'Microphone is in use by another application. Please close it and try again.';
  }
  return `Microphone error: ${err.message}`;
}

function showError(message) {
  micError.textContent = message;
  micError.classList.remove('hidden');
}

function getWebmMimeType() {
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
  for (const t of types) {
    if (MediaRecorder.isTypeSupported(t)) return t;
  }
  return '';
}

// ── Meetings List ──
async function loadMeetings() {
  try {
    const res = await fetch('/api/meetings');
    const meetings = await res.json();

    meetingsList.innerHTML = '';

    if (!meetings.length) {
      noMeetings.classList.remove('hidden');
      return;
    }

    noMeetings.classList.add('hidden');

    for (const m of meetings) {
      const card = document.createElement('div');
      card.className = 'meeting-card';
      card.onclick = () => showMeetingDetail(m.id);

      const title = m.title || 'Untitled Meeting';
      const date = formatDate(m.created_at);
      const duration = m.duration ? formatDuration(m.duration) : '';

      card.innerHTML = `
        <div class="meeting-card-info">
          <h3>${escapeHtml(title)}</h3>
          <p>${date}${duration ? ' &middot; ' + duration : ''}</p>
        </div>
        <span class="meeting-card-arrow">&rsaquo;</span>
      `;

      meetingsList.appendChild(card);
    }
  } catch (err) {
    meetingsList.innerHTML = '<p class="error-banner">Failed to load meetings.</p>';
  }
}

// ── Meeting Detail ──
async function showMeetingDetail(id) {
  try {
    const res = await fetch(`/api/meetings/${id}`);
    if (!res.ok) throw new Error('Meeting not found');
    const data = await res.json();
    const meta = data.metadata || {};
    const transcript = data.transcript || [];

    detailTitle.textContent = meta.title || 'Untitled Meeting';
    detailDate.textContent = formatDate(meta.created_at);
    detailDuration.textContent = meta.duration ? formatDuration(meta.duration) : '';

    detailStatus.textContent = meta.status || 'completed';
    detailStatus.className = 'status-badge ' + (meta.status || 'completed');

    // Audio player — always try to load; hide on error (backend returns 404 if no file)
    audioPlayer.src = `/api/meetings/${id}/audio`;
    audioPlayerContainer.classList.remove('hidden');
    audioPlayer.onerror = () => {
      audioPlayerContainer.classList.add('hidden');
    };

    // Transcript segments
    detailSegments.innerHTML = '';
    if (transcript.length) {
      for (const seg of transcript) {
        const div = document.createElement('div');
        div.className = 'segment';
        div.textContent = seg.text || seg;
        detailSegments.appendChild(div);
      }
    } else {
      detailSegments.innerHTML = '<p style="color: var(--text-muted)">No transcript available.</p>';
    }

    // Wire up delete
    btnDelete.onclick = () => deleteMeeting(id);

    // Show detail view
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('view-detail').classList.add('active');
  } catch (err) {
    showError('Failed to load meeting details.');
  }
}

btnBack.addEventListener('click', () => showView('history'));

async function deleteMeeting(id) {
  if (!confirm('Delete this meeting? This cannot be undone.')) return;
  try {
    await fetch(`/api/meetings/${id}`, { method: 'DELETE' });
    showView('history');
  } catch (err) {
    showError('Failed to delete meeting.');
  }
}

// ── Utility ──
function formatDate(dateStr) {
  if (!dateStr) return '';
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return dateStr;
  }
}

function formatDuration(seconds) {
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
