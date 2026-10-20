// Glue between the To-do checklist (views/checklist.js) and app.js: its actions and form inputs.
// Ticking, editing and deleting show on screen at once and are sent to the server right after;
// if the server refuses, the list is reloaded so the screen never drifts from what is saved.
// Every change also goes into a short undo history (this browser tab only): each entry keeps the
// items it touched as they were before and after, and undo / redo sends the other side to
// POST /checklist/apply - deleted items come back under their old id, so later entries still fit.

import { store } from './api.js';
import { more, loadChecklist, chkSync } from './data.js';
import { IS_MAC } from './shortcuts.js';

const u = () => store.ui;
const items = () => store.data.checklist.items;
const byId = id => items().find(i => i.id === id);
const sorted = list => [...list].sort((a, b) => a.done - b.done || a.position - b.position || a.id - b.id);
const setItems = fn => { store.data = { ...store.data, checklist: { ...store.data.checklist, items: sorted(fn(items())) } }; };

// "todo" | "doing" (in progress) | "done". A server from before "in progress" only sends done.
export const statusOf = it => it.status || (it.done ? 'done' : 'todo');

// ui.chk: text (new-item box), dest (side panel: '' none / project id / null = follow the filter), filter (side panel:
// 'all' | 'none' | project id), state ('all' or one status), sort ('manual' | 'new' | 'old' | 'az'),
// full (null, or the list shown full screen: 'side' or a project id), edit (id being edited) with editText / editProject.
export const chkState = ui => ({ text: '', dest: null, filter: 'all', state: 'all', sort: 'manual', full: null, edit: null, editText: '', editProject: '', ...((ui || u()).chk || {}) });
const put = o => { u().chk = { ...chkState(), ...o }; };

const num = v => (v === '' || v == null ? null : Number(v));

export function checklistInput(path, v) {
  if (path === 'chk.text') put({ text: v });
  else if (path === 'chk.dest') put({ dest: v === '' ? '' : Number(v) });
  else if (path === 'chk.filter') put({ filter: v === 'all' || v === 'none' ? v : Number(v), dest: null });
  else if (path === 'chk.sort') put({ sort: v });
  else if (path === 'chk.editText') put({ editText: v });
  else if (path === 'chk.editProject') put({ editProject: v });
}

// Runs one change against the server while polls hold off; any failure reloads the true list.
async function sync(job, fail) {
  chkSync.busy++;
  try { await job(); } catch (e) { fail(e); }
  finally {
    chkSync.busy--;
    await loadChecklist().catch(() => {});
    store.set({});
  }
}

// ---------------------------------------------------------------- undo / redo
// An entry: { label, before, after }. Each side is a list of snapshots: { id, ...fields } (a whole item when
// it has to be rebuilt) or { id, gone: true } for "not there".
const hist = { past: [], future: [] };
const HIST_MAX = 50;
const MOD = IS_MAC ? '⌘' : 'Ctrl+';
export const chkHistory = () => ({ undo: hist.past.length ? hist.past[hist.past.length - 1].label : null, redo: hist.future.length ? hist.future[hist.future.length - 1].label : null, mod: MOD });
const record = (label, before, after) => {
  hist.past.push({ label, before, after });
  if (hist.past.length > HIST_MAX) hist.past.shift();
  hist.future = [];
};
const removed = id => ({ id, gone: true });
const name = t => `“${t.length > 32 ? t.slice(0, 31).trimEnd() + '…' : t}”`;

// Puts snapshots on screen now and on the server.
function applySnaps(snaps, fail) {
  const del = snaps.filter(s => s.gone).map(s => s.id), up = snaps.filter(s => !s.gone);
  setItems(list => {
    const out = list.filter(i => !del.includes(i.id));
    for (const s of up) {
      const i = out.findIndex(x => x.id === s.id);
      const next = { ...(i >= 0 ? out[i] : {}), ...s, ...('status' in s ? { done: s.status === 'done' ? 1 : 0 } : {}) };
      if (i >= 0) out[i] = next; else if (s.text) out.push(next);
    }
    return out;
  });
  sync(() => more.checklistApply({ upsert: up, delete: del }), fail);
}

// ⌘Z / ⇧⌘Z (or Ctrl+Y) while a to-do list is on screen: the action to run, or null. Text boxes keep their own undo.
export function chkUndoKey(e) {
  if (!(e.metaKey || e.ctrlKey) || e.altKey || !document.querySelector('.chk')) return null;
  const k = e.key.toLowerCase();
  if (k === 'z') return e.shiftKey ? 'chkRedo' : 'chkUndo';
  if (k === 'y' && !e.shiftKey) return 'chkRedo';
  return null;
}

