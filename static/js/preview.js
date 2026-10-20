// Preview of a project document or an attached picture, in a sheet. Pictures, PDFs, plain text, audio and
// video show in place; anything else gets its name, size and a Download button.
// The files need the API key, so each is fetched once and shown from a local (blob:) URL.
//
// A blob: URL runs with this page's origin, so nothing in a file is ever rendered as a page: text is shown
// escaped, an SVG only ever goes in an <img>, and a PDF is framed only once its first bytes say it is one.
//
// store.ui.preview: { src, name, size, mime, docId, added, kind, state, url, text, cut, error }
//   kind  'image' | 'pdf' | 'text' | 'audio' | 'video' | '' (no preview)
//   state 'loading' | 'ready' | 'error'

import { store, apiKey } from './api.js';
import { escapeHTML as esc, ago } from './format.js';
import { icon, spinner } from './ui.js';
const TEXT_MAX = 256 * 1024;          // a longer text file shows its start
const EXT = {
  image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'svg', 'ico', 'heic', 'heif'],
  audio: ['mp3', 'm4a', 'wav', 'ogg', 'oga', 'aac', 'flac', 'opus'],
  video: ['mp4', 'm4v', 'mov', 'webm', 'ogv'],
  text: ['txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'yaml', 'yml', 'xml', 'html', 'htm', 'log', 'ini', 'toml', 'rst',
    'sql', 'py', 'js', 'mjs', 'ts', 'tsx', 'jsx', 'css', 'scss', 'sh', 'zsh', 'swift', 'kt', 'java', 'go', 'rs', 'rb', 'php',
    'c', 'h', 'cpp', 'hpp', 'cs', 'env', 'conf', 'cfg', 'plist', 'diff', 'patch', 'srt', 'vtt', 'tex'],
};
const IMAGE_MIME = { jpg: 'image/jpeg', svg: 'image/svg+xml', ico: 'image/x-icon' };

const extOf = name => (/\.([A-Za-z0-9]{1,10})$/.exec(name || '') || [])[1]?.toLowerCase() || '';
const isSvg = p => p.mime === 'image/svg+xml' || extOf(p.name) === 'svg';

export function kindOf(name, mime = '') {
  const ext = extOf(name);
  if (mime === 'application/pdf' || ext === 'pdf') return 'pdf';
  for (const k of ['image', 'audio', 'video']) if (mime.startsWith(k + '/') || EXT[k].includes(ext)) return k;
  if (mime.startsWith('text/') || /^application\/(json|xml|x-yaml|x-sh)/.test(mime) || EXT.text.includes(ext)) return 'text';
  return '';
}

let raw = null;       // the open file's bytes, for Download
let owned = '';       // a blob: URL made here, freed when the next file opens
let seq = 0;
let redraw = () => {};

function release() {
  if (owned) URL.revokeObjectURL(owned);
  owned = ''; raw = null;
}

// What to show for the bytes: { kind, url } or { kind: 'text', text, cut }. A file that isn't what its name
// says (a ".pdf" that isn't a PDF, a ".txt" full of zero bytes) gets no preview.
async function view(kind, blob, p) {
  if (kind === 'text') {
    const buf = new Uint8Array(await blob.slice(0, TEXT_MAX).arrayBuffer());
    if (buf.subarray(0, 8192).includes(0)) return { kind: '', odd: true };
    return { kind, text: new TextDecoder().decode(buf), cut: blob.size > TEXT_MAX };
  }
  if (kind === 'pdf') {
    const head = new TextDecoder('latin1').decode(await blob.slice(0, 1024).arrayBuffer());
    if (!head.includes('%PDF-')) return { kind: '', odd: true };
    return { kind, url: URL.createObjectURL(new Blob([blob], { type: 'application/pdf' })) };
  }
  const type = kind === 'image' ? (p.mime.startsWith('image/') ? p.mime : IMAGE_MIME[extOf(p.name)] || `image/${extOf(p.name) || 'png'}`) : p.mime;
  return { kind, url: URL.createObjectURL(new Blob([blob], { type })) };
}

// Thumbnails for the Documents grid: a picture is fetched once (with the key) and kept as a blob: URL.
// '' while loading or if it can't be shown, so the tile falls back to its file-type badge.
const THUMB_MAX = 8 * 1024 * 1024;
const thumbs = new Map();
export function docThumb(doc) {
  if (thumbs.has(doc.id)) return thumbs.get(doc.id);
  thumbs.set(doc.id, '');
  if (kindOf(doc.name, doc.mime || '') !== 'image' || doc.size > THUMB_MAX) return '';
  fetch(`/documents/${doc.id}`, { headers: { 'X-API-Key': apiKey() } })
    .then(res => (res.ok ? res.blob() : Promise.reject()))
    .then(blob => {
      const m = doc.mime || '';
      const type = m.startsWith('image/') ? m : IMAGE_MIME[extOf(doc.name)] || `image/${extOf(doc.name) || 'png'}`;
      thumbs.set(doc.id, URL.createObjectURL(new Blob([blob], { type })));
      redraw();
    }).catch(() => {});
  return '';
}

