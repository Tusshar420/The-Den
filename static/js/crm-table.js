// The Projects page as a CRM: the Table and Board (pipeline) views. This file holds what they work out and
// do - columns, filters, sorting, saved views, CSV export, moving a project between stages. They are drawn
// by views/crm-table.js. The choices (view, sort, columns, filters, saved views) are remembered per device
// in localStorage; nothing about them goes to the server. A stage move is PATCH /projects/{id} {stage}.

import { store } from './api.js';
import { more } from './data.js';

const KEY = 'octopus-crm-table';

// [key, label, colour] - same keys as crm.STAGES on the server. The status each stands for is set there.
export const STAGES = [
  ['lead', 'Lead', 'var(--text-meta)'], ['proposal', 'Proposal', 'var(--review)'], ['active', 'Active', 'var(--run)'],
  ['on_hold', 'On hold', 'var(--wait)'], ['done', 'Done', 'var(--ok)'], ['lost', 'Lost', 'var(--err)'],
];
export const stageInfo = key => STAGES.find(s => s[0] === key) || STAGES[0];
// Until the server sends a stage (it does after the Den restarts onto this code), work it out from the status - crm.STATUS_STAGE.
const STATUS_STAGE = { not_started: 'lead', in_progress: 'active', need_info: 'active', on_hold: 'on_hold', completed: 'done', archived: 'done' };
export const serverHasStages = d => d.projects.some(p => 'stage' in p);
const PRIO_RANK = { low: 0, medium: 1, high: 2, critical: 3 };
const HEALTH_RANK = { on_track: 0, at_risk: 1, off_track: 2 };
const STAGE_RANK = Object.fromEntries(STAGES.map(([k], i) => [k, i]));

// The projects' client. Clients arrive with CRM 3 (p.client_name); until then it is empty.
export const clientOf = p => (p.client_name || '').trim();

// ---------------------------------------------------------------- columns
// sort: the value to order by (null sorts last either way); text: what the CSV holds; num: right-aligned.
const iso = v => (v ? String(v) : null);
export const COLUMNS = [
  { k: 'name', label: 'Name', fixed: true, sort: r => r.p.name.toLowerCase(), text: r => r.p.name },
  { k: 'client', label: 'Client', sort: r => r.client.toLowerCase() || null, text: r => r.client },
  { k: 'stage', label: 'Stage', sort: r => STAGE_RANK[r.stage] ?? 0, text: r => stageInfo(r.stage)[1] },
  { k: 'priority', label: 'Priority', sort: r => PRIO_RANK[r.p.priority] ?? null, text: r => r.p.priority || '' },
  { k: 'health', label: 'Health', sort: r => HEALTH_RANK[r.p.health] ?? null, text: r => r.p.health || '' },
  { k: 'start', label: 'Start', sort: r => iso(r.p.start_date), text: r => r.p.start_date || '' },
  { k: 'due', label: 'Due', sort: r => iso(r.p.due_date), text: r => r.p.due_date || '' },
  { k: 'value', label: 'Value', num: true, sort: r => r.p.contract_value ?? null, text: r => (r.p.contract_value == null ? '' : `${r.p.contract_value} ${r.p.currency || ''}`.trim()) },
  { k: 'progress', label: 'Progress', num: true, sort: r => (r.p.progress && r.p.progress.percent) ?? null, text: r => { const n = r.p.progress && r.p.progress.percent; return n == null ? '' : n + '%'; } },
  { k: 'activity', label: 'Last activity', sort: r => r.activity || null, text: r => (r.activity ? new Date(r.activity).toISOString() : '') },
];
const COL_KEYS = COLUMNS.map(c => c.k);
const DEFAULT_COLS = ['name', 'client', 'stage', 'priority', 'health', 'due', 'value', 'progress', 'activity'];

// ---------------------------------------------------------------- state
const EMPTY_FILTERS = { category: '', stage: '', priority: '', health: '', client: '', tag: '', range: '', from: '', to: '', overdue: false };
export const NO_CLIENT = '__none';
export const RANGES = [['', 'Any time'], ['week', 'Next 7 days'], ['month', 'This month'], ['next30', 'Next 30 days'], ['custom', 'Custom…']];