export function checklistActions(h) {
  const { set, toast, fail } = h;
  const step = (from, to, side, verb) => {
    const e = from.pop();
    if (!e) return toast(`Nothing to ${verb}`);
    to.push(e);
    if (chkState().edit != null) put({ edit: null });
    applySnaps(e[side], fail);
    set({});
    toast(`${verb === 'undo' ? 'Undone' : 'Redone'}: ${e.label}`);
  };
  const setStatus = (it, status, label) => {
    if (statusOf(it) === status) return;
    record(label, [{ id: it.id, status: statusOf(it), done_at: it.done_at }], [{ id: it.id, status }]);
    setItems(list => list.map(i => (i.id === it.id ? { ...i, status, done: status === 'done' ? 1 : 0 } : i)));
    set({});
    sync(() => more.checklistUpdate(it.id, { status }), fail);
  };
  return {
    // Adds the box's text: one to-do per line. `data-pid` fixes the project (a project's own tab); the sidebar uses its picker.
    chkAdd: el => {
      const s = chkState();
      if (!s.text.trim()) return;
      const fixed = el.dataset.pid;
      const eff = s.dest !== null ? (s.dest === '' ? null : s.dest) : typeof s.filter === 'number' ? s.filter : null;
      const project_id = fixed ? Number(fixed) : eff;
      const text = s.text;
      put({ text: '' });
      document.querySelectorAll('textarea[data-input="chk.text"]').forEach(t => { t.value = ''; if (t._grow) t._grow.reset(); });
      sync(async () => {
        const r = await more.checklistAdd({ text, project_id });
        setItems(list => [...list, ...r.items]);
        record(r.items.length > 1 ? `Add ${r.items.length} to-dos` : `Add ${name(r.items[0].text)}`, r.items.map(i => removed(i.id)), r.items.map(i => ({ ...i })));
        if (r.items.length > 1) toast(`Added ${r.items.length} to-dos`);
      }, e => { put({ text }); fail(e); });
    },
    // The tick box: done <-> not done (an item in progress counts as not done).
    chkToggle: el => {
      const it = byId(Number(el.dataset.id));
      if (!it) return;
      const done = statusOf(it) === 'done';
      setStatus(it, done ? 'todo' : 'done', `${done ? 'Reopen' : 'Done'} ${name(it.text)}`);
    },
    // Start / stop: data-v is the status to move to.
    chkStatus: el => {
      const it = byId(Number(el.dataset.id)), v = el.dataset.v;
      if (!it || !['todo', 'doing', 'done'].includes(v)) return;
      setStatus(it, v, `${v === 'doing' ? 'Start' : v === 'done' ? 'Done' : 'Stop'} ${name(it.text)}`);
    },
    chkShow: el => { put({ state: el.dataset.v || 'all' }); set({}); },
    chkFull: el => {
      const ctx = el.dataset.ctx;
      put({ full: chkState().full != null || !ctx ? null : ctx === 'side' ? 'side' : Number(ctx) });
      set({});
    },
    chkUndo: () => step(hist.past, hist.future, 'before', 'undo'),
    chkRedo: () => step(hist.future, hist.past, 'after', 'redo'),
    // Drag to reorder: ids are the new order of one group's rows. They take over the same position
    // numbers between them, so rows hidden by a filter keep their place.
    chkReorder: ids => {
      const its = ids.map(byId).filter(Boolean);
      const slots = its.map(i => i.position).sort((a, b) => a - b);
      for (let k = 1; k < slots.length; k++) if (slots[k] <= slots[k - 1]) slots[k] = slots[k - 1] + 1;
      const moved = its.filter((i, k) => i.position !== slots[k]);
      if (!moved.length) return;
      const after = its.map((i, k) => ({ id: i.id, position: slots[k] })).filter((s, k) => its[k].position !== s.position);
      record('Reorder', moved.map(i => ({ id: i.id, position: i.position })), after);
      applySnaps(after, fail);
      set({});
    },
    chkDelete: el => {
      const id = Number(el.dataset.id), it = byId(id);
      if (!it) return;
      record(`Delete ${name(it.text)}`, [{ ...it }], [removed(id)]);
      setItems(list => list.filter(i => i.id !== id));
      if (chkState().edit === id) put({ edit: null });
      set({});
      sync(() => more.checklistDelete(id), fail);
    },
    chkEdit: el => {
      const it = byId(Number(el.dataset.id));
      if (!it) return;
      put({ edit: it.id, editText: it.text, editProject: it.project_id == null ? '' : String(it.project_id) });
      set({});
      setTimeout(() => { const t = document.querySelector('.chk-edit__text'); if (t) { t.focus(); t.select(); } }, 40);
    },
    chkEditCancel: () => { put({ edit: null }); set({}); },
    chkSave: () => {
      const s = chkState(), id = s.edit, text = s.editText.trim(), it = byId(id);
      if (id == null || !it) return;
      if (!text) return toast('A to-do needs some text');
      const project_id = num(s.editProject);
      if (text !== it.text || project_id !== it.project_id) record(`Edit ${name(text)}`, [{ id, text: it.text, project_id: it.project_id }], [{ id, text, project_id }]);
      setItems(list => list.map(i => (i.id === id ? { ...i, text, project_id } : i)));
      put({ edit: null });
      set({});
      sync(() => more.checklistUpdate(id, { text, project_id }), fail);
    },
    // Deletes every ticked to-do (of one project when `data-pid` is set).
    chkClearDone: el => {
      const pid = el.dataset.pid ? Number(el.dataset.pid) : null;
      const gone = items().filter(i => i.done && (pid == null || i.project_id === pid));
      if (!gone.length || !confirm(`Delete ${gone.length} completed to-do${gone.length === 1 ? '' : 's'}? You can undo this until you reload the page.`)) return;
      record(`Clear ${gone.length} done`, gone.map(i => ({ ...i })), gone.map(i => removed(i.id)));
      setItems(list => list.filter(i => !gone.includes(i)));
      set({});
      sync(() => more.checklistClear(pid), fail);
    },
  };
}
