// Tushar's Workspace - dashboard entry point: API-key gate, hash router, render loop,
// actions (event delegation on data-action / data-submit / data-input), gestures,
// keyboard shortcuts and polling.

import { store, subscribe, apiKey, endpoints } from './api.js';
import { more, refresh, startPolling, loadThread, loadProject, dismissTask, projectById, taskById, generalProject,
  realProjects, pendingApprovals, queuedTasks, isGeneral } from './data.js';
import { derive } from './derive.js';
import { morph } from './morph.js';
import { polish } from './polish.js';
import { pageSwipe, swipeDecision, dragReorder, onDoubleTap, autoGrow } from './gestures.js';
import { plural } from './format.js';
import { modelLabel } from './ui.js';
import { topbar, sidebar, tabbar, toast as toastHTML, homeSkeleton, listSkeleton } from './views/shell.js';
import { splash, gate, markSeen } from './views/splash.js';
import { home } from './views/home.js';
import { projects } from './views/projects.js';
import { project } from './views/project.js';
import { recipes } from './views/recipes.js';
import { activity } from './views/activity.js';
import { chatPane } from './views/chat.js';
import { approvalPane } from './views/approval.js';
import { sheet, WHENS } from './views/sheets.js';

const root = document.getElementById('app');
const KEY_STORAGE = 'cq_api_key';
const LAST_PROJECT = 'octopus-last-project';
const SKELETON_MS = 520;
const VIEWS = ['home', 'projects', 'recipes', 'activity'];

// ---------------------------------------------------------------- UI state (on top of api.js's store.ui)
const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch {} return null; };
Object.assign(store.ui, {
  booted: false, splashLeaving: false, bootDate: new Date(), bootRand: Math.random(), gateError: '',
  palette: null, onboard: { name: '', path: '', error: '' }, todoText: '', reply: '', denyNote: '',
  busy: {}, polishing: null, focus: null, lastProjectId: Number(ls(LAST_PROJECT)) || null,
});
store.ui.ask.touched = false;
const ui = () => store.ui;

let interacting = false, pending = false, frame = 0;
function requestRender() {
  if (interacting) { pending = true; return; }
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; render(); });
}
subscribe(requestRender);
const set = patch => { Object.assign(store.ui, patch); requestRender(); };
const setData = patch => { store.data = { ...store.data, ...patch }; requestRender(); };

let toastSeq = 0;
function toast(text, opts = {}) {
  set({ toast: { id: ++toastSeq, text, action: opts.action, onAction: opts.onAction, until: Date.now() + 4000 } });
}
const fail = e => toast(e && e.message ? e.message : String(e));

const isDesk = () => window.innerWidth >= 900;
function rememberProject(pid) {
  const p = projectById(pid);
  if (!p || isGeneral(p)) return;
  store.ui.lastProjectId = pid; ls(LAST_PROJECT, String(pid));
}
const defaultProjectId = () => {
  const u = ui();
  if (u.view === 'project' && projectById(u.projectId)) return u.projectId;
  if (u.lastProjectId && projectById(u.lastProjectId)) return u.lastProjectId;
  const real = realProjects()[0];
  return real ? real.id : (generalProject() || {}).id;
};