const todayISO = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
// The [from, to] dates (YYYY-MM-DD) a due range means today; null ends are open.
export function rangeBounds(f, now = new Date()) {
  if (f.range === 'custom') return [f.from || null, f.to || null];
  const day = (add = 0) => todayISO(new Date(now.getFullYear(), now.getMonth(), now.getDate() + add));
  if (f.range === 'week') return [day(0), day(7)];
  if (f.range === 'next30') return [day(0), day(30)];
  if (f.range === 'month') return [todayISO(new Date(now.getFullYear(), now.getMonth(), 1)), todayISO(new Date(now.getFullYear(), now.getMonth() + 1, 0))];
  return [null, null];
}

// Starter saved views, added once; delete them freely.
const STARTERS = [
  { name: 'Work - due this month', f: { category: 'work', range: 'month' } },
  { name: 'Overdue', f: { overdue: true } },
  { name: 'At risk', f: { health: 'at_risk' } },
];

const uid = () => Math.random().toString(36).slice(2, 9);
const clean = (f = {}) => ({ ...EMPTY_FILTERS, ...Object.fromEntries(Object.entries(f).filter(([k]) => k in EMPTY_FILTERS)) });
const goodCols = cols => { const l = (Array.isArray(cols) ? cols : []).filter(k => COL_KEYS.includes(k)); return l.includes('name') ? l : ['name', ...l]; };

function defaults() {
  return { view: 'cards', sort: { k: 'name', dir: 'asc' }, cols: DEFAULT_COLS, f: clean(), q: '', saved: [], seeded: false, active: null };
}

function load() {
  const d = defaults();
  let raw = null;
  try { raw = JSON.parse(localStorage.getItem(KEY)); } catch { /* unreadable: start fresh */ }
  if (!raw || typeof raw !== 'object') raw = {};
  const sort = raw.sort && COL_KEYS.includes(raw.sort.k) ? { k: raw.sort.k, dir: raw.sort.dir === 'desc' ? 'desc' : 'asc' } : d.sort;
  const saved = (Array.isArray(raw.saved) ? raw.saved : []).filter(s => s && typeof s.name === 'string').slice(0, 30)
    .map(s => ({ id: String(s.id || uid()), name: s.name.slice(0, 60), f: clean(s.f), sort: s.sort && COL_KEYS.includes(s.sort.k) ? s.sort : d.sort, cols: goodCols(s.cols || DEFAULT_COLS), q: String(s.q || ''), view: s.view === 'board' ? 'board' : 'table' }));
  const seeded = !!raw.seeded;
  return {
    view: ['cards', 'table', 'board'].includes(raw.view) ? raw.view : 'cards', sort, cols: raw.cols ? goodCols(raw.cols) : d.cols,
    f: clean(raw.f), q: String(raw.q || ''), seeded,
    saved: seeded ? saved : [...saved, ...STARTERS.map(s => ({ id: uid(), name: s.name, f: clean(s.f), sort: d.sort, cols: d.cols, q: '', view: 'table' }))],
    active: typeof raw.active === 'string' ? raw.active : null,
  };
}

// The state lives in store.ui.crmt: the saved choices plus `panel` (open panel) and `saveName` (the name box).
export function crmt() {
  if (!store.ui.crmt) store.ui.crmt = { ...load(), panel: null, saveName: '', seeded: true };
  return store.ui.crmt;
}
function persist() {
  const { panel, saveName, ...keep } = crmt();
  try { localStorage.setItem(KEY, JSON.stringify(keep)); } catch { /* private mode: it just isn't remembered */ }
}

// ---------------------------------------------------------------- rows
const ms = s => { const t = Date.parse(s); return Number.isFinite(t) ? t : 0; };

// One row per project (General has no record): the project, its client, and when anything last happened -
// p.last_activity_at from the server (activities.last_activity: activities logged, stage changes, follow-ups,
// finished tasks), else when it was created. A server from before activities: the latest task activity, else
// when the record last changed, else when it was created.
export function rowsFor(d) {
  const last = new Map();
  for (const t of d.tasks) {
    const at = Math.max(ms(t.finished_at), ms(t.started_at), ms(t.created_at));
    if (at > (last.get(t.project_id) || 0)) last.set(t.project_id, at);
  }
  return d.projects.filter(p => !p.is_general).map(p => ({
    p, client: clientOf(p), stage: p.stage || STATUS_STAGE[p.status] || 'lead',
    activity: 'last_activity_at' in p ? ms(p.last_activity_at) || ms(p.created_at) : Math.max(last.get(p.id) || 0, ms(p.updated_at), ms(p.created_at)),
  }));
}

