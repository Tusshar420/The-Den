/* The Notes editor (DenNotes): rich text like Word / Apple Notes, built on Quill 2 (vendored, MIT).

   A note's document is Quill delta ops (pad_notes.content); its Markdown copy (body) is what search,
   previews, Octopus and "Save as file" read, and what a note is rebuilt from when it has no document
   (older notes, or text changed somewhere else). Besides Quill's own formats it has:
     important   a highlighted "Important" section (a block format; consecutive lines read as one box)
     comment     a commented stretch of text (inline; the value is the comment's id in the note's meta)
     denimg      a picture in place ({id} - an upload, fetched with the API key)
     denfile     a document in place ({id, name, size, mime})
     denlink     a link pinned at a spot without text of its own ({url, title})
   Lists behave like Word / Notes: Enter on an empty bullet or box ends the list, Backspace at the start
   of one turns it back into normal text, and "- ", "1. ", "[] " start a list as you type. */
(function () {
  'use strict';
  const VENDOR = '/static/den/vendor/';
  const FORMATS = ['bold', 'italic', 'underline', 'strike', 'code', 'color', 'background', 'link', 'comment',
    'header', 'list', 'indent', 'align', 'blockquote', 'code-block', 'important', 'denimg', 'denfile', 'denlink'];
  const SAFE_URL = /^(https?:\/\/|mailto:|tel:)\S+$/i;
  const UPLOAD_ID = '[0-9a-f]{32}\\.[a-z0-9]+';
  let loading = null;

  // Pictures and files need the API key, so they're fetched once and shown from a blob URL.
  const blobs = {};
  function blobUrl(id) {
    if (!blobs[id]) {
      const D = window.Den;
      blobs[id] = fetch(D.base() + '/uploads/' + encodeURIComponent(id), { headers: { 'X-API-Key': D.key() } })
        .then(r => { if (!r.ok) throw new Error(r.status === 404 ? 'That file is gone' : 'Couldn’t open the file'); return r.blob(); })
        .then(b => URL.createObjectURL(b))
        .catch(e => { delete blobs[id]; throw e; });
    }
    return blobs[id];
  }
  const safeUrl = u => (SAFE_URL.test(String(u || '').trim()) ? String(u).trim() : '');
  // What someone types in the link box: "example.com" means https://example.com.
  function normUrl(u) {
    u = String(u || '').trim(); if (!u) return '';
    if (/^[\w.+-]+@[\w-]+\.[\w.-]+$/.test(u)) u = 'mailto:' + u;
    else if (!/^[a-z][a-z0-9+.-]*:/i.test(u)) u = 'https://' + u.replace(/^\/+/, '');
    return safeUrl(u);
  }
  const sizeLabel = n => (n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n / 1024) + ' KB' : (n || 0) + ' B');
  const hostOf = u => { try { const x = new URL(u); return x.protocol.startsWith('http') ? x.host.replace(/^www\./, '') : u.replace(/^(mailto|tel):/i, ''); } catch (e) { return u; } };

  function load() {
    if (window.Quill && window.Quill.__den) return Promise.resolve(window.Quill);
    if (loading) return loading;
    loading = new Promise((resolve, reject) => {
      const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = VENDOR + 'quill-2.0.3.core.css';
      document.head.appendChild(css);
      const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
      const s = document.createElement('script'); s.src = VENDOR + 'quill-2.0.3.js';
      s.onload = () => { try { register(window.Quill); resolve(window.Quill); } catch (e) { loading = null; reject(e); } };
      s.onerror = () => { loading = null; s.remove(); reject(new Error('The editor didn’t load - check the connection and reopen the note')); };
      document.head.appendChild(s);
    });
    return loading;
  }

  function register(Quill) {
    const Block = Quill.import('blots/block');
    const Inline = Quill.import('blots/inline');
    const Embed = Quill.import('blots/embed');
    const { EmbedBlot } = Quill.import('parchment');

    class Important extends Block {}
    Important.blotName = 'important'; Important.tagName = 'DIV'; Important.className = 'den-imp';

    class Comment extends Inline {
      static create(v) { const n = super.create(); n.setAttribute('data-c', String(v)); return n; }
      static formats(n) { return n.getAttribute('data-c') || undefined; }
      format(name, v) { if (name === 'comment' && v) this.domNode.setAttribute('data-c', String(v)); else super.format(name, v); }
    }
    Comment.blotName = 'comment'; Comment.tagName = 'SPAN'; Comment.className = 'den-cmt';
    Inline.order.splice(Inline.order.indexOf('inline') + 1, 0, 'comment');

    class DenImg extends EmbedBlot {
      static create(v) {
        const n = super.create(); const id = (v && v.id) || '';
        n.setAttribute('data-id', id); n.setAttribute('alt', ''); n.setAttribute('draggable', 'false');
        if (id) blobUrl(id).then(u => n.setAttribute('src', u)).catch(() => n.classList.add('den-gone'));
        return n;
      }
      static value(n) { return { id: n.getAttribute('data-id') || '' }; }
    }
    DenImg.blotName = 'denimg'; DenImg.tagName = 'IMG'; DenImg.className = 'den-img';

    const chip = (n, icon, label, sub) => {
      const i = document.createElement('span'); i.className = 'den-chip-i'; i.textContent = icon;
      const l = document.createElement('span'); l.className = 'den-chip-l'; l.textContent = label;
      n.append(i, l);
      if (sub) { const s = document.createElement('span'); s.className = 'den-chip-s'; s.textContent = sub; n.append(s); }
    };
    class DenFile extends Embed {
      static create(v) {
        const n = super.create(); v = v || {};
        n.setAttribute('data-id', v.id || ''); n.setAttribute('data-name', v.name || 'File');
        n.setAttribute('data-size', String(v.size || 0)); n.setAttribute('data-mime', v.mime || '');
        chip(n, '📎', v.name || 'File', v.size ? sizeLabel(v.size) : '');
        return n;
      }
      static value(n) { return { id: n.getAttribute('data-id') || '', name: n.getAttribute('data-name') || 'File', size: +n.getAttribute('data-size') || 0, mime: n.getAttribute('data-mime') || '' }; }
    }
    DenFile.blotName = 'denfile'; DenFile.tagName = 'SPAN'; DenFile.className = 'den-file';

    class DenLink extends Embed {
      static create(v) {
        const n = super.create(); v = v || {}; const url = safeUrl(v.url);
        n.setAttribute('data-url', url); n.setAttribute('data-title', v.title || '');
        chip(n, '🔗', v.title || hostOf(url) || 'Link');
        return n;
      }
      static value(n) { return { url: n.getAttribute('data-url') || '', title: n.getAttribute('data-title') || '' }; }
    }
    DenLink.blotName = 'denlink'; DenLink.tagName = 'SPAN'; DenLink.className = 'den-chip';

    // Links only ever open web, mail or phone addresses.
    const Link = Quill.import('formats/link');
    Link.PROTOCOL_WHITELIST = ['http', 'https', 'mailto', 'tel'];

    Quill.register({ 'formats/important': Important, 'formats/comment': Comment, 'formats/denimg': DenImg,
      'formats/denfile': DenFile, 'formats/denlink': DenLink }, true);
    Quill.__den = true;
  }

  // ------------------------------------------------------------------ Markdown, both ways
  const clean = s => String(s || '').replace(/[\[\]\n]/g, ' ').trim();
  function inlineMd(ops) {
    return ops.map(o => {
      const e = o.insert;
      if (typeof e !== 'string') {
        if (e.denimg) return '![image](upload:' + e.denimg.id + ')';
        if (e.denfile) return '[📎 ' + clean(e.denfile.name) + '](upload:' + e.denfile.id + ')';
        if (e.denlink) return '[🔗 ' + clean(e.denlink.title || hostOf(e.denlink.url)) + '](' + e.denlink.url + ')';
        return '';
      }
      const a = o.attributes || {};
      if (!e.trim()) return e;
      const lead = e.match(/^\s*/)[0], trail = e.match(/\s*$/)[0];
      let core = e.slice(lead.length, e.length - trail.length);
      if (a.code) core = '`' + core + '`';
      else { if (a.bold) core = '**' + core + '**'; if (a.italic) core = '*' + core + '*'; if (a.strike) core = '~~' + core + '~~'; }
      if (a.link) core = '[' + core.replace(/[\[\]]/g, '') + '](' + a.link + ')';
      return lead + core + trail;
    }).join('');
  }
  function toMarkdown(ops) {
    const Delta = window.Quill.import('delta');
    const out = []; let code = false; const num = [];
    new Delta(ops || []).eachLine((line, a) => {
      const isCode = !!a['code-block'];
      if (isCode !== code) { out.push('```'); code = isCode; }
      if (isCode) { out.push(line.ops.map(o => (typeof o.insert === 'string' ? o.insert : '')).join('')); return; }
      let t = inlineMd(line.ops); const lvl = Math.min(8, a.indent || 0); const ind = '  '.repeat(lvl);
      if (a.list === 'ordered') { num.length = lvl + 1; num[lvl] = (num[lvl] || 0) + 1; } else if (a.list) num.length = lvl + 1; else num.length = 0;
      if (a.header) t = '#'.repeat(a.header) + ' ' + t;
      else if (a.list === 'bullet') t = ind + '- ' + t;
      else if (a.list === 'ordered') t = ind + num[lvl] + '. ' + t;
      else if (a.list === 'checked') t = ind + '- [x] ' + t;
      else if (a.list === 'unchecked') t = ind + '- [ ] ' + t;
      else if (a.blockquote) t = '> ' + t;
      else if (a.important) t = '!! ' + t;
      out.push(t);
    });
    if (code) out.push('```');
    return out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  const INLINE_RX = new RegExp([
    '!\\[[^\\]]*\\]\\(upload:(' + UPLOAD_ID + ')\\)',                                   // 1 picture
    '\\[📎 ?([^\\]]*)\\]\\(upload:(' + UPLOAD_ID + ')\\)',                               // 2,3 file
    '\\[🔗 ?([^\\]]*)\\]\\(((?:https?://|mailto:|tel:)[^\\s)]+)\\)',                     // 4,5 link chip
    '\\[([^\\]]+)\\]\\(((?:https?://|mailto:|tel:)[^\\s)]+)\\)',                         // 6,7 link
    '\\*\\*([^*\\n]+)\\*\\*',                                                            // 8 bold
    '~~([^~\\n]+)~~',                                                                    // 9 strike
    '`([^`\\n]+)`',                                                                      // 10 code
    '(?<![\\w*])\\*([^*\\s](?:[^*\\n]*[^*\\s])?)\\*(?![\\w*])',                          // 11 italic
    '(https?://[^\\s<>()\\[\\]]*[^\\s<>()\\[\\].,;:!?\'"])',                            // 12 bare address
  ].join('|'), 'g');
  function inlineOps(text) {
    const ops = []; let at = 0; let m;
    const txt = (s, attributes) => { if (s) ops.push(attributes ? { insert: s, attributes } : { insert: s }); };
    INLINE_RX.lastIndex = 0;
    while ((m = INLINE_RX.exec(text))) {
      txt(text.slice(at, m.index)); at = m.index + m[0].length;
      if (m[1]) ops.push({ insert: { denimg: { id: m[1] } } });
      else if (m[3]) ops.push({ insert: { denfile: { id: m[3], name: m[2] || 'File', size: 0, mime: '' } } });
      else if (m[5]) ops.push({ insert: { denlink: { url: m[5], title: m[4] || '' } } });
      else if (m[7]) txt(m[6], { link: m[7] });
      else if (m[8]) txt(m[8], { bold: true });
      else if (m[9]) txt(m[9], { strike: true });
      else if (m[10]) txt(m[10], { code: true });
      else if (m[11]) txt(m[11], { italic: true });
      else if (m[12]) txt(m[12], { link: m[12] });
    }
    txt(text.slice(at));
    return ops;
  }
  // Markdown (or any plain text) to a document. Pictures the note had before it was rich go at the end.
  function fromMarkdown(md, images) {
    const ops = []; let code = false;
    const lines = String(md || '').replace(/\r\n?/g, '\n').split('\n');
    const lvl = s => Math.min(8, Math.floor(s.replace(/\t/g, '  ').length / 2));
    for (const raw of lines) {
      if (/^\s*```/.test(raw)) { code = !code; continue; }
      if (code) { ops.push({ insert: raw }, { insert: '\n', attributes: { 'code-block': true } }); continue; }
      let m, a = null, t = raw;
      if ((m = raw.match(/^(\s*)[-*•]\s+\[( |x|X)\]\s?(.*)$/))) { a = { list: m[2] === ' ' ? 'unchecked' : 'checked' }; if (lvl(m[1])) a.indent = lvl(m[1]); t = m[3]; }
      else if ((m = raw.match(/^(\s*)[-*•]\s+(.*)$/))) { a = { list: 'bullet' }; if (lvl(m[1])) a.indent = lvl(m[1]); t = m[2]; }
      else if ((m = raw.match(/^(\s*)\d+[.)]\s+(.*)$/))) { a = { list: 'ordered' }; if (lvl(m[1])) a.indent = lvl(m[1]); t = m[2]; }
      else if ((m = raw.match(/^(#{1,3})\s+(.*)$/))) { a = { header: m[1].length }; t = m[2]; }
      else if ((m = raw.match(/^>\s?(.*)$/))) { a = { blockquote: true }; t = m[1]; }
      else if ((m = raw.match(/^!!\s?(.*)$/))) { a = { important: true }; t = m[1]; }
      ops.push(...inlineOps(t), a ? { insert: '\n', attributes: a } : { insert: '\n' });
    }
    for (const id of images || []) if (!String(md || '').includes(id)) ops.push({ insert: { denimg: { id } } }, { insert: '\n' });
    return ops.length ? ops : [{ insert: '\n' }];
  }
  // A one-line preview of a note's Markdown for lists and reminders.
  function plain(md) {
    return String(md || '')
      .replace(/!\[[^\]]*\]\(upload:[^)]*\)/g, '🖼').replace(/\[(📎|🔗) ?([^\]]*)\]\([^)]*\)/g, '$1 $2').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/^(\s*)[-*]\s+\[ \]\s?/gm, '$1☐ ').replace(/^(\s*)[-*]\s+\[[xX]\]\s?/gm, '$1☑ ').replace(/^(\s*)[-*]\s+/gm, '$1• ')
      .replace(/^#{1,3}\s+/gm, '').replace(/^>\s?/gm, '').replace(/^!!\s?/gm, '❗ ').replace(/^```.*$/gm, '')
      .replace(/\*\*([^*]+)\*\*/g, '$1').replace(/~~([^~]+)~~/g, '$1').replace(/`([^`]+)`/g, '$1');
  }

  // Where things are in a document: pictures, files, links, Important sections, comments (index = position).
  function scan(ops) {
    const r = { images: [], files: [], links: [], important: [], comments: {} };
    let i = 0, lineStart = 0, lineText = '';
    const mark = (list, item, key) => {
      const last = list[list.length - 1];
      if (last && last[key] === item[key] && last.index + last.length === item.index) { last.length += item.length; last.text += item.text; } else list.push(item);
    };
    for (const o of ops || []) {
      const e = o.insert; const a = o.attributes || {};
      if (typeof e !== 'string') {
        if (e.denimg) r.images.push({ id: e.denimg.id, index: i });
        else if (e.denfile) r.files.push({ ...e.denfile, index: i });
        else if (e.denlink) r.links.push({ url: e.denlink.url, title: e.denlink.title || '', text: '', chip: true, index: i, length: 1 });
        i += 1; lineText += ' '; continue;
      }
      e.split('\n').forEach((p, k) => {
        if (k > 0) {
          if (a.important) {
            const last = r.important[r.important.length - 1];
            if (last && last.end === lineStart - 1) { last.length = i - last.index; last.text += '\n' + lineText; last.end = i; }
            else r.important.push({ index: lineStart, length: i - lineStart, text: lineText, end: i });
          }
          i += 1; lineStart = i; lineText = '';
        }
        if (!p) return;
        if (a.link) mark(r.links, { url: a.link, title: '', text: p, chip: false, index: i, length: p.length }, 'url');
        if (a.comment) { const c = r.comments[a.comment]; if (c) { c.length = i + p.length - c.index; c.text += p; } else r.comments[a.comment] = { index: i, length: p.length, text: p }; }
        lineText += p; i += p.length;
      });
    }
    return r;
  }

  // ------------------------------------------------------------------ the editor
  // opts: placeholder, onChange(), onState({ formats, range }), onUpload(files) -> Promise<[{id, mime, size, name}]>,
  //       onClickLink(url), onClickComment(id), onClickFile(file), onClickImage(id), onSay(title, sub)
  function create(host, opts) {
    const Quill = window.Quill; const Delta = Quill.import('delta');
    host.innerHTML = ''; host.classList.add('den-ned');
    const el = document.createElement('div'); host.appendChild(el);
    const lineOff = (q, index, fmt) => q.formatLine(index, 0, fmt, false, 'user');
    const q = new Quill(el, {
      formats: FORMATS, placeholder: opts.placeholder || '',
      modules: {
        history: { delay: 700, maxStack: 400, userOnly: true },
        keyboard: { bindings: {
          // Important sections end like lists: Enter on an empty line, or Backspace at the start of one.
          'den important empty enter': { key: 'Enter', collapsed: true, format: ['important'], empty: true, handler: r => lineOff(q, r.index, 'important') },
          'den important backspace': { key: 'Backspace', collapsed: true, format: ['important'], offset: 0, handler: r => lineOff(q, r.index, 'important') },
          'den header backspace': { key: 'Backspace', collapsed: true, format: ['header'], offset: 0, empty: true, handler: r => lineOff(q, r.index, 'header') },
          'den link': { key: 'k', shortKey: true, handler: () => { opts.onCommand && opts.onCommand('link'); return false; } },
          'den checklist': { key: 76, shortKey: true, shiftKey: true, handler: () => { api.list('unchecked'); return false; } },  /* ⌘⇧L, as in Apple Notes */
          'den bullets': { key: 56, shortKey: true, shiftKey: true, handler: () => { api.list('bullet'); return false; } },      /* ⌘⇧8 */
          'den numbers': { key: 55, shortKey: true, shiftKey: true, handler: () => { api.list('ordered'); return false; } },     /* ⌘⇧7 */
          'den strike': { key: 88, shortKey: true, shiftKey: true, handler: () => { api.toggle('strike'); return false; } },     /* ⌘⇧X */
          'den important': { key: 69, shortKey: true, shiftKey: true, handler: () => { api.important(); return false; } },       /* ⌘⇧E */
          'den comment': { key: 77, shortKey: true, altKey: true, handler: () => { opts.onCommand && opts.onCommand('comment'); return false; } }, /* ⌘⌥M */
        } },
      },
    });
    // A shortcut the editor used (⌘K = link, as in Word and Notes) stops here, not at the page's own (⌘K = palette).
    q.root.addEventListener('keydown', e => { if (e.defaultPrevented && (e.metaKey || e.ctrlKey || e.altKey)) e.stopPropagation(); });
    // Pictures pasted or dropped in a note become uploads, never pictures pasted from a web page as-is.
    q.clipboard.addMatcher('IMG', (node, delta) => (node.classList && node.classList.contains('den-img') ? delta : new Delta()));
    const uploader = q.getModule('uploader');
    uploader.options.mimetypes = { includes: () => true };
    uploader.options.handler = (range, files) => api.insertFiles(range, files);

    // A pasted web address on selected text links that text.
    q.root.addEventListener('paste', e => {
      const t = (e.clipboardData && e.clipboardData.getData('text/plain') || '').trim(); const r = q.getSelection();
      if (!r || !r.length || !SAFE_URL.test(t) || /\s/.test(t) || (e.clipboardData.files || []).length) return;
      e.preventDefault(); e.stopPropagation(); q.formatText(r.index, r.length, 'link', t, 'user');
    }, true);

    // A web address typed into the text becomes a link when you finish it with a space or Enter.
    q.on('text-change', (delta, old, source) => {
      if (source === 'user') {
        const ins = delta.ops.find(o => typeof o.insert === 'string');
        const r = q.getSelection();
        if (ins && /^[ \n]$/.test(ins.insert) && r && r.index > 1) {
          const before = q.getText(Math.max(0, r.index - 201), Math.min(r.index - 1, 200));
          const m = before.match(/(?:^|\s)((?:https?:\/\/|www\.)[^\s]+[^\s.,;:!?'")\]])$/);
          if (m) { const at = r.index - 1 - m[1].length; if (!q.getFormat(at, m[1].length).link) q.formatText(at, m[1].length, 'link', m[1].startsWith('www.') ? 'https://' + m[1] : m[1], 'api'); }
        }
      }
      if (source !== 'silent') opts.onChange && opts.onChange();
    });
    let last = null;
    const state = () => { const r = q.getSelection(); if (r) last = r; opts.onState && opts.onState({ formats: r ? q.getFormat(r) : (last ? q.getFormat(last) : {}), range: r || last, focused: !!r }); };
    q.on('editor-change', state);

    q.root.addEventListener('click', e => {
      const t = e.target;
      const img = t.closest && t.closest('img.den-img');
      if (img) return opts.onClickImage && opts.onClickImage(img.getAttribute('data-id'));
      const file = t.closest && t.closest('.den-file');
      if (file) return opts.onClickFile && opts.onClickFile({ id: file.getAttribute('data-id'), name: file.getAttribute('data-name'), mime: file.getAttribute('data-mime') });
      const link = t.closest && (t.closest('a[href]') || t.closest('.den-chip'));
      if (link) {
        const url = link.getAttribute('href') || link.getAttribute('data-url');
        if (e.metaKey || e.ctrlKey) { e.preventDefault(); if (safeUrl(url)) window.open(url, '_blank', 'noopener'); return; }
        return opts.onClickLink && opts.onClickLink(url);
      }
      const c = t.closest && t.closest('.den-cmt');
      if (c) opts.onClickComment && opts.onClickComment(c.getAttribute('data-c'));
    });

    const range = () => q.getSelection() || last || { index: Math.max(0, q.getLength() - 1), length: 0 };
    const api = {
      quill: q,
      range, focus: () => { q.focus(); if (last) q.setSelection(last, 'silent'); },
      set(ops) { q.setContents(ops && ops.length ? ops : [{ insert: '\n' }], 'silent'); q.history.clear(); last = null; },
      ops: () => q.getContents().ops,
      markdown: () => toMarkdown(q.getContents().ops),
      scan: () => scan(q.getContents().ops),
      undo: () => q.history.undo(), redo: () => q.history.redo(),
      toggle(name) { const r = range(); q.setSelection(r, 'silent'); const f = q.getFormat(r); q.format(name, !f[name], 'user'); },
      format(name, value) { q.setSelection(range(), 'silent'); q.format(name, value, 'user'); },
      list(kind) { const r = range(); q.setSelection(r, 'silent'); const cur = q.getFormat(r).list; const on = kind === 'unchecked' ? cur === 'checked' || cur === 'unchecked' : cur === kind; q.format('list', on ? false : kind, 'user'); },
      indent(dir) { const r = range(); q.setSelection(r, 'silent'); q.format('indent', dir > 0 ? '+1' : '-1', 'user'); },
      clear() { const r = range(); if (r.length) q.removeFormat(r.index, r.length, 'user'); else q.formatLine(r.index, 0, { header: false, list: false, blockquote: false, 'code-block': false, important: false, align: false, indent: false }, 'user'); },
      // Important: marks the selected lines, or the cursor's empty line; on a line with text it adds a new
      // Important section just below and puts the cursor in it. Pressed again inside one, it turns it off.
      important() {
        const r = range(); q.setSelection(r, 'silent');
        const [line, off] = q.getLine(r.index); const on = !!q.getFormat(r).important;
        if (r.length || on || line.length() <= 1) { q.formatLine(r.index, r.length, 'important', !on, 'user'); return; }
        const end = r.index - off + line.length() - 1;
        q.updateContents(new Delta().retain(end).insert('\n', line.formats()).retain(1, { important: true, list: null, header: null, blockquote: null }), 'user');
        q.setSelection(end + 1, 0, 'user');
      },
      link(r, url) { q.formatText(r.index, r.length, 'link', url, 'user'); q.setSelection(r.index + r.length, 0, 'user'); q.format('link', false, 'silent'); },
      chip(r, url, title) { q.insertEmbed(r.index, 'denlink', { url, title }, 'user'); q.insertText(r.index + 1, ' ', 'user'); q.setSelection(r.index + 2, 0, 'user'); },
      unlink(url) { const s = scan(q.getContents().ops); s.links.filter(l => l.url === url).reverse().forEach(l => (l.chip ? q.deleteText(l.index, 1, 'user') : q.formatText(l.index, l.length, 'link', false, 'user'))); },
      comment(r, id) { q.formatText(r.index, r.length, 'comment', id, 'user'); q.setSelection(r.index + r.length, 0, 'silent'); q.format('comment', false, 'silent'); },
      uncomment(id) { const c = scan(q.getContents().ops).comments[id]; if (c) q.formatText(c.index, c.length, 'comment', false, 'user'); },
      unimportant(index, length) { q.formatLine(index, length, 'important', false, 'user'); },
      async insertFiles(r, files) {
        if (!files || !files.length) return;
        r = r || range();
        let ups; try { ups = await opts.onUpload([...files]); } catch (e) { return; }
        let at = Math.min(r.index, q.getLength() - 1); if (r.length) q.deleteText(r.index, r.length, 'user');
        for (const u of ups || []) {
          if (/^image\//.test(u.mime)) q.insertEmbed(at, 'denimg', { id: u.id }, 'user');
          else q.insertEmbed(at, 'denfile', { id: u.id, name: u.name, size: u.size, mime: u.mime }, 'user');
          at += 1;
        }
        q.setSelection(at, 0, 'user');
      },
      // Jump to a spot (from the side panel) and flash it.
      reveal(index, length) {
        q.setSelection(index, length || 0, 'user');
        const [leaf] = q.getLeaf(index + (length ? 1 : 0)); let node = leaf && leaf.domNode; if (node && node.nodeType === 3) node = node.parentElement;
        const [line] = q.getLine(index); const box = (line && line.domNode) || node;
        if (box && box.scrollIntoView) { box.scrollIntoView({ block: 'center', behavior: 'smooth' }); box.classList.remove('den-flash'); void box.offsetWidth; box.classList.add('den-flash'); setTimeout(() => box.classList.remove('den-flash'), 1500); }
      },
      markComment(id) { q.root.querySelectorAll('.den-cmt.on').forEach(n => n.classList.remove('on')); if (id) q.root.querySelectorAll('.den-cmt[data-c="' + CSS.escape(id) + '"]').forEach(n => n.classList.add('on')); },
      replace(index, length, ops) { q.updateContents(new Delta().retain(index).delete(length).concat(new Delta(ops)), 'user'); },
      slice(index, length) { return q.getContents(index, length).ops; },
    };
    return api;
  }

  // Open a picture or document from the note: pictures and PDFs in a new tab, others download.
  function openUpload(id, name, mime) {
    const view = /^(image\/|application\/pdf|text\/plain)/.test(mime || '') || /\.(png|jpe?g|gif|webp|pdf|txt|md)$/i.test(id);
    const w = view ? window.open('', '_blank') : null;
    return blobUrl(id).then(u => {
      if (w) { w.location.href = u; return; }
      const a = document.createElement('a'); a.href = u; a.download = name || id; document.body.appendChild(a); a.click(); a.remove();
    }).catch(e => { if (w) w.close(); throw e; });
  }

  const CHECK = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2306111A' stroke-width='3.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7.5'/%3E%3C/svg%3E";
  const CSS = `
.den-ned .ql-container{font-family:inherit;font-size:15px;height:auto}
.den-ned .ql-editor{padding:2px 0 48px;min-height:260px;height:auto;overflow:visible;color:#DCE4EB;line-height:1.7;caret-color:#FF7B5C;-webkit-user-select:text;user-select:text}
.den-ned .ql-editor.ql-blank::before{color:#5E6B79;font-style:normal;left:0;right:0}
.den-ned .ql-editor h1{font-size:1.7em;font-weight:750;letter-spacing:-.012em;line-height:1.3;margin:.55em 0 .2em;color:#F2F5F8}
.den-ned .ql-editor h2{font-size:1.32em;font-weight:720;line-height:1.35;margin:.5em 0 .15em;color:#EDF1F5}
.den-ned .ql-editor h3{font-size:1.1em;font-weight:700;margin:.45em 0 .1em;color:#E8EEF3}
.den-ned .ql-editor > :first-child{margin-top:0}
.den-ned .ql-editor strong{font-weight:750;color:#F2F5F8}
.den-ned .ql-editor a{color:#8DBBFF;text-decoration:underline;text-decoration-color:rgba(127,178,255,.45);text-underline-offset:2px;cursor:pointer}
.den-ned .ql-editor code{font-family:'JetBrains Mono',monospace;font-size:.86em;background:rgba(255,255,255,.08);border-radius:5px;padding:1px 5px}
.den-ned .ql-editor blockquote{border-left:3px solid rgba(255,255,255,.2);padding:2px 0 2px 13px;color:#A9B6C3;margin:4px 0}
.den-ned .ql-editor .ql-code-block-container{font-family:'JetBrains Mono',monospace;font-size:13px;line-height:1.6;background:rgba(0,0,0,.32);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 13px;margin:6px 0;color:#CFE3D9}
.den-ned .ql-editor ol{padding-left:.15em}
.den-ned .ql-editor li{padding-left:1.7em}
.den-ned .ql-editor li > .ql-ui:before{margin-left:-1.7em;width:1.35em}
.den-ned .ql-editor li[data-list=bullet] > .ql-ui:before{content:'\\2022';color:#FF7B5C;font-weight:800;font-size:1.15em;line-height:1}
.den-ned .ql-editor li[data-list=ordered] > .ql-ui:before{color:#93A1B0;font-variant-numeric:tabular-nums}
.den-ned .ql-editor li[data-list=checked] > .ql-ui:before,.den-ned .ql-editor li[data-list=unchecked] > .ql-ui:before{content:'';display:inline-block;box-sizing:border-box;width:17px;height:17px;border-radius:6px;border:1.7px solid #6B7A8A;vertical-align:-3px;margin-left:-1.7em;margin-right:calc(1.7em - 17px - 1px);transition:background .12s,border-color .12s}
.den-ned .ql-editor li[data-list=unchecked] > .ql-ui:hover:before{border-color:#45E0C8}
.den-ned .ql-editor li[data-list=checked] > .ql-ui:before{background:#45E0C8 url("${CHECK}") center/12px no-repeat;border-color:#45E0C8}
.den-ned .ql-editor li[data-list=checked]{color:#6B7A8A;text-decoration:line-through;text-decoration-color:rgba(147,161,176,.6)}
.den-ned .ql-editor .den-imp{background:rgba(255,181,71,.09);border-left:3px solid #FFB547;padding:0 14px;color:#F3E6CF}
.den-ned .ql-editor :not(.den-imp) + .den-imp,.den-ned .ql-editor > .den-imp:first-child{border-top-right-radius:10px;padding-top:9px;margin-top:6px}
.den-ned .ql-editor .den-imp:not(:has(+ .den-imp)){border-bottom-right-radius:10px;padding-bottom:9px;margin-bottom:6px}
.den-ned .ql-editor :not(.den-imp) + .den-imp::before,.den-ned .ql-editor > .den-imp:first-child::before{content:'Important';display:block;font-size:10.5px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:#FFB547;line-height:1.6;pointer-events:none;-webkit-user-select:none;user-select:none}
.den-ned .ql-editor .den-cmt{background:rgba(255,213,79,.13);border-bottom:2px solid rgba(255,213,79,.55);cursor:pointer;border-radius:2px}
.den-ned .ql-editor .den-cmt.on{background:rgba(255,213,79,.3)}
.den-ned .ql-editor img.den-img{display:inline-block;max-width:100%;max-height:440px;min-width:48px;min-height:36px;border-radius:12px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.03);vertical-align:bottom;margin:6px 2px;cursor:zoom-in}
.den-ned .ql-editor img.den-img.den-gone{min-width:120px;min-height:60px;opacity:.4}
.den-ned .ql-editor .den-file > span,.den-ned .ql-editor .den-chip > span{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:2px 10px;margin:1px 0;border-radius:9px;font-size:13px;line-height:1.6;cursor:pointer;vertical-align:baseline;text-decoration:none;white-space:nowrap}
.den-ned .ql-editor .den-file > span{background:rgba(127,178,255,.09);border:1px solid rgba(127,178,255,.28);color:#C4DAFF}
.den-ned .ql-editor .den-chip > span{background:rgba(69,224,200,.08);border:1px solid rgba(69,224,200,.3);color:#9FF0E2}
.den-ned .ql-editor .den-chip-l{overflow:hidden;text-overflow:ellipsis;max-width:320px}
.den-ned .ql-editor .den-chip-s{color:#7D8B99;font-size:11.5px}
.den-ned .ql-editor .den-flash{animation:denFlash 1.5s ease-out}
@keyframes denFlash{0%,35%{box-shadow:0 0 0 3px rgba(255,123,92,.45);background-color:rgba(255,123,92,.12)}100%{box-shadow:0 0 0 3px rgba(255,123,92,0)}}
.den-ned .ql-editor .ql-align-center{text-align:center}.den-ned .ql-editor .ql-align-right{text-align:right}
`;

  window.DenNoteEditor = { load, create, toMarkdown, fromMarkdown, plain, scan, normUrl, safeUrl, hostOf, sizeLabel, openUpload, blobUrl };
})();