// ---------------------------------------------------------------- router (#/home, #/projects, #/project/12, #/recipes, #/activity)
let skeletonTimer = 0;
function parseHash() {
  const [v, id] = location.hash.replace(/^#\/?/, '').split('/');
  if (v === 'project' && id && !Number.isNaN(Number(id))) return { view: 'project', projectId: Number(id) };
  return { view: VIEWS.includes(v) ? v : 'home', projectId: ui().projectId };
}
function applyRoute() {
  const r = parseHash();
  const u = ui();
  const changed = r.view !== u.view || (r.view === 'project' && r.projectId !== u.projectId);
  if (!changed) return requestRender();
  const patch = { view: r.view, projectId: r.projectId, skeleton: true };
  if (r.view === 'project' && r.projectId !== u.projectId) patch.projectTab = 'work';
  if (!isDesk()) patch.pane = null;
  if (r.view === 'project') {
    rememberProject(r.projectId);
    if (isDesk() && !u.ask.touched) u.ask.projectId = r.projectId;
    loadProject(r.projectId).catch(() => {});
  }
  set(patch);
  const main = document.getElementById('main');
  if (main) main.scrollTop = 0;
  clearTimeout(skeletonTimer);
  skeletonTimer = setTimeout(() => set({ skeleton: false }), SKELETON_MS);
}
function navigate(view, projectId) {
  const hash = view === 'project' ? `#/project/${projectId}` : `#/${view}`;
  if (location.hash !== hash) location.hash = hash; else applyRoute();
}
window.addEventListener('hashchange', applyRoute);

function neighbors() {
  if (isDesk()) return { prev: null, next: null };
  const v = ui().view;
  if (v === 'project') return { prev: () => navigate('projects'), next: null };
  const i = VIEWS.indexOf(v);
  return { prev: i > 0 ? () => navigate(VIEWS[i - 1]) : null, next: i >= 0 && i < VIEWS.length - 1 ? () => navigate(VIEWS[i + 1]) : null };
}

// ---------------------------------------------------------------- render
function context() {
  const d = store.data, u = ui(), now = Date.now();
  const w = derive(d, now);
  return { d, ui: u, w, now, desk: isDesk(), compact: u.density === 'compact', project: u.view === 'project' ? projectById(u.projectId) : null };
}

function page(c) {
  if (!c.d.loaded || c.ui.skeleton || !c.ui.booted) return c.ui.view === 'home' ? homeSkeleton(c) : listSkeleton(c);
  switch (c.ui.view) {
    case 'projects': return projects(c);
    case 'project': return project(c);
    case 'recipes': return recipes(c);
    case 'activity': return activity(c);
    default: return home(c);
  }
}

function pane(c) {
  const p = c.ui.pane;
  if (!p) return '';
  if (p.type === 'approval') {
    const a = pendingApprovals(c.d).find(x => x.id === p.approvalId);
    if (a) return approvalPane(c, a);
    // Decided (here or elsewhere): show the chat it belongs to instead.
    if (p.taskId != null) return chatPane(c, p.taskId);
    return '';
  }
  return chatPane(c, p.taskId);
}

function appHTML(c) {
  if (!apiKey()) return gate(c);
  return `<div class="app${c.desk ? ' app--desk' : ''}" data-density="${c.ui.density}" data-key="app">
    ${c.desk ? sidebar(c) : ''}
    <main class="app__main" id="main">
      ${topbar(c)}
      <div class="pager" data-pager data-live-style>${page(c)}</div>
    </main>
    ${pane(c)}
    ${c.desk ? '' : tabbar(c)}
    ${sheet(c)}
    ${toastHTML(c)}
    ${c.ui.booted ? '' : splash(c)}
  </div>`;
}

const bound = new WeakSet();
let lastPaneKey = null;

function render() {
  const c = context();
  document.documentElement.classList.toggle('is-desk', c.desk);
  const paneEl = root.querySelector('.pane');
  const nearBottom = paneEl ? paneEl.scrollHeight - paneEl.scrollTop - paneEl.clientHeight < 80 : false;
  morph(root, appHTML(c));
  bindGestures(c);

  const newPane = root.querySelector('.pane');
  const key = newPane ? newPane.getAttribute('data-key') : null;
  if (newPane && key !== lastPaneKey) newPane.scrollTop = key.startsWith('pane-chat') ? newPane.scrollHeight : 0;
  else if (newPane && nearBottom && key.startsWith('pane-chat')) newPane.scrollTop = newPane.scrollHeight;
  lastPaneKey = key;

  if (c.ui.focus) {
    const el = document.getElementById(c.ui.focus);
    if (el) { el.focus(); if (el.setSelectionRange) el.setSelectionRange(el.value.length, el.value.length); }
    store.ui.focus = null;
  }
}

function bindGestures(c) {
  for (const el of root.querySelectorAll('[data-splash]')) {
    if (bound.has(el)) continue; bound.add(el);
    onDoubleTap(el, dismissSplash);
  }
  for (const el of root.querySelectorAll('[data-pager]')) {
    if (bound.has(el)) continue; bound.add(el);
    pageSwipe(el, neighbors);
  }
  for (const card of root.querySelectorAll('[data-swipe]')) {
    if (bound.has(card)) continue; bound.add(card);
    const id = Number(card.dataset.id);
    swipeDecision(card, card.previousElementSibling, { onApprove: () => approve(id), onDeny: () => deny(id) });
  }
  for (const list of root.querySelectorAll('[data-drag-list]')) {
    if (bound.has(list)) continue; bound.add(list);
    dragReorder(list, { onDrop: ids => (list.dataset.dragList === 'queue' ? reorderQueue(ids.map(Number)) : reorderBacklog(Number(list.dataset.pid), ids.map(Number))) });
  }
  for (const ta of root.querySelectorAll('textarea[data-autogrow]')) {
    if (!bound.has(ta)) { bound.add(ta); ta._grow = autoGrow(ta, Number(ta.dataset.autogrow)); }
    if (!ta.value) ta.style.height = '';
  }
}

// Hold renders while a finger is on a drag handle or the approval card, so a poll
// can't reset the transform mid-gesture.
document.addEventListener('pointerdown', e => { if (e.target.closest('.handle, [data-swipe]')) interacting = true; }, true);
const endInteract = () => setTimeout(() => { if (!interacting) return; interacting = false; if (pending) { pending = false; requestRender(); } }, 0);
window.addEventListener('pointerup', endInteract, true);
window.addEventListener('pointercancel', endInteract, true);
window.addEventListener('resize', requestRender);

function dismissSplash() {
  if (ui().booted || ui().splashLeaving) return;
  set({ splashLeaving: true, skeleton: false });
  markSeen();
  setTimeout(() => set({ booted: true, splashLeaving: false }), 420);
}

// ---------------------------------------------------------------- actions
async function busy(key, fn) {
  if (ui().busy[key]) return;
  set({ busy: { ...ui().busy, [key]: true } });
  try { return await fn(); } finally { const b = { ...ui().busy }; delete b[key]; set({ busy: b }); }
}
const again = () => refresh().catch(() => {});

function openChat(taskId) {
  const t = taskById(taskId);
  const rid = t ? (t.thread_id || t.id) : taskId;
  set({ pane: { type: 'chat', taskId: rid }, reply: '', sheet: null });
  loadThread(rid).catch(() => {});
}
function openApproval(approvalId) {
  const a = pendingApprovals().find(x => x.id === approvalId);
  set({ pane: { type: 'approval', approvalId, taskId: a ? a.task_id : null }, denyNote: '', sheet: null });
  if (a) more.approvalContext(approvalId).then(v => setData({ approvalCtx: { ...store.data.approvalCtx, [approvalId]: v } })).catch(() => {});
}

function afterDecision(approvalId) {
  setData({ approvals: store.data.approvals.filter(a => a.id !== approvalId) });
  const p = ui().pane;
  if (p && p.type === 'approval' && p.approvalId === approvalId && p.taskId != null) openChat(p.taskId);
}
async function approve(id, always = false) {
  const a = pendingApprovals().find(x => x.id === id);
  if (!a) return;
  await busy('ap' + id, async () => {
    try {
      await more.decide(id, true, always ? { always: true } : {});
      const ctx = store.data.approvalCtx[id];
      const p = projectById(a.project_id);
      toast(always && ctx ? `Approved · ${ctx.allow_entry} is always allowed in ${p ? p.name : 'this project'}` : `Approved · resuming ${a.task_prompt}`);
      afterDecision(id);
    } catch (e) { fail(e); }
  });
  again();
}
async function deny(id, note) {
  try {
    await more.decide(id, false, note ? { note } : {});
    toast('Denied · Octopus will wrap up without it');
    set({ denyNote: '' });
    afterDecision(id);
  } catch (e) { fail(e); }
  again();
}

function reorderQueue(ids) {
  const pos = new Map(ids.map((id, i) => [id, i]));
  setData({ tasks: store.data.tasks.map(t => (pos.has(t.id) ? { ...t, queue_pos: pos.get(t.id) } : t)) });
  more.reorderQueue(ids).then(again, fail);
}
function reorderBacklog(pid, ids) {
  const todos = store.data.todos[pid] || [];
  const rest = todos.filter(t => !ids.includes(t.id)).map(t => t.id);
  const order = [...ids, ...rest];
  const byId = new Map(todos.map(t => [t.id, t]));
  setData({ todos: { ...store.data.todos, [pid]: order.map(id => byId.get(id)).filter(Boolean) } });
  endpoints.reorderTodos(pid, order).then(v => setData({ todos: { ...store.data.todos, [pid]: v } }), fail);
}

async function runAgain(id) {
  const t = taskById(id);
  if (!t) return;
  try {
    if (t.lane === 'api') {
      await more.send(t.prompt, t.project_id, t.model_id, t.priority);
      toast('Asked again · ' + modelLabel(t.model_id));
    } else {
      const r = await endpoints.addTask(t.project_id, t.prompt, t.priority);
      toast(`Queued again · #${r.position || '?'}`);
    }
  } catch (e) { fail(e); }
  again();
}

async function runRecipe(id, pid) {
  const r = (store.data.recipes || []).find(x => x.id === id);
  if (!r) return;
  await busy('rc' + id, async () => {
    try {
      if (r.target === 'all' && pid == null) {
        const out = await more.runRecipe(id, {});
        toast(`Queued ${r.name} for ${plural(out.tasks.length, 'project')}`);
      } else {
        const target = pid ?? (/^\d+$/.test(r.target) ? Number(r.target) : defaultProjectId());
        const out = await more.runRecipe(id, { project_id: target });
        const p = projectById(target);
        rememberProject(target);
        toast(`${r.name} · ${p ? p.name : ''} · #${out.tasks[0].position || '?'} in line`);
      }
      set({ sheet: null, palette: null });
    } catch (e) { fail(e); }
  });
  again();
}

function openPalette(pre = {}) {
  const pid = pre.projectId ?? defaultProjectId();
  const s = store.data.projectSettings[pid];
  set({ sheet: 'palette', palette: { text: '', model: 'auto', priority: s ? s.default_priority : 0, when: 'now', sending: false, error: '', projectId: pid, ...pre }, focus: 'palInput' });
}

async function submitPalette() {
  const pal = ui().palette;
  if (!pal || !pal.text.trim() || pal.sending) return;
  pal.sending = true; pal.error = ''; requestRender();
  const text = pal.text.trim();
  const p = projectById(pal.projectId);
  try {
    if (pal.when !== 'now') {
      const w = WHENS.find(x => x.k === pal.when);
      const date = w.tomorrow ? (() => { const d = new Date(Date.now() + 86400000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; })() : undefined;
      await more.addSchedule({ prompt: text, project_id: pal.projectId, cadence: w.cadence, time: w.time, date, model: pal.model, priority: pal.priority });
      toast('Scheduled · ' + w.l);
    } else {
      const r = await more.send(text, p && !p.is_general ? p.id : null, pal.model, pal.priority);
      toast(r.decision.lane === 'api'
        ? `→ ${r.decision.model_label} · answering · #${r.task.id}`
        : `Queued · ${p ? p.name : 'General'} · ${r.decision.model_label} · #${r.task.position || '?'} in line`);
    }
    rememberProject(pal.projectId);
    set({ sheet: null, palette: null });
  } catch (e) {
    if (ui().palette) { ui().palette.sending = false; ui().palette.error = 'Could not send: ' + e.message; requestRender(); }
    return;
  }
  again();
}

async function submitAsk() {
  const a = ui().ask;
  const text = a.text.trim();
  if (!text || a.sending) return;
  Object.assign(a, { sending: true, result: null, menu: null }); requestRender();
  try {
    const gen = generalProject();
    const pid = a.projectId != null && !(gen && a.projectId === gen.id) ? a.projectId : null;
    const r = await more.send(text, pid, a.model, pid ? (store.data.projectSettings[pid] || {}).default_priority || 0 : 0);
    const reason = r.decision.reason || '';
    const why = r.decision.category === 'manual' || reason.startsWith(r.decision.category) ? reason : `${r.decision.category}: ${reason}`;
    Object.assign(a, { sending: false, text: '', result: { model: r.decision.model_label, lane: r.decision.lane, why, id: r.task.id } });
    const ta = document.getElementById('askInput');
    if (ta) { ta.value = ''; ta.style.height = ''; }
    if (pid) rememberProject(pid);
  } catch (e) {
    Object.assign(a, { sending: false, result: { error: e.message } });
  }
  requestRender();
  again();
}

async function sendReply(text) {
  const p = ui().pane;
  const body = (text ?? ui().reply).trim();
  if (!p || !body) return;
  const th = store.data.threads[p.taskId];
  const msgs = th ? th.tasks : store.data.tasks.filter(t => (t.thread_id || t.id) === p.taskId);
  const rootTask = msgs.find(t => t.id === p.taskId) || taskById(p.taskId);
  if (!rootTask) return;
  const latest = taskById(msgs[msgs.length - 1].id) || msgs[msgs.length - 1];
  if (rootTask.lane === 'api' && latest.status === 'running') return toast('Wait for this answer to finish');
  await busy('reply', async () => {
    try {
      await endpoints.addTask(rootTask.project_id, body, rootTask.priority || 0, rootTask.id);
      if (text == null) {
        set({ reply: '' });
        const ta = document.getElementById('replyInput');
        if (ta) { ta.value = ''; ta.style.height = ''; }
      }
      await loadThread(rootTask.id);
    } catch (e) { fail(e); }
  });
  again();
}

const TEXT_TARGETS = {
  ask: { get: () => ui().ask.text, set: v => { ui().ask.text = v; ui().ask.result = null; }, id: 'askInput' },
  reply: { get: () => ui().reply, set: v => { store.ui.reply = v; }, id: 'replyInput' },
  palette: { get: () => (ui().palette || {}).text || '', set: v => { if (ui().palette) ui().palette.text = v; }, id: 'palInput' },
};
function putText(target, v) {
  const t = TEXT_TARGETS[target];
  t.set(v);
  const el = document.getElementById(t.id);
  if (el) { el.value = v; if (el._grow) el._grow.fit(); }
  requestRender();
}
async function doPolish(target) {
  const t = TEXT_TARGETS[target];
  if (!t || !t.get().trim() || ui().polishing) return;
  set({ polishing: target });
  const r = await polish(t.get(), text => endpoints.polish(text));
  set({ polishing: null });
  if (r.changed) {
    putText(target, r.text);
    toast('Text polished', { action: 'Undo', onAction: () => putText(target, r.original) });
  } else toast('Already reads well');
}

async function addProjectSubmit() {
  const ob = ui().onboard;
  const name = ob.name.trim(), path = ob.path.trim();
  if (!name || !path) { ob.error = 'Give it a name and the folder path on this Mac.'; return requestRender(); }
  await busy('addProject', async () => {
    try {
      const p = await endpoints.addProject(name, path);
      set({ onboard: { name: '', path: '', error: '' }, sheet: null });
      rememberProject(p.id);
      toast(`Added ${p.name} · ready when you are`);
    } catch (e) { ob.error = e.message; }
  });
  again();
}

async function patchProjectSettings(pid, patch) {
  const cur = store.data.projectSettings[pid];
  if (cur) setData({ projectSettings: { ...store.data.projectSettings, [pid]: { ...cur, ...patch } } });
  try {
    const v = await more.setProjectSettings(pid, patch);
    setData({ projectSettings: { ...store.data.projectSettings, [pid]: v } });
  } catch (e) { fail(e); }
}

function patchTodos(pid, fn) {
  const todos = store.data.todos[pid] || [];
  setData({ todos: { ...store.data.todos, [pid]: todos.map(fn) } });
}

const num = v => (v === '' || v == null ? null : Number(v));

const ACTIONS = {
  go: el => {
    const v = el.dataset.view;
    if (el.dataset.filter) store.ui.activityFilter = el.dataset.filter;
    if (el.dataset.quick) store.ui.projectsList = { ...ui().projectsList, quick: el.dataset.quick, open: {} };
    navigate(v);
  },
  goBack: () => navigate('projects'),
  openProject: el => navigate('project', Number(el.dataset.id)),
  openPalette: el => openPalette(el.dataset.pid ? { projectId: Number(el.dataset.pid) } : {}),
  openSheet: el => set({ sheet: el.dataset.sheet, onboard: { ...ui().onboard, error: '' }, focus: el.dataset.sheet === 'addProject' ? 'addName' : null }),
  closeSheet: () => set({ sheet: null, palette: null }),
  scrimClose: (el, e) => { if (e.target === el) set({ sheet: null, palette: null }); },
  toggleDensity: () => {
    const compact = ui().density !== 'compact';
    ls('octopus-compact', compact ? '1' : '0');
    set({ density: compact ? 'compact' : 'comfortable' });
    toast(compact ? 'Compact layout on' : 'Comfortable layout on');
  },
  toastAction: () => { const t = ui().toast; set({ toast: null }); if (t && t.onAction) t.onAction(); },
  openChat: el => openChat(Number(el.dataset.id)),
  openApproval: el => openApproval(Number(el.dataset.id)),
  openApprovalForTask: el => { const a = pendingApprovals().find(x => x.task_id === Number(el.dataset.id)); a ? openApproval(a.id) : openChat(Number(el.dataset.id)); },
  closePane: () => set({ pane: null }),
  approve: el => approve(Number(el.dataset.id)),
  approveAlways: el => approve(Number(el.dataset.id), true),
  deny: el => deny(Number(el.dataset.id)),
  retry: el => busy('rt' + el.dataset.id, async () => {
    try { await more.requeue(Number(el.dataset.id), true); toast('Retry queued at the front'); } catch (e) { fail(e); }
  }).then(again),
  dismiss: el => {
    const id = Number(el.dataset.id);
    dismissTask(id);
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, dismissed: true } : t)) });
  },
  reviewed: el => more.reviewed(Number(el.dataset.id)).then(() => { toast('Marked as reviewed'); again(); }, fail),
  runAgain: el => runAgain(Number(el.dataset.id)),
  toTop: el => {
    const id = Number(el.dataset.id);
    const min = Math.min(0, ...queuedTasks().map(t => t.queue_pos ?? 0));
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, queue_pos: min - 1 } : t)) });
    more.toTop(id).then(() => { toast('Moved to the top'); again(); }, fail);
  },
  cancelQueued: el => {
    const id = Number(el.dataset.id);
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, status: 'cancelled' } : t)) });
    endpoints.cancel(id).then(() => {
      toast('Cancelled', { action: 'Undo', onAction: () => more.requeue(id, false).then(again, fail) });
      again();
    }, fail);
  },
  togglePingBack: () => {
    const on = !(store.data.settings && store.data.settings.notify_on_resume);
    setData({ settings: { ...store.data.settings, notify_on_resume: on } });
    more.setSettings({ notify_on_resume: on }).then(v => setData({ settings: v }), fail);
  },
  runRecipe: el => runRecipe(Number(el.dataset.id), num(el.dataset.pid)),
  scheduleRecipe: el => {
    const r = (store.data.recipes || []).find(x => x.id === Number(el.dataset.id));
    if (!r) return;
    const pid = num(el.dataset.pid) ?? (/^\d+$/.test(r.target) ? Number(r.target) : defaultProjectId());
    openPalette({ text: r.prompt, when: 'daily', projectId: pid });
  },
  toggleSchedule: el => {
    const id = Number(el.dataset.id);
    const s = (store.data.schedules || []).find(x => x.id === id);
    if (!s) return;
    setData({ schedules: store.data.schedules.map(x => (x.id === id ? { ...x, enabled: s.enabled ? 0 : 1 } : x)) });
    more.setSchedule(id, !s.enabled).then(again, fail);
  },
  askMenu: el => { const a = ui().ask; a.menu = a.menu === el.dataset.menu ? null : el.dataset.menu; requestRender(); },
  askPick: el => { Object.assign(ui().ask, { model: el.dataset.id, menu: null, result: null, touched: true }); requestRender(); },
  askTarget: el => { Object.assign(ui().ask, { projectId: num(el.dataset.id), menu: null, result: null, touched: true }); requestRender(); },
  closeMenus: () => { ui().ask.menu = null; store.ui.projectsList = { ...ui().projectsList, menu: null }; requestRender(); },
  submitAsk: () => submitAsk(),
  polish: el => doPolish(el.dataset.target),
  plQuick: el => set({ projectsList: { ...ui().projectsList, quick: el.dataset.q, open: {} } }),
  plMenu: el => { const pl = ui().projectsList; set({ projectsList: { ...pl, menu: pl.menu === el.dataset.menu ? null : el.dataset.menu } }); },
  plSort: el => set({ projectsList: { ...ui().projectsList, sort: el.dataset.sort, menu: null } }),
  plFilter: el => { const pl = ui().projectsList, k = el.dataset.f; set({ projectsList: { ...pl, [k]: !pl[k] } }); },
  plSearch: () => { const pl = ui().projectsList; set({ projectsList: { ...pl, searchOpen: !pl.searchOpen, q: pl.searchOpen ? '' : pl.q, menu: null }, focus: pl.searchOpen ? null : 'plSearch' }); },
  plSearchClose: () => set({ projectsList: { ...ui().projectsList, q: '', searchOpen: false } }),
  plToggle: el => { const pl = ui().projectsList; set({ projectsList: { ...pl, open: { ...pl.open, [el.dataset.id]: el.dataset.open !== 'true' } } }); },
  plClear: () => set({ projectsList: { ...ui().projectsList, quick: 'all', q: '', searchOpen: false, showPaused: true, onlyActive: false, open: {} } }),
  projectTab: el => { set({ projectTab: el.dataset.tab }); const m = document.getElementById('main'); const tabs = root.querySelector('.tabs'); if (m && tabs && m.scrollTop > tabs.offsetTop) m.scrollTop = Math.max(0, tabs.offsetTop - 80); },
  togglePause: () => {
    const p = projectById(ui().projectId);
    if (!p) return;
    setData({ projects: store.data.projects.map(x => (x.id === p.id ? { ...x, active: p.active ? 0 : 1 } : x)) });
    endpoints.setActive(p.id, !p.active).then(() => { toast(p.active ? `Paused ${p.name} · its tasks will be skipped` : `Resumed ${p.name}`); again(); }, fail);
  },
  todoCheck: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const t = (store.data.todos[pid] || []).find(x => x.id === id);
    if (!t) return;
    patchTodos(pid, x => (x.id === id ? { ...x, checked: t.checked ? 0 : 1 } : x));
    endpoints.updateTodo(id, { checked: !t.checked }).catch(fail);
  },
  backlogSelectAll: el => {
    const pid = ui().projectId, on = el.dataset.all !== '1';
    const todos = (store.data.todos[pid] || []).filter(t => t.status === 'pending' && !!t.checked !== on);
    patchTodos(pid, x => (x.status === 'pending' ? { ...x, checked: on ? 1 : 0 } : x));
    Promise.all(todos.map(t => endpoints.updateTodo(t.id, { checked: on }))).catch(fail);
  },
  backlogDelete: () => {
    const pid = ui().projectId;
    const sel = (store.data.todos[pid] || []).filter(t => t.status === 'pending' && t.checked);
    setData({ todos: { ...store.data.todos, [pid]: (store.data.todos[pid] || []).filter(t => !sel.includes(t)) } });
    Promise.all(sel.map(t => endpoints.deleteTodo(t.id))).then(() => toast('Deleted ' + sel.length), fail);
  },
  backlogRun: () => busy('backlogRun', async () => {
    const pid = ui().projectId;
    const n = (store.data.todos[pid] || []).filter(t => t.status === 'pending' && t.checked).length;
    try {
      const v = await endpoints.runTodos(pid);
      setData({ todos: { ...store.data.todos, [pid]: v } });
      toast(`Queued ${plural(n, 'task')} in your order`);
    } catch (e) { fail(e); }
  }).then(again),
  setProjectSetting: el => {
    const k = el.dataset.k, raw = el.dataset.v;
    patchProjectSettings(ui().projectId, { [k]: raw === 'true' ? true : raw === 'false' ? false : Number(raw) });
  },
  removeAllowed: el => {
    const pid = ui().projectId, s = store.data.projectSettings[pid];
    if (!s) return;
    patchProjectSettings(pid, { allow_commands: s.allow_commands.filter((_, i) => i !== Number(el.dataset.i)) });
  },
  budgetStep: el => {
    const pid = ui().projectId, s = store.data.projectSettings[pid];
    if (s) patchProjectSettings(pid, { budget_usd: Math.max(0, s.budget_usd + Number(el.dataset.d)) });
  },
  activityFilter: el => set({ activityFilter: el.dataset.f }),
  fillReply: el => { putText('reply', el.dataset.text); set({ focus: 'replyInput' }); },
  quickReply: el => sendReply(el.dataset.text),
  sendReply: () => sendReply(),
  stopTask: el => more.stop(Number(el.dataset.id)).then(() => { toast('Stopped · moving to the next task'); again(); }, fail),
  palProject: el => { const p = ui().palette; if (p) { p.projectId = Number(el.dataset.id); p.error = ''; requestRender(); } },
  palModel: el => { const p = ui().palette; if (p) { p.model = el.dataset.id; p.error = ''; requestRender(); } },
  palPriority: el => { const p = ui().palette; if (p) { p.priority = Number(el.dataset.v); requestRender(); } },
  palWhen: el => { const p = ui().palette; if (p) { p.when = el.dataset.k; p.error = ''; requestRender(); } },
  palRecipe: el => {
    const p = ui().palette, r = (store.data.recipes || []).find(x => x.id === Number(el.dataset.id));
    if (!p || !r) return;
    if (p.when === 'now') runRecipe(r.id, r.target === 'all' ? null : p.projectId);
    else putText('palette', r.prompt);
  },
  palRecent: el => {
    const p = ui().palette, t = taskById(Number(el.dataset.id));
    if (!p || !t) return;
    p.projectId = t.project_id;
    putText('palette', t.prompt);
  },
  submitPalette: () => submitPalette(),
  toggleHoldLow: () => {
    const on = !(store.data.settings && store.data.settings.hold_low_when_busy);
    setData({ settings: { ...store.data.settings, hold_low_when_busy: on } });
    more.setSettings({ hold_low_when_busy: on }).then(v => setData({ settings: v }), fail);
  },
  testPush: () => more.testPush().then(r => toast(r.sent ? 'Test push sent · check your phone' : "Couldn't reach ntfy"), fail),
};