export function matches(r, f, q, now = new Date()) {
  const p = r.p;
  if (f.category && p.category !== f.category) return false;
  if (f.stage && r.stage !== f.stage) return false;
  if (f.priority && p.priority !== f.priority) return false;
  if (f.health && p.health !== f.health) return false;
  if (f.client === NO_CLIENT ? r.client : f.client && r.client !== f.client) return false;
  if (f.tag && !(p.tags || []).some(t => t.toLowerCase() === f.tag.toLowerCase())) return false;
  if (f.overdue && !(p.schedule && p.schedule.due_state === 'overdue')) return false;
  const [from, to] = rangeBounds(f, now);
  if (from || to) {
    if (!p.due_date || (from && p.due_date < from) || (to && p.due_date > to)) return false;
  }
  const s = q.trim().toLowerCase();
  if (s && !`${p.name} ${r.client} ${p.owner || ''} ${(p.tags || []).join(' ')} ${p.description || ''}`.toLowerCase().includes(s)) return false;
  return true;
}

export function sorted(rows, sort) {
  const col = COLUMNS.find(c => c.k === sort.k) || COLUMNS[0], dir = sort.dir === 'desc' ? -1 : 1;
  return [...rows].sort((a, b) => {
    const x = col.sort(a), y = col.sort(b);
    if (x == null || y == null) return x == null && y == null ? 0 : x == null ? 1 : -1;     // empty last, either way
    return (x < y ? -1 : x > y ? 1 : 0) * dir || a.p.name.localeCompare(b.p.name);
  });
}

// What the view shows: the rows after filters and search, in order.
export const visibleRows = (d, st = crmt()) => sorted(rowsFor(d).filter(r => matches(r, st.f, st.q)), st.sort);

export const filterCount = f => Object.entries(f).filter(([k, v]) => v && !['from', 'to'].includes(k) && !(k === 'range' && v === '')).length;
export const allTags = d => [...new Set(d.projects.flatMap(p => p.tags || []))].sort((a, b) => a.localeCompare(b));
export const allClients = d => [...new Set(d.projects.map(clientOf).filter(Boolean))].sort((a, b) => a.localeCompare(b));

// Contract value per stage column, one total per currency: [{currency, total}].
export function stageTotals(rows) {
  const by = new Map();
  for (const { p } of rows) if (p.contract_value != null) by.set(p.currency || 'USD', (by.get(p.currency || 'USD') || 0) + p.contract_value);
  return [...by].map(([currency, total]) => ({ currency, total }));
}

// ---------------------------------------------------------------- CSV
// A cell starting with = + - @ would run as a formula in a spreadsheet; a leading ' keeps it text.
const cell = v => {
  let s = String(v ?? '');
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};
export function toCSV(rows, cols) {
  const use = COLUMNS.filter(c => cols.includes(c.k));
  return [use.map(c => cell(c.label)).join(','), ...rows.map(r => use.map(c => cell(c.text(r))).join(','))].join('\r\n') + '\r\n';
}

// ---------------------------------------------------------------- actions
const num = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
export function money(v, cur) {
  if (v == null) return '';
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD', maximumFractionDigits: v % 1 ? 2 : 0 }).format(v); }
  catch { return `${cur || ''} ${v}`.trim(); }
}

let dragId = null;
const clearOver = () => document.querySelectorAll('.is-over').forEach(el => el.classList.remove('is-over'));

