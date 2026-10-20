// Glue between the Notes pad (views/pad.js) and app.js: its actions and form inputs, plus autosave.
// Typing updates the note on screen at once and is sent to the server a moment after you stop.

import { store } from './api.js';
import { more, loadPad } from './data.js';

const u = () => store.ui;
const items = () => store.data.pad.items;
const noteOf = id => items().find(n => n.id === id);
const putNote = n => { store.data = { ...store.data, pad: { ...store.data.pad, items: items().map(x => (x.id === n.id ? { ...x, ...n } : x)) } }; };

const evDirty = new Map();  // calendar item id -> notes text not yet on the server (a meeting's own notes, edited full-view)
const dirty = new Map();   // note id -> fields not yet on the server
let timer = 0;

export function padFlush() {
  clearTimeout(timer);
  const jobs = [...dirty.entries()];
  dirty.clear();
  const evJobs = [...evDirty.entries()];
  evDirty.clear();
  const evSaves = evJobs.map(([id, notes]) => more.updateEvent(id, { notes: notes.trim() || null }).then(
    () => { if (!evDirty.has(id)) { u().pad = { ...u().pad, status: 'Saved' }; store.set({}); } },
    () => { if (!evDirty.has(id)) evDirty.set(id, notes); u().pad = { ...u().pad, status: 'Not saved - will retry' }; store.set({}); },
  ));
  return Promise.all([...evSaves, ...jobs.map(([id, fields]) => more.padUpdate(id, fields).then(
    n => { if (!dirty.has(id)) { u().pad = { ...u().pad, status: 'Saved' }; putNote({ id, updated_at: n.updated_at }); store.set({}); } },
    () => { dirty.set(id, { ...fields, ...(dirty.get(id) || {}) }); u().pad = { ...u().pad, status: 'Not saved - will retry' }; store.set({}); },
  ))]);
}

function editEventNotes(id, text) {
  const tl = store.data.timeline;
  store.data = { ...store.data, timeline: { ...tl, events: tl.events.map(x => (x.id === id ? { ...x, notes: text } : x)) } };
  evDirty.set(id, text);
  u().pad = { ...u().pad, status: 'Saving…' };
  clearTimeout(timer);
  timer = setTimeout(padFlush, 700);
}

function edit(id, fields) {
  putNote({ id, ...fields });
  dirty.set(id, { ...(dirty.get(id) || {}), ...fields });
  u().pad = { ...u().pad, status: 'Saving…' };
  clearTimeout(timer);
  timer = setTimeout(padFlush, 700);
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => { padFlush(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) padFlush(); });
}

export function padState() {
  return { sel: null, ev: null, filter: 'all', q: '', status: '', save: null, ...(u().pad || {}) };
}

export function padInput(path, v) {
  const p = padState(), id = p.sel;
  if (path === 'pad.q') { u().pad = { ...p, q: v }; store.set({}); return; }
  if (path.startsWith('pad.save.') && p.save) { u().pad = { ...p, save: { ...p.save, [path.slice(9)]: v, error: '' } }; return; }
  if (path === 'pad.evnotes' && p.ev) { editEventNotes(p.ev, v); return; }
  if (!id || !noteOf(id)) return;
  if (path === 'pad.title') edit(id, { title: v });
  else if (path === 'pad.body') edit(id, { body: v });
  else if (path === 'pad.project') edit(id, { project_id: v ? Number(v) : null });
  else if (path === 'pad.event') edit(id, { event_id: v ? Number(v) : null });
}

export function padActions(h) {
  const { set, toast, fail, navigate } = h;
  const patch = o => set({ pad: { ...padState(), ...o } });
  return {
    padOpen: el => { padFlush(); patch({ sel: Number(el.dataset.id), ev: null, save: null }); },
    padBack: () => { padFlush(); patch({ sel: null, ev: null, save: null }); },
    // A meeting's notes in the full-page editor (from the Notes page, a project's Notes tab, or the calendar item's sheet).
    openMeetingNotes: el => {
      padFlush();
      set({ sheet: null, evEdit: null, pad: { ...padState(), sel: null, ev: Number(el.dataset.id), save: null, status: '' } });
      navigate('notes');
    },
    // A new note tied to a meeting, opened full-page.
    padNewForMeeting: el => {
      const e = (store.data.timeline.events || []).find(x => x.id === Number(el.dataset.id));
      if (!e) return;
      more.padCreate({ title: e.title, body: '', project_id: e.project_id, event_id: e.id }).then(n => {
        store.data = { ...store.data, pad: { ...store.data.pad, items: [n, ...items()] } };
        patch({ sel: n.id, ev: null, save: null, status: '' });
        setTimeout(() => { const t = document.querySelector('.pad__body'); if (t) t.focus(); }, 60);
      }, fail);
    },
    padFilter: el => patch({ filter: ['all', 'unfiled', 'meeting'].includes(el.dataset.v) ? el.dataset.v : Number(el.dataset.v) }),
    padNew: () => {
      const f = padState().filter;
      more.padCreate({ title: '', body: '', project_id: typeof f === 'number' ? f : null }).then(n => {
        store.data = { ...store.data, pad: { ...store.data.pad, items: [n, ...items()] } };
        patch({ sel: n.id, ev: null, save: null, status: '' });
        setTimeout(() => { const t = document.querySelector('.pad__title'); if (t) t.focus(); }, 60);
      }, fail);
    },
    padDelete: () => {
      const id = padState().sel, n = id && noteOf(id);
      if (!n || ((n.title || n.body) && !confirm(`Delete "${n.title || 'this note'}"? This can't be undone.`))) return;
      dirty.delete(id);
      more.padDelete(id).then(() => { patch({ sel: null, save: null }); toast('Note deleted'); return loadPad(); }, fail);
    },
    padSaveOpen: () => {
      const n = noteOf(padState().sel);
      if (n) patch({ save: { project_id: n.project_id == null ? '' : String(n.project_id), folder: '', filename: '', busy: false, error: '' } });
    },
    padSaveClose: () => patch({ save: null }),
    padSaveGo: async () => {
      const p = padState(), s = p.save, id = p.sel;
      if (!s || s.busy || !id) return;
      patch({ save: { ...s, busy: true, error: '' } });
      try {
        await padFlush();
        const r = await more.padSaveFile(id, { project_id: s.project_id ? Number(s.project_id) : null, folder: s.folder.trim(), filename: s.filename.trim() || null });
        putNote(r.note);
        patch({ save: null });
        toast(`Saved to ${r.path}`);
      } catch (e) { patch({ save: { ...s, busy: false, error: e && e.message ? e.message : String(e) } }); }
    },
  };
}