const SUBMITS = {
  saveKey: () => {
    const v = (document.getElementById('keyInput') || {}).value || '';
    if (!v.trim()) return;
    ls(KEY_STORAGE, v.trim());
    set({ gateError: '' });
    boot();
  },
  addProject: () => addProjectSubmit(),
  addTodo: () => {
    const pid = ui().projectId, text = ui().todoText.trim();
    if (!text) return;
    set({ todoText: '' });
    const el = document.getElementById('todoInput'); if (el) el.value = '';
    endpoints.addTodo(pid, text).then(t => setData({ todos: { ...store.data.todos, [pid]: [...(store.data.todos[pid] || []), t] } }), fail);
  },
  denyWithNote: el => deny(Number(el.dataset.id), ui().denyNote.trim()),
};

root.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el || !root.contains(el) || el.disabled) return;
  const fn = ACTIONS[el.dataset.action];
  if (!fn) return;
  if (el.tagName !== 'INPUT') e.preventDefault();
  fn(el, e);
});
root.addEventListener('submit', e => {
  const form = e.target.closest('[data-submit]');
  if (!form) return;
  e.preventDefault();
  const fn = SUBMITS[form.dataset.submit];
  if (fn) fn(form, e);
});
root.addEventListener('input', e => {
  const el = e.target;
  const path = el.dataset && el.dataset.input;
  if (!path) return;
  const v = el.value;
  if (path === 'ask') { ui().ask.text = v; if (ui().ask.result && ui().ask.result.error) ui().ask.result = null; }
  else if (path === 'reply') store.ui.reply = v;
  else if (path === 'denyNote') store.ui.denyNote = v;
  else if (path === 'todoText') store.ui.todoText = v;
  else if (path === 'palette.text') { if (ui().palette) { ui().palette.text = v; ui().palette.error = ''; } }
  else if (path === 'onboard.name') { ui().onboard.name = v; ui().onboard.error = ''; }
  else if (path === 'onboard.path') { ui().onboard.path = v; ui().onboard.error = ''; }
  else if (path === 'pl.q') store.ui.projectsList = { ...ui().projectsList, q: v };
  requestRender();
});
root.addEventListener('keydown', e => {
  const el = e.target;
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && el.dataset && el.dataset.enter) {
    e.preventDefault();
    ACTIONS[el.dataset.enter](el, e);
  }
});

