// Pictures attached to a prompt (Ask box, New task sheet, chat composer, backlog items).
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
  list.forEach(i => { if (i.id && i.url) blobs.set(i.id, i.url); else if (i.url) URL.revokeObjectURL(i.url); });
  list.length = 0;
}

export const readyIds = list => list.filter(i => i.state === 'ready').map(i => i.id);

// A draft holding pictures that were already uploaded (editing a backlog item), so they can be removed or added to.
export const draftOf = ids => (ids || []).map(id => ({ key: ++seq, name: 'Attached picture', url: '', id, state: 'ready' }));

// Why the draft can't be sent yet ('' when it can).
export function imageProblem(list) {
  if (list.some(i => i.state === 'uploading')) return 'Still uploading your picture…';
  if (list.some(i => i.state === 'error')) return 'A picture failed to upload - remove it or attach it again';
  return '';
}

// The row of thumbnails above a box. `target` is which box (ask | palette | reply | todo | todoEdit).
export function imageStrip(list, target) {
  if (!list || !list.length) return '';
  return `<div class="attach-strip" data-noswipe>${list.map(i => `<div class="attach-thumb is-${i.state}" title="${esc(i.error || i.name)}">
    ${i.url || localUrl(i.id) ? `<button class="attach-thumb__open" data-action="previewImage" data-url="${i.url || localUrl(i.id)}" data-name="${esc(i.name)}" aria-label="Preview ${esc(i.name)}"><img src="${i.url || localUrl(i.id)}" alt="${esc(i.name)}"></button>` : ''}
    ${i.state === 'uploading' ? '<span class="spinner spinner--tool"></span>' : i.state === 'error' ? '<span class="attach-thumb__err">!</span>' : ''}
    <button class="attach-thumb__x" data-action="removeImage" data-target="${target}" data-img="${i.key}" aria-label="Remove ${esc(i.name)}">×</button>
  </div>`).join('')}</div>`;
}

// ---------------------------------------------------------------- pictures already sent
// <img> can't send the API key, so a sent picture is fetched once with it and cached as a local URL.
const blobs = new Map();   // path -> local URL | 'loading' | 'failed'

function authedUrl(path) {
  const v = blobs.get(path);
  if (v) return v === 'loading' || v === 'failed' ? '' : v;
  blobs.set(path, 'loading');
  fetch(path, { headers: { 'X-API-Key': apiKey() } })
    .then(r => (r.ok ? r.blob() : Promise.reject(new Error(r.statusText))))
    .then(b => blobs.set(path, URL.createObjectURL(b)), () => blobs.set(path, 'failed'))
    .finally(redraw);
  return '';
}

const localUrl = id => authedUrl('/uploads/' + encodeURIComponent(id));

// A reply's text as HTML: Markdown (headings, lists, bold/italic, code, links) rendered from escaped text, and a
// picture Octopus made (`![alt](/octopus/images/x.png)`) shown as the picture.
const MADE_IMAGE = /!\[([^\]]*)\]\((\/octopus\/images\/[A-Za-z0-9._-]+)\)/g;

function inlineMd(raw) {
  const codes = [];
  let t = esc(raw).replace(/`([^`\n]+)`/g, (_, c) => `\u0001${codes.push(c) - 1}\u0001`);
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w*])\*([^*\n]+)\*(?![\w*])/g, '$1<em>$2</em>');
  return t.replace(/\u0001(\d+)\u0001/g, (_, i) => `<code>${codes[i]}</code>`);
}

function markdown(text) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let para = [], code = null;
  let stack = [];   // open lists, outermost first: { tag, indent, start, items: [{ html, kids: [] }] }
  const renderList = l => `<${l.tag}${l.tag === 'ol' && l.start > 1 ? ` start="${l.start}"` : ''}>${l.items.map(i => `<li>${i.html}${i.kids.map(renderList).join('')}</li>`).join('')}</${l.tag}>`;
  const flushPara = () => {
    if (para.length) {
      // A paragraph that is only bold text is a section title ("**Decisions I need from you**").
      const only = para.length === 1 && /^\s*\*\*[^*\n]+\*\*:?\s*$/.test(para[0]);
      out.push(`<p${only ? ' class="md-h"' : ''}>${para.map(inlineMd).join('<br>')}</p>`);
    }
    para = [];
  };
  const flushList = () => { if (stack.length) out.push(renderList(stack[0])); stack = []; };
  const lastItem = () => { const l = stack[stack.length - 1]; return l && l.items[l.items.length - 1]; };
  for (const line of lines) {
    if (code) {
      if (/^\s*```/.test(line)) { out.push(`<pre class="md-code">${esc(code.join('\n'))}</pre>`); code = null; } else code.push(line);
      continue;
    }
    let m;
    if (/^\s*```/.test(line)) { flushPara(); flushList(); code = []; }
    else if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) { flushPara(); flushList(); out.push(`<p class="md-h">${inlineMd(m[2])}</p>`); }
    else if ((m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line))) {
      flushPara();
      const indent = m[1].replace(/\t/g, '    ').length, tag = /\d/.test(m[2]) ? 'ol' : 'ul';
      while (stack.length && stack[stack.length - 1].indent > indent) stack.pop();
      let cur = stack[stack.length - 1];
      if (cur && cur.indent === indent && cur.tag !== tag) { stack.pop(); cur = stack[stack.length - 1]; }
      if (!cur || cur.indent < indent) {
        const l = { tag, indent, start: parseInt(m[2], 10) || 1, items: [], kids: [] };
        const parent = cur && cur.items[cur.items.length - 1];
        if (parent) parent.kids.push(l);
        stack.push(l);
        cur = l;
      }
      cur.items.push({ html: inlineMd(m[3]), kids: [] });
    }
    else if (!line.trim()) flushPara();   // a blank line keeps an open list going: the next line decides
    else if (stack.length && /^\s+/.test(line) && lastItem()) lastItem().html += '<br>' + inlineMd(line.trim());
    else { flushList(); para.push(line); }
  }
  if (code) out.push(`<pre class="md-code">${esc(code.join('\n'))}</pre>`);
  flushPara(); flushList();
  return out.join('');
}

export function replyText(text) {
  const pics = [];
  const body = text.replace(MADE_IMAGE, (_, alt, path) => {
    const u = authedUrl(path);
    return `\u0002${pics.push(u
      ? `<button class="msg-made-img__open" data-action="previewImage" data-url="${u}" data-name="${esc(alt || 'Picture')}" aria-label="Preview picture"><img class="msg-made-img" src="${u}" alt="${esc(alt)}"></button>`
      : `<span class="msg-made-img msg-made-img--wait" title="${esc(alt)}"></span>`) - 1}\u0002`;
  });
  return markdown(body).replace(/\u0002(\d+)\u0002/g, (_, i) => pics[i]);
}

// Thumbnails inside a sent message's bubble; tapping one opens it in the preview sheet (preview.js).
export function sentStrip(ids) {
  if (!ids || !ids.length) return '';
  return `<div class="msg-you__imgs">${ids.map(id => {
    const u = localUrl(id);
    return u ? `<button class="msg-you__img" data-action="previewImage" data-url="${u}" data-name="Attached picture" aria-label="Preview attached picture"><img class="attach-thumb attach-thumb--sent" src="${u}" alt="Attached picture"></button>`
      : `<span class="attach-thumb attach-thumb--sent attach-thumb--wait"></span>`;
  }).join('')}</div>`;
}