export function crmTableActions(h) {
  const { set, toast, fail } = h;
  const upd = patch => { Object.assign(crmt(), patch, { active: null }); persist(); set({}); };
  const keep = patch => { Object.assign(crmt(), patch); persist(); set({}); };

  // Move a project to a stage: shown at once, then saved; if the server says no, it goes back.
  async function moveTo(id, stage) {
    const d = store.data, p = d.projects.find(x => x.id === id);
    if (!p || (p.stage || STATUS_STAGE[p.status]) === stage) return;
    if (!serverHasStages(d)) return toast('Restart the Den to move projects between stages');
    const before = { stage: p.stage, status: p.status };
    p.stage = stage;
    set({});
    try {
      const rec = await more.updateProject(id, { stage });
      Object.assign(p, { stage: rec.stage, status: rec.status, schedule: rec.schedule, progress: rec.progress, updated_at: rec.updated_at });
      if (d.records && d.records[id]) d.records = { ...d.records, [id]: rec };
      set({});
    } catch (e) {
      Object.assign(p, before);
      set({});
      fail(e);
    }
  }

  return {
    ctView: el => { crmt().view = el.dataset.v; crmt().panel = null; persist(); set({}); },
    ctSort: el => {
      const s = crmt().sort, k = el.dataset.k;
      upd({ sort: s.k === k ? { k, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { k, dir: ['value', 'progress', 'activity', 'priority', 'health'].includes(k) ? 'desc' : 'asc' } });
    },
    ctPanel: el => { const s = crmt(); s.panel = s.panel === el.dataset.p ? null : el.dataset.p; set({}); },
    ctCol: el => {
      const k = el.dataset.k, cols = crmt().cols;
      if (k === 'name') return;
      upd({ cols: COL_KEYS.filter(c => c === 'name' || (c === k ? !cols.includes(c) : cols.includes(c))) });
    },
    ctColsReset: () => upd({ cols: DEFAULT_COLS }),
    ctFilter: el => {                                  // a <select>, <input type=date> or checkbox button
      const k = el.dataset.ctF, st = crmt();
      const v = el.dataset.toggle != null ? !st.f[k] : el.value;
      upd({ f: { ...st.f, [k]: v, ...(k === 'range' && v === 'custom' ? {} : k === 'range' ? { from: '', to: '' } : {}) } });
    },
    ctClear: () => upd({ f: clean(), q: '' }),
    ctSaveView: () => {
      const st = crmt(), name = st.saveName.trim();
      if (!name) return toast('Name the view first');
      const view = { id: uid(), name: name.slice(0, 60), f: { ...st.f }, sort: { ...st.sort }, cols: [...st.cols], q: st.q, view: st.view === 'board' ? 'board' : 'table' };
      keep({ saved: [...st.saved.filter(s => s.name.toLowerCase() !== name.toLowerCase()), view].slice(-30), active: view.id, saveName: '' });
      toast(`Saved view "${view.name}"`);
    },
    ctApplyView: el => {
      const v = crmt().saved.find(s => s.id === el.dataset.id);
      if (!v) return;
      keep({ f: clean(v.f), sort: v.sort, cols: goodCols(v.cols), q: v.q, view: v.view, active: v.id, panel: null });
    },
    ctDeleteView: el => { const st = crmt(); keep({ saved: st.saved.filter(s => s.id !== el.dataset.id), active: st.active === el.dataset.id ? null : st.active }); },
    ctExport: () => {
      const st = crmt(), csv = toCSV(visibleRows(store.data, st), st.cols);
      const url = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }));
      const a = Object.assign(document.createElement('a'), { href: url, download: `projects-${todayISO()}.csv` });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    },
    ctMove: el => moveTo(Number(el.dataset.id), el.value),
    ctDrop: moveTo,
  };
}

// The search box and the save-view name box.
export function crmtInput(path, v) {
  const st = crmt();
  if (path === 'ct.q') { st.q = v; st.active = null; persist(); }
  else if (path === 'ct.name') st.saveName = v;
}

// Drag a board card onto another column (desktop). Phones use the card's "Move to" menu instead.
export function crmDragSetup(onDrop) {
  document.addEventListener('dragstart', e => {
    const c = e.target.closest && e.target.closest('[data-drag-project]');
    if (!c) return;
    dragId = Number(c.dataset.dragProject);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', 'project:' + dragId);
    c.classList.add('is-dragging');
  });
  document.addEventListener('dragover', e => {
    const col = dragId != null && e.target.closest && e.target.closest('[data-drop-stage]');
    clearOver();
    if (!col) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    col.classList.add('is-over');
  });
  document.addEventListener('drop', e => {
    const col = dragId != null && e.target.closest && e.target.closest('[data-drop-stage]');
    if (!col) return;
    e.preventDefault();
    const id = dragId;
    dragId = null;
    clearOver();
    onDrop(id, col.dataset.dropStage);
  });
  document.addEventListener('dragend', () => { dragId = null; clearOver(); document.querySelectorAll('.is-dragging').forEach(el => el.classList.remove('is-dragging')); });
}
