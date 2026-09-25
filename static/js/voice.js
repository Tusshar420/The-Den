// Voice in the browser: the microphone in, speech out. It never has a pipeline of its own - what you say
// becomes text and goes through POST /voice/command, which is the same path typed requests take
// (same plan, same approvals). Providers are chosen in Settings; the server can transcribe with a local
// model (Whisper) or the browser can do it itself.

import { apiKey, store } from './api.js';
import { octoApi } from './octopus-data.js';

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null, recorder = null, stream = null, speakId = 0;

// One audio element for the whole session. Browsers only let a page play sound after a tap, so the mic tap
// "unlocks" this element and every later reply (which arrives long after that tap) reuses it.
const player = new Audio();
const SILENCE = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';
export function unlockAudio() {
  try { player.src = SILENCE; player.play().catch(() => {}); } catch {}
  try { window.speechSynthesis && window.speechSynthesis.speak(new SpeechSynthesisUtterance('')); } catch {}
}

const v = () => store.ui.octo.voice;
const setVoice = patch => store.set(u => ({ octo: { ...u.octo, voice: { ...u.octo.voice, ...patch } } }));
const settings = () => store.data.settings || {};

// What this browser + server combination can do.
export function micMode() {
  const stt = settings().voice_stt || 'browser';
  const server = (store.data.octo.voice || {}).stt || [];
  if (stt === 'browser') return SR ? 'browser' : null;
  const p = server.find(x => x.id === stt);
  return p && p.available && navigator.mediaDevices && window.MediaRecorder ? 'server' : (SR ? 'browser' : null);
}
export const micAvailable = () => micMode() !== null;

// ---------------------------------------------------------------- choosing a voice
// Browser voices differ per device, so that choice stays on the device; the macOS voice is a server setting.
const VOICE_KEY = 'octopus-voice';
export const ttsMode = () => (settings().voice_tts || 'browser') === 'macos' ? 'macos' : 'browser';
export const browserVoiceName = () => { try { return localStorage.getItem(VOICE_KEY) || ''; } catch { return ''; } };
export const setBrowserVoiceName = name => { try { name ? localStorage.setItem(VOICE_KEY, name) : localStorage.removeItem(VOICE_KEY); } catch {} };

// [{ name, language, on }] for the provider in use - the device's own voices, or the Mac's.
export function voiceChoices() {
  const mac = ttsMode() === 'macos';
  const all = mac
    ? ((store.data.octo.voice || {}).voices || []).map(x => ({ name: x.name, language: x.language }))
    : (window.speechSynthesis ? window.speechSynthesis.getVoices() : []).map(x => ({ name: x.name, language: x.lang }));
  const current = mac ? (settings().voice_tts_voice || '') : browserVoiceName();
  const lang = (navigator.language || 'en').slice(0, 2).toLowerCase();
  const mine = x => x.language.toLowerCase().replace('_', '-').startsWith(lang);
  return all.filter(mine).concat(all.filter(x => !mine(x))).map(x => ({ ...x, on: x.name === current }));
}
// Voices load a moment after the page does; redraw once they are there.
if (window.speechSynthesis) window.speechSynthesis.addEventListener('voiceschanged', () => store.set({}));

// ---------------------------------------------------------------- speaking
export function stopSpeaking() {
  speakId++;
  try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch {}
  try { player.pause(); } catch {}
}

export async function speak(text) {
  text = (text || '').trim();
  if (!text) return;
  stopSpeaking();
  const id = speakId;
  setVoice({ state: 'speaking' });
  try {
    if (ttsMode() === 'macos') {
      const res = await fetch('/voice/speak', { method: 'POST', headers: { 'X-API-Key': apiKey(), 'Content-Type': 'application/json' }, body: JSON.stringify({ text, provider: 'macos' }) });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const blob = await res.blob();
      if (id !== speakId) return;
      player.src = URL.createObjectURL(blob);
      await new Promise((resolve, reject) => { player.onended = resolve; player.onpause = resolve; player.onerror = reject; player.play().catch(reject); });
    } else if (window.speechSynthesis) {
      await new Promise(resolve => {
        const clipped = text.length > 600 ? text.slice(0, 600) : text;
        const u = new SpeechSynthesisUtterance(clipped);
        const name = browserVoiceName(), voice = name && window.speechSynthesis.getVoices().find(x => x.name === name);
        if (voice) { u.voice = voice; u.lang = voice.lang; }
        // Some browsers never fire "end" for long or interrupted speech, which would freeze the conversation.
        const guard = setTimeout(resolve, 6000 + clipped.length * 90);
        u.onend = u.onerror = () => { clearTimeout(guard); resolve(); };
        window.speechSynthesis.speak(u);
      });
    }
  } catch (e) {
    if (id === speakId) setVoice({ error: 'Could not speak: ' + e.message });
  } finally {
    if (id === speakId && v().state === 'speaking') setVoice({ state: 'idle' });
  }
}