// ---------------------------------------------------------------- keyboard shortcuts
window.addEventListener('keydown', e => {
  if (!apiKey()) return;
  const u = ui();
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea';
  const mod = e.metaKey || e.ctrlKey;
  if (!u.booted) return;
  if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); u.sheet === 'palette' ? set({ sheet: null, palette: null }) : openPalette(); return; }
  if (e.key === 'Escape') {
    if (u.ask.menu || u.projectsList.menu) return ACTIONS.closeMenus();
    if (u.sheet) return set({ sheet: null, palette: null });
    if (u.pane) return set({ pane: null });
    return;
  }
  if (typing || mod || e.altKey) return;
  const first = pendingApprovals()[0];
  if (e.key === 'n') { e.preventDefault(); openPalette(); }
  else if (e.key === 'a' && first) approve(first.id);
  else if (e.key === 'd' && first) deny(first.id);
  else if (e.key === '?') set({ sheet: 'keys' });
  else if (['1', '2', '3', '4'].includes(e.key)) navigate(VIEWS[Number(e.key) - 1]);
});

// ---------------------------------------------------------------- clock + toast expiry
setInterval(() => {
  const u = ui(), d = store.data;
  if (u.toast && u.toast.until < Date.now()) set({ toast: null });
  const live = d.tasks.some(t => t.status === 'running' || t.status === 'rate_limited') || pendingApprovals(d).length;
  if (live || !u.booted) requestRender();
}, 1000);
setInterval(requestRender, 30000);   // "2m ago" labels

// ---------------------------------------------------------------- boot
let stopPolling = null;
function onPollError(e) {
  if (/X-API-Key/i.test(e.message)) {
    try { localStorage.removeItem(KEY_STORAGE); } catch {}
    if (stopPolling) { stopPolling(); stopPolling = null; }
    set({ gateError: "That key didn't work. Check API_KEY in the launchd plist." });
    return;
  }
  setData({ error: e.message });
}

function boot() {
  // SwiftBar links here as /dashboard?status=<status>: open Activity on that filter.
  const status = new URLSearchParams(location.search).get('status');
  if (status && ['done', 'error', 'needs_review', 'cancelled'].includes(status)) {
    store.ui.activityFilter = status;
    history.replaceState(null, '', location.pathname + '#/activity');
  }
  if (!apiKey()) { requestRender(); return; }
  if (!stopPolling) stopPolling = startPolling(onPollError);
  applyRoute();
  requestRender();
}

boot();
