// Pictures attached to a prompt (Ask box, New task sheet, chat composer).
// Pick or paste -> each file is uploaded once (POST /uploads) -> the prompt is sent with the upload ids.
// A draft is a plain array kept in store.ui: [{ key, name, url, id, state, error }] where url is a local
// preview and state is 'uploading' | 'ready' | 'error'.

import { apiKey } from './api.js';
import { escapeHTML as esc } from './format.js';

export const MAX_IMAGES = 6;

let seq = 0;
let redraw = () => {};
export const setRedraw = fn => { redraw = fn; };

async function upload(file) {
  const res = await fetch('/uploads', {
    method: 'POST',
    headers: { 'X-API-Key': apiKey(), 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// Adds what fits to `list` and starts uploading it. Returns how many files were left out
// (not pictures, or past the limit) so the caller can say so.
export function addImages(list, files) {
  const pics = [...files].filter(f => f.type.startsWith('image/'));
  const room = Math.max(MAX_IMAGES - list.length, 0);
  for (const file of pics.slice(0, room)) {
    const item = { key: ++seq, name: file.name || 'pasted picture', url: URL.createObjectURL(file), state: 'uploading' };
    list.push(item);
    upload(file).then(r => { item.id = r.id; item.state = 'ready'; }, e => { item.state = 'error'; item.error = e.message; }).finally(redraw);
  }
  redraw();
  return files.length - Math.min(pics.length, room);
}

export function removeImage(list, key) {
  const i = list.findIndex(x => x.key === key);
  if (i < 0) return;
  URL.revokeObjectURL(list[i].url);
  list.splice(i, 1);
}

// Empties a draft after it was sent. The local previews are kept as the sent message's thumbnails.
export function clearSent(list) {
  list.forEach(i => { if (i.id) blobs.set(i.id, i.url); else URL.revokeObjectURL(i.url); });
  list.length = 0;
}

export const readyIds = list => list.filter(i => i.state === 'ready').map(i => i.id);

// Why the draft can't be sent yet ('' when it can).
export function imageProblem(list) {
  if (list.some(i => i.state === 'uploading')) return 'Still uploading your picture…';
  if (list.some(i => i.state === 'error')) return 'A picture failed to upload - remove it or attach it again';
  return '';
}

// The row of thumbnails above a box. `target` is which box (ask | palette | reply).
export function imageStrip(list, target) {
  if (!list || !list.length) return '';
  return `<div class="attach-strip" data-noswipe>${list.map(i => `<div class="attach-thumb is-${i.state}" title="${esc(i.error || i.name)}">
    <img src="${i.url}" alt="${esc(i.name)}">
    ${i.state === 'uploading' ? '<span class="spinner spinner--tool"></span>' : i.state === 'error' ? '<span class="attach-thumb__err">!</span>' : ''}
    <button class="attach-thumb__x" data-action="removeImage" data-target="${target}" data-img="${i.key}" aria-label="Remove ${esc(i.name)}">×</button>
  </div>`).join('')}</div>`;
}

// ---------------------------------------------------------------- pictures already sent
// <img> can't send the API key, so a sent picture is fetched once with it and cached as a local URL.
const blobs = new Map();   // upload id -> local URL | 'loading' | 'failed'

function localUrl(id) {
  const v = blobs.get(id);
  if (v) return v === 'loading' || v === 'failed' ? '' : v;
  blobs.set(id, 'loading');
  fetch('/uploads/' + encodeURIComponent(id), { headers: { 'X-API-Key': apiKey() } })
    .then(r => (r.ok ? r.blob() : Promise.reject(new Error(r.statusText))))
    .then(b => blobs.set(id, URL.createObjectURL(b)), () => blobs.set(id, 'failed'))
    .finally(redraw);
  return '';
}

// Thumbnails inside a sent message's bubble; tapping one opens it full size in a new tab.
export function sentStrip(ids) {
  if (!ids || !ids.length) return '';
  return `<div class="msg-you__imgs">${ids.map(id => {
    const u = localUrl(id);
    return u ? `<a href="${u}" target="_blank" rel="noopener"><img class="attach-thumb attach-thumb--sent" src="${u}" alt="Attached picture"></a>`
      : `<span class="attach-thumb attach-thumb--sent attach-thumb--wait"></span>`;
  }).join('')}</div>`;
}