// ---------------------------------------------------------------- listening
// listen() delivers exactly one of: onText(text) / onAudio(blob) - what was said - or onEmpty(failed) when
// nothing was said (failed = the mic itself didn't work; the error is already on screen). Each is dropped if
// the state has moved on from "listening" (cancelListening, ending the conversation).
export function stopListening() {
  try { recognition && recognition.stop(); } catch {}
  try { recorder && recorder.state !== 'inactive' && recorder.stop(); } catch {}
}
export function cancelListening() {
  if (v().state === 'listening') setVoice({ state: 'idle', text: '' });
  try { recognition && recognition.abort(); } catch {}
  try { recorder && recorder.state !== 'inactive' && recorder.stop(); } catch {}
}

export function listen(onText, onAudio, onEmpty = () => {}) {
  const mode = micMode();
  if (!mode) { setVoice({ state: 'idle', error: 'This browser can\'t listen. Use Safari or Chrome, or choose a server-side voice input in Settings.' }); return onEmpty(true); }
  stopSpeaking();
  setVoice({ state: 'listening', text: '', error: '', heard: '' });
  if (mode === 'browser') return listenBrowser(onText, onEmpty);
  return listenServer(onAudio, onEmpty);
}

function listenBrowser(onText, onEmpty) {
  const rec = recognition = new SR();
  rec.lang = navigator.language || 'en-US';
  rec.interimResults = true;
  rec.maxAlternatives = 1;
  let finalText = '', failed = false;
  const fail = error => { failed = true; if (v().state === 'listening') setVoice({ state: 'idle', error }); };
  rec.onresult = e => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += t; else interim += t;
    }
    setVoice({ text: (finalText + interim).trim() });
  };
  rec.onerror = e => {
    if (e.error === 'aborted') return;
    if (e.error === 'no-speech') return;                       // handled as "nothing said" in onend
    fail(e.error === 'not-allowed' || e.error === 'service-not-allowed' ? 'Microphone permission was denied.' : 'Speech recognition failed: ' + e.error);
  };
  rec.onend = () => {
    if (recognition === rec) recognition = null;
    const text = finalText.trim() || (v().state === 'listening' ? v().text.trim() : '');
    if (v().state !== 'listening') return failed ? onEmpty(true) : undefined;
    if (text) onText(text); else { setVoice({ state: 'idle' }); onEmpty(false); }
  };
  try { rec.start(); } catch (e) { recognition = null; setVoice({ state: 'idle', error: e.message }); onEmpty(true); }
}

async function listenServer(onAudio, onEmpty) {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch { setVoice({ state: 'idle', error: 'Microphone permission was denied.' }); return onEmpty(true); }
  const chunks = [];
  const mime = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm'].find(m => window.MediaRecorder.isTypeSupported(m)) || '';
  const rec = recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const mine = stream;
  let heard = false, ctx = null, timer = null;
  const stop = () => { try { rec.state !== 'inactive' && rec.stop(); } catch {} };
  rec.ondataavailable = e => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    clearInterval(timer); clearTimeout(cap);
    try { ctx && ctx.close(); } catch {}
    mine.getTracks().forEach(t => t.stop());
    if (recorder === rec) recorder = null;
    if (v().state !== 'listening') return;
    if (!heard) { setVoice({ state: 'idle' }); return onEmpty(false); }
    onAudio(new Blob(chunks, { type: rec.mimeType || 'audio/webm' }));
  };
  rec.start();
  // Stop by itself when you stop talking (or never start): a conversation has no button to tap between turns.
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    const an = ctx.createAnalyser(); an.fftSize = 1024;
    ctx.createMediaStreamSource(mine).connect(an);
    const buf = new Uint8Array(an.fftSize), t0 = Date.now();
    let lastLoud = t0;
    timer = setInterval(() => {
      an.getByteTimeDomainData(buf);
      let peak = 0; for (const x of buf) peak = Math.max(peak, Math.abs(x - 128));
      const now = Date.now();
      if (peak > 12) { heard = true; lastLoud = now; }
      else if (heard && now - lastLoud > 1300) stop();
      else if (!heard && now - t0 > 8000) stop();
    }, 100);
  } catch { heard = true; }                                    // no level meter: fall back to tap-to-stop
  const cap = setTimeout(stop, 30000);
}

// Send a recording to the server (it transcribes with the chosen provider, then runs the request).
export async function sendAudio(blob) {
  const res = await fetch('/voice/command', { method: 'POST', headers: { 'X-API-Key': apiKey(), 'Content-Type': blob.type || 'audio/webm' }, body: blob });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

export const sendText = (text, extra = {}) => octoApi.voiceCommand({ text, ...extra });
