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

// The voice picked by hand in the Ask box. While it is set it reads every language; "" = automatic, where each
// language uses its own default voice (below).
const MANUAL_KEY = 'octopus-voice-manual';
export const manualVoiceName = () => {
  if (ttsMode() === 'macos') return settings().voice_manual || '';
  try { return localStorage.getItem(MANUAL_KEY) || ''; } catch { return ''; }
};
export const setBrowserManualVoice = name => { try { name ? localStorage.setItem(MANUAL_KEY, name) : localStorage.removeItem(MANUAL_KEY); } catch {} };

// ---------------------------------------------------------------- languages
// Which language a reply is in decides which voice reads it. The rules come from the server (GET /voice ->
// octopus/voice/languages.py) so the browser and the Mac detect identically. Until they load: English.
const languages = () => (store.data.octo.voice || {}).languages || [];
const LANG_KEY = 'octopus-voice-languages';
const shortCode = tag => (tag || '').slice(0, 2).toLowerCase();

export function detectLanguage(text) {
  const t = (text || '').replace(/```[\s\S]*?```|`[^`]*`|https?:\/\/\S+/g, ' ');
  const letters = (t.match(/\p{L}/gu) || []).length;
  if (!letters) return 'en';
  for (const l of languages()) {
    if (l.script && (t.match(new RegExp(l.script, 'gu')) || []).length / letters >= 0.3) return l.code;
  }
  const words = t.toLowerCase().match(/\p{L}+/gu) || [];
  const scores = languages().filter(l => l.words.length).map(l => [words.filter(w => l.words.includes(w)).length, l.code]).sort((a, b) => b[0] - a[0]);
  return scores.length > 1 && scores[0][0] >= 2 && scores[0][0] > scores[1][0] ? scores[0][1] : 'en';
}

// Voice chosen for each language: the Mac's are a server setting; a browser's are this device's own.
export function languageVoices() {
  if (ttsMode() === 'macos') return settings().voice_language_voices || {};
  try { return JSON.parse(localStorage.getItem(LANG_KEY) || '{}'); } catch { return {}; }
}
export function setBrowserLanguageVoice(code, name) {
  const all = { ...languageVoices() };
  name ? all[code] = name : delete all[code];
  try { localStorage.setItem(LANG_KEY, JSON.stringify(all)); } catch {}
}
export const allVoices = () => ttsMode() === 'macos'
  ? ((store.data.octo.voice || {}).voices || []).map(x => ({ name: x.name, language: x.language }))
  : (window.speechSynthesis ? window.speechSynthesis.getVoices() : []).map(x => ({ name: x.name, language: x.lang }));
// The voices that can read `code`, e.g. "hi" -> Lekha (hi_IN), Kalpana (hi-IN).
export const voicesForLanguage = code => allVoices().filter(x => shortCode(x.language) === code);
export const speechRate = () => Number(settings().voice_rate) || 1;

// The language the mic listens for. Browser recognition hears one language per turn and can't guess it, so it
// follows the conversation (a Hindi answer means Hindi next) and the voice pane lets you switch by hand.
// '' = this device's own language.
export const listenLanguage = () => v().lang || shortCode(navigator.language) || 'en';
export const setListenLanguage = code => setVoice({ lang: code });
// "en" plus every language you have picked a voice for on the Voice page.
export function listenChoices() {
  const codes = ['en', ...Object.keys(languageVoices()).filter(c => c !== 'en')];
  return codes.map(code => ({ code, label: (languages().find(l => l.code === code) || {}).label || code, on: code === listenLanguage() }));
}
// Follow a language you or Octopus just used, if it is one you have a voice for.
export function followLanguage(...texts) {
  for (const t of texts) {
    const code = detectLanguage(t);
    if (code !== 'en' && code in languageVoices()) return setListenLanguage(code);
  }
}
const listenTag = code => {
  if (code === shortCode(navigator.language)) return navigator.language;
  const voice = voicesForLanguage(code)[0];
  return voice ? voice.language.replace('_', '-') : code === 'en' ? 'en-US' : code;
};

// [{ name, language, on }] for the provider in use - the device's own voices, or the Mac's. `on` marks the
// default voice, or with { manual: true } the one picked by hand in the Ask box.
export function voiceChoices({ manual = false } = {}) {
  const mac = ttsMode() === 'macos';
  const all = allVoices();
  const current = manual ? manualVoiceName() : mac ? (settings().voice_tts_voice || '') : browserVoiceName();
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
        const all = window.speechSynthesis.getVoices(), lang = detectLanguage(text);
        const named = n => n && all.find(x => x.name === n);
        // A voice picked by hand wins; else the voice chosen for this language, else the default voice if it speaks it,
        // else let the device pick one that does.
        const voice = named(manualVoiceName()) || named(languageVoices()[lang]) || (named(browserVoiceName()) && shortCode(named(browserVoiceName()).lang) === lang ? named(browserVoiceName()) : null);
        if (voice) { u.voice = voice; u.lang = voice.lang; } else if (lang !== shortCode(navigator.language)) u.lang = lang;
        u.rate = speechRate();
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
  rec.lang = listenTag(listenLanguage());
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

// Dictation: speech becomes text for a text box (the New task sheet) instead of a request. Calls done(text) with
// what was said ('' if nothing / failed). Tapping again while listening ends it.
export function dictate(done) {
  if (v().state === 'listening') return stopListening();
  listen(done, async blob => {
    try {
      const res = await fetch('/voice/transcribe', { method: 'POST', headers: { 'X-API-Key': apiKey(), 'Content-Type': blob.type || 'audio/webm' }, body: blob });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || res.statusText);
      done((body.text || '').trim());
    } catch (e) { setVoice({ state: 'idle', error: e.message }); done(''); }
  }, () => done(''));
}