// Open the preview sheet. `item`: { src (an API path), name, size?, mime?, docId?, added? } - or `url` for a
// picture already fetched (a thumbnail on screen), which is shown as is.
export async function openPreview(set, item) {
  const my = ++seq;
  release();
  const p = { size: 0, mime: '', docId: null, added: null, ...item };
  const kind = kindOf(p.name, p.mime);
  set({ sheet: 'preview', preview: { ...p, kind, state: p.url ? 'ready' : 'loading', url: p.url || '', text: '', cut: false, error: '' } });
  if (p.url) return;
  try {
    const res = await fetch(p.src, { headers: { 'X-API-Key': apiKey() } });
    if (!res.ok) throw new Error(res.status === 404 ? 'That file is gone' : res.statusText || 'It could not be loaded');
    const blob = await res.blob();
    if (my !== seq) return;
    raw = blob;
    const out = kind ? await view(kind, blob, p) : { kind: '' };
    if (my !== seq) { if (out.url) URL.revokeObjectURL(out.url); return; }
    owned = out.url || '';
    patch({ state: 'ready', size: p.size || blob.size, url: '', text: '', cut: false, ...out });
  } catch (e) {
    if (my === seq) patch({ state: 'error', error: e.message || String(e) });
  }

  function patch(v) {
    const cur = store.ui.preview;
    if (cur && store.ui.sheet === 'preview') set({ preview: { ...cur, ...v } });
  }
}

// The browser couldn't show a picture / clip it was given (an .heic in Chrome, say): fall back to "no preview".
document.addEventListener('error', e => {
  const p = store.ui.preview;
  if (p && e.target.classList && e.target.classList.contains('preview__media') && p.kind) {
    store.ui.preview = { ...p, kind: '', failed: true };
    redraw();
  }
}, true);

export function previewActions({ set, fail }) {
  redraw = () => set({});
  return {
    previewDoc: el => {
      const pid = store.ui.projectId, doc = (store.data.documents[pid] || []).find(x => x.id === Number(el.dataset.id));
      if (doc) openPreview(set, { src: `/documents/${doc.id}`, name: doc.name, size: doc.size, mime: doc.mime || '', docId: doc.id, added: doc.created_at });
    },
    // A picture attached to a prompt: data-url is the thumbnail's local URL, data-src its API path.
    previewImage: el => openPreview(set, { url: el.dataset.url || '', src: el.dataset.src || '', name: el.dataset.name || 'Picture', mime: 'image/png' }),
    previewDownload: async () => {
      const p = store.ui.preview;
      if (!p) return;
      try {
        let blob = raw;
        if (!blob) {
          const res = await fetch(p.url || p.src, p.url ? {} : { headers: { 'X-API-Key': apiKey() } });
          if (!res.ok) throw new Error(res.status === 404 ? 'That file is gone' : res.statusText);
          blob = await res.blob();
        }
        const url = URL.createObjectURL(new Blob([blob], { type: 'application/octet-stream' }));
        const a = Object.assign(document.createElement('a'), { href: url, download: p.name });
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
      } catch (e) { fail(e); }
    },
    previewOpenTab: () => {
      const p = store.ui.preview;
      if (p && p.url && canOpen(p)) window.open(p.url, '_blank', 'noopener');
    },
  };
}

// Only a PDF or a plain picture opens in a tab of its own; an SVG there would run as a page.
const canOpen = p => p.state === 'ready' && p.url && (p.kind === 'pdf' || (p.kind === 'image' && !isSvg(p)));

const fmtBytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1).replace(/\.0$/, '')} MB`;

function stage(p) {
  const name = esc(p.name);
  if (p.state === 'loading') return `<div class="preview__stage preview__stage--wait">${spinner()}<span class="meta">Loading preview…</span></div>`;
  if (p.state === 'error') return `<div class="preview__stage preview__stage--none"><span class="form-err">${esc(p.error)}</span></div>`;
  switch (p.kind) {
    case 'image': return `<div class="preview__stage preview__stage--image"><img class="preview__media preview__img" src="${p.url}" alt="${name}"></div>`;
    case 'pdf': return `<div class="preview__stage preview__stage--pdf"><iframe class="preview__media preview__pdf" src="${p.url}" title="${name}"></iframe></div>`;
    case 'audio': return `<div class="preview__stage preview__stage--audio"><audio class="preview__media" controls preload="metadata" src="${p.url}"></audio></div>`;
    case 'video': return `<div class="preview__stage preview__stage--video"><video class="preview__media preview__video" controls playsinline preload="metadata" src="${p.url}"></video></div>`;
    case 'text': return `<pre class="preview__text">${esc(p.text) || '<span class="meta">This file is empty.</span>'}</pre>${p.cut ? `<span class="meta">Showing the first ${fmtBytes(TEXT_MAX)} - download it to see the rest.</span>` : ''}`;
    default: {
      const ext = extOf(p.name);
      return `<div class="preview__stage preview__stage--none">
        <span class="preview__ext">${ext ? esc(ext.toUpperCase()) : 'FILE'}</span>
        <span class="meta">${p.failed ? 'This browser can’t show this file.' : p.odd ? 'This file isn’t what its name says, so it isn’t shown here.' : 'No preview for this kind of file.'} Download it to open it.</span>
      </div>`;
    }
  }
}

export function previewSheet(c, wrap) {
  const p = c.ui.preview;
  if (!p) return '';
  const meta = [p.size ? fmtBytes(p.size) : '', p.added ? `added ${ago(p.added, c.now)}` : ''].filter(Boolean).join(' · ');
  return wrap('preview', `<div class="sheet__head"><span class="sheet__title truncate" title="${esc(p.name)}">${esc(p.name)}</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <div class="sheet__body preview">
      ${stage(p)}
      <div class="need__row preview__acts">
        ${meta ? `<span class="meta grow">${esc(meta)}</span>` : '<span class="grow"></span>'}
        ${canOpen(p) ? `<button class="btn btn--ghost btn--38" data-action="previewOpenTab">Open in new tab</button>` : ''}
        <button class="btn btn--ghost btn--38" data-action="previewDownload"${p.state === 'loading' ? ' disabled' : ''}>${icon('download', 16)} Download</button>
        ${p.docId ? `<button class="btn-dismiss" data-action="docDelete" data-id="${p.docId}">Delete</button>` : ''}
      </div>
    </div>`, 'Preview');
}
