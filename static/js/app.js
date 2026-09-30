// Tushar's Workspace - dashboard entry point: API-key gate, hash router, render loop,
// actions (event delegation on data-action / data-submit / data-input), gestures,
// keyboard shortcuts and polling.

import { store, subscribe, apiKey, endpoints } from './api.js';
import { more, refresh, startPolling, loadThread, loadProject, loadPersonal, loadTimeline, loadBriefing, loadMessages, loadMessagesSoon, loadSearchSoon, dismissTask, projectById, taskById, generalProject,
  realProjects, pendingApprovals, queuedTasks, isGeneral, rootId, projectStatus } from './data.js';
import { derive, commandOf } from './derive.js';
import { isAssistant } from './views/assistant-card.js';
import { morph } from './morph.js';
import { polish } from './polish.js';
import { pageSwipe, swipeDecision, dragReorder, onDoubleTap, autoGrow } from './gestures.js';
import { plural } from './format.js';
import { modelLabel, clock } from './ui.js';
import { NotificationCenter } from './notifications.js';
import { octopusSVG } from './octopus.js';
import { topbar, sidebar, tabbar, homeSkeleton, listSkeleton } from './views/shell.js';
import { splash, gate, markSeen } from './views/splash.js';
import { home } from './views/home.js';
import { projects } from './views/projects.js';
import { project } from './views/project.js';
import { recipes } from './views/recipes.js';
import { activity } from './views/activity.js';
import { messages } from './views/messages.js';
import { personal, PERSONAL_TABS } from './views/personal.js';
import { chatPane, emptyPane } from './views/chat.js';
import { offlinePopup } from './views/offline.js';
import { voicePane } from './views/voice-pane.js';
import { approvalPane } from './views/approval.js';
import { sheet, WHENS } from './views/sheets.js';
import { quickButtons } from './views/quick.js';
import { FEATURES } from './features.js';
import { searchItems } from './views/search.js';
import { routing } from './views/routing.js';
import { providers } from './views/providers.js';
import { settings } from './views/settings.js';
import { voiceSettings } from './views/voice-settings.js';
import { usage as tokenUsage } from './views/usage.js';
import { octopusActions, octoInput, octoSent } from './octopus-ui.js';
import { octoApi, startOctopusData, onStoreChange } from './octopus-data.js';
import { calendarPage, briefingPage } from './views/calendar.js';
import { timelineActions, timelineInput, calState } from './timeline-ui.js';
import { setRedraw, addImages, removeImage, clearSent, readyIds, imageProblem, MAX_IMAGES } from './attach.js';

const root = document.getElementById('app');
const KEY_STORAGE = 'cq_api_key';
const LAST_PROJECT = 'octopus-last-project';
const SKELETON_MS = 520;
const PANE_WIDTH = 'octopus-pane-w';
const SIDEBAR_PREF = 'octopus-sidebar';
const SIDEBAR_WIDE = 1280;   // from here the sidebar sits beside the page; below (down to 900) it's a slide-over drawer
const PANE_DEFAULT = 460, PANE_MIN = 360, MAIN_MIN = 380, SIDEBAR = 250;
const VIEWS = ['home', 'projects', 'recipes', 'activity', 'personal', 'messages', 'routing', 'providers', 'settings', 'voice', 'usage', 'calendar', 'briefing'];
const PROJECT_TABS = ['work', 'timeline', 'notes', 'activity', 'recipes', 'settings'];

// ---------------------------------------------------------------- UI state (on top of api.js's store.ui)
const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch {} return null; };
Object.assign(store.ui, {
  booted: false, splashLeaving: false, bootDate: new Date(), bootRand: Math.random(), gateError: '',
  palette: null, onboard: { name: '', path: '', category: 'work', error: '' }, todoText: '', todoOpen: {}, todoEdit: null, quickEdit: false, quickNew: null, noteText: '', noteKind: 'guideline', noteEdit: null, reply: '', replyMode: null, replyModel: null, replyMenu: false, denyNote: '',
  scope: ['all', 'work', 'personal'].includes(ls('octopus-scope')) ? ls('octopus-scope') : 'all',
  busy: {}, polishing: null, focus: null, tabs: [], drafts: {}, lastProjectId: Number(ls(LAST_PROJECT)) || null,
  personalTab: 'email', sidebarPref: ls(SIDEBAR_PREF) !== '0', navDrawer: false,
  restart: null,   // { pid, waitingOn } while Restart Octopus is in progress
  offlineHidden: null, offlineChecking: false,   // the offline popup: dismissed for this outage (its offlineSince), Try now in flight
  // Header search. open = results showing (on a phone: the header is the search box); sel = highlighted result;
  // chats = the server's message matches for chatsFor.
  search: { q: '', open: false, sel: 0, chats: [], chatsFor: '' },
  // Project calendar (views/calendar.js): the month on screen, the picked day and project; the add / edit sheet.
  cal: calState(), evEdit: null, tlShowDone: false,
});
store.ui.ask.touched = false;
const ui = () => store.ui;

let interacting = false, pending = false, frame = 0;
function requestRender() {
  if (interacting) { pending = true; return; }
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; render(); });
}
subscribe(() => { checkAutoOpen(); onStoreChange(); requestRender(); });
const set = patch => { Object.assign(store.ui, patch); requestRender(); };
const setData = patch => { store.data = { ...store.data, ...patch }; requestRender(); };

// ---------------------------------------------------------------- toasts + notification centre (notifications.js)
// notify.toast(text, { action, actionLabel, taskId }): top-right toast; taskId makes tapping it open that task.
let notify = null;
const toast = (text, opts) => { if (notify) notify.toast(text, opts); };
const fail = e => toast(e && e.message ? e.message : String(e));

// Notifications point at a chat's root task. What counts is the chat as a whole: waiting
// on an approval anywhere in it, otherwise its newest message's status. null = gone.
function chatStatus(id) {
  const d = store.data, rid = rootOf(id);
  const msgs = d.tasks.filter(t => t.id === rid || t.thread_id === rid);
  if (!msgs.length) return null;
  if (pendingApprovals(d).some(a => msgs.some(t => t.id === a.task_id))) return 'approval';
  return msgs.reduce((a, b) => (b.id > a.id ? b : a)).status;
}
function openFromNotification(id) {
  const rid = rootOf(id);
  const a = pendingApprovals().find(x => rootOf(x.task_id) === rid);
  a ? openApproval(a.id) : openChat(rid);
}

// The bell and .notif-root are rendered empty and keyed, so morph keeps the same elements;
// only a brand-new .app (after the key gate) needs a fresh NotificationCenter.
function mountNotifications() {
  const nroot = root.querySelector('.notif-root'), bell = document.getElementById('bell');
  if (!nroot || !bell || (notify && notify.root === nroot && notify.bell === bell)) return;
  notify = new NotificationCenter({
    root: nroot, bell,
    statusOf: chatStatus,
    onOpenTask: openFromNotification,
    emptyArt: () => octopusSVG('sleep'),
    modelLabel: t => modelLabel(t.model_id),
    // Approvals: show what it wants to run and let it be decided right in the toast / panel row.
    describeApproval: a => (isAssistant(a) ? '' : '$ ') + commandOf(a),
    approvalPending: id => pendingApprovals().some(a => a.id === id),
    onDecide: (id, ok) => (ok ? approve(id) : deny(id)),
  });
  feedNotifications();
}

// Derive notifications from each fresh snapshot. Tasks carry a short generated title;
// show that rather than the whole prompt.
let notifySrc = null, notifyData = null, notifyProjects = null;
function feedNotifications() {
  const d = store.data;
  if (!notify || !d.loaded) return;
  if (d !== notifySrc) { notifySrc = d; notifyData = { ...d, tasks: d.tasks.map(t => (t.title ? { ...t, prompt: t.title } : t)) }; }
  // A different set of visible projects (the Work / Personal switch) brings older tasks in all
  // at once: take them in as a fresh, silent baseline instead of toasting each one as new.
  const ids = d.projects.map(p => p.id).join();
  if (ids !== notifyProjects) { notifyProjects = ids; notify.prevTasks = null; }
  notify.ingest(notifyData);
}
subscribe(feedNotifications);

const isDesk = () => window.innerWidth >= 900;
const isWideDesk = () => window.innerWidth >= SIDEBAR_WIDE;
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

// ---------------------------------------------------------------- router (#/home, #/projects, #/project/12, #/recipes, #/activity, #/personal/email)
let skeletonTimer = 0;
function parseHash() {
  const [v, id, sub] = location.hash.replace(/^#\/?/, '').split('/');
  // #/project/3/timeline (a deadline push) opens that tab.
  if (v === 'project' && id && !Number.isNaN(Number(id))) return { view: 'project', projectId: Number(id), projectTab: PROJECT_TABS.includes(sub) ? sub : null };
  if (v === 'personal') return { view: 'personal', projectId: ui().projectId, personalTab: PERSONAL_TABS.some(([k]) => k === id) ? id : 'email' };
  return { view: VIEWS.includes(v) ? v : 'home', projectId: ui().projectId };
}
function applyRoute() {
  const r = parseHash();
  const u = ui();
  const tabOnly = r.view === 'project' && r.view === u.view && r.projectId === u.projectId && r.projectTab && r.projectTab !== u.projectTab;
  const changed = r.view !== u.view || (r.view === 'project' && r.projectId !== u.projectId) || (r.view === 'personal' && r.personalTab !== u.personalTab);
  if (r.projectTab) history.replaceState(null, '', `#/project/${r.projectId}`);     // the tab is one-shot; the address stays the project's
  if (tabOnly) return set({ projectTab: r.projectTab });
  if (!changed) return requestRender();
  const patch = { view: r.view, projectId: r.projectId, skeleton: true };
  if (r.view === 'personal') patch.personalTab = r.personalTab;
  if (r.view === 'project' && (r.projectId !== u.projectId || r.projectTab)) patch.projectTab = r.projectTab || 'work';
  if (!isDesk()) { patch.pane = null; patch.tabs = []; }
  if (r.view === 'project') {
    rememberProject(r.projectId);
    if (isDesk() && !u.ask.touched) u.ask.projectId = r.projectId;
    loadProject(r.projectId).catch(() => {});
  }
  set(patch);
  if (r.view === 'personal') loadPersonal().catch(() => {});
  if (r.view === 'messages') loadMessages().catch(() => {});
  if (['home', 'calendar', 'project', 'briefing'].includes(r.view)) loadTimeline().catch(() => {});
  if (r.view === 'briefing') loadBriefing().catch(() => {});
  const main = document.getElementById('main');
  if (main) main.scrollTop = 0;
  clearTimeout(skeletonTimer);
  skeletonTimer = setTimeout(() => set({ skeleton: false }), SKELETON_MS);
}
function navigate(view, projectId, tab) {
  // for 'personal' the second argument is the tab (email | calendar | reminders | connections); for 'project', tab opens one of its tabs
  const hash = view === 'project' ? `#/project/${projectId}${tab ? '/' + tab : ''}` : view === 'personal' ? `#/personal/${projectId || ui().personalTab || 'email'}` : `#/${view}`;
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
  const desk = isDesk(), navOverlay = !isWideDesk();
  // Wide screens: open unless the user collapsed it (remembered). Narrower desktops and phones: closed until opened, then it floats over the page.
  const navOpen = navOverlay ? u.navDrawer : u.sidebarPref;
  return { d, ui: u, w, now, desk, navOverlay, navOpen, compact: u.density === 'compact', project: u.view === 'project' ? projectById(u.projectId) : null };
}

function page(c) {
  if (!c.d.loaded || c.ui.skeleton || !c.ui.booted) return c.ui.view === 'home' ? homeSkeleton(c) : listSkeleton(c);
  switch (c.ui.view) {
    case 'projects': return projects(c);
    case 'project': return project(c);
    case 'recipes': return recipes(c);
    case 'activity': return activity(c);
    case 'messages': return messages(c);
    case 'personal': return personal(c);
    case 'routing': return routing(c);
    case 'providers': return providers(c);
    case 'settings': return settings(c);
    case 'voice': return voiceSettings(c);
    case 'usage': return tokenUsage(c);
    case 'calendar': return calendarPage(c);
    case 'briefing': return briefingPage(c);
    default: return home(c);
  }
}

function pane(c) {
  const p = c.ui.pane;
  if (!p) return '';
  if (p.type === 'empty') return emptyPane(c);
  if (p.type === 'voice') return voicePane(c);
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
  return `<div class="app${c.desk ? ' app--desk' : ''}${c.ui.pane ? ' app--pane' : ''}" data-density="${c.ui.density}" data-key="app">
    ${sidebar(c)}
    <main class="app__main" id="main" data-key="main">
      ${topbar(c)}
      <div class="pager" data-pager data-live-style>${page(c)}</div>
    </main>
    ${pane(c)}
    ${c.desk && c.ui.pane ? '<div class="pane-resizer" data-key="pane-resizer" title="Drag to resize · double-click to reset" aria-hidden="true"></div>' : ''}
    ${c.desk ? '' : tabbar(c)}
    ${sheet(c)}
    ${offlinePopup(c)}
    <div class="notif-root" data-key="notif-root"></div>
    ${c.ui.booted ? '' : splash(c)}
  </div>`;
}

const bound = new WeakSet();
let lastPaneKey = null, searchScroll = false;

function render() {
  const c = context();
  document.documentElement.classList.toggle('is-desk', c.desk);
  const paneEl = root.querySelector('.pane');
  const nearBottom = paneEl ? paneEl.scrollHeight - paneEl.scrollTop - paneEl.clientHeight < 80 : false;
  morph(root, appHTML(c));
  bindGestures(c);
  mountNotifications();

  const newPane = root.querySelector('.pane');
  const key = newPane ? newPane.getAttribute('data-key') : null;
  const follows = key && (key.startsWith('pane-chat') || key === 'pane-voice');     // panes that read top to bottom, newest last
  if (newPane && key !== lastPaneKey) newPane.scrollTop = follows ? newPane.scrollHeight : 0;
  else if (newPane && nearBottom && follows) newPane.scrollTop = newPane.scrollHeight;
  lastPaneKey = key;

  if (c.ui.focus) {
    const el = document.getElementById(c.ui.focus);
    if (el) { el.focus(); if (el.setSelectionRange) el.setSelectionRange(el.value.length, el.value.length); }
    store.ui.focus = null;
  }
  if (searchScroll) {
    searchScroll = false;
    const sel = root.querySelector('.srow.is-sel');
    if (sel) sel.scrollIntoView({ block: 'nearest' });
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
window.addEventListener('resize', () => { applyPaneWidth(paneWidth); if (isWideDesk()) store.ui.navDrawer = false; requestRender(); });

// ---------------------------------------------------------------- resizable side pane (desktop)
// The width lives in the --pane-w custom property on <html>, so dragging never re-renders.
// At 1280px+ the pane sits beside the page, so leave the page MAIN_MIN; below that it
// floats over the page and may cover most of it.
const clampPane = w => Math.round(Math.max(PANE_MIN, Math.min(w, window.innerWidth - SIDEBAR - (window.innerWidth >= 1280 ? MAIN_MIN : 60))));
let paneWidth = Number(ls(PANE_WIDTH)) || PANE_DEFAULT;
function applyPaneWidth(w) { document.documentElement.style.setProperty('--pane-w', clampPane(w) + 'px'); }
applyPaneWidth(paneWidth);
document.addEventListener('pointerdown', e => {
  const handle = e.target.closest('.pane-resizer');
  if (!handle || e.button) return;
  e.preventDefault();
  handle.setPointerCapture(e.pointerId);
  document.documentElement.classList.add('is-resizing');
  const move = ev => { paneWidth = clampPane(window.innerWidth - ev.clientX); applyPaneWidth(paneWidth); };
  const up = () => {
    handle.removeEventListener('pointermove', move);
    document.documentElement.classList.remove('is-resizing');
    ls(PANE_WIDTH, String(paneWidth));
  };
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up, { once: true });
  handle.addEventListener('pointercancel', up, { once: true });
});
document.addEventListener('dblclick', e => {
  if (!e.target.closest('.pane-resizer')) return;
  paneWidth = PANE_DEFAULT; applyPaneWidth(paneWidth); ls(PANE_WIDTH, String(paneWidth));
});

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

// Chats open as tabs in the desktop side pane: `tabs` are conversation (root task) ids, `pane` is what's showing.
// Opening another chat adds a tab instead of replacing the current one, and each tab keeps its half-typed reply.
const rootOf = id => { const t = taskById(id); return t ? (t.thread_id || t.id) : id; };
const paneRoot = p => (p && p.taskId != null ? rootOf(p.taskId) : null);

function showPane(pane, extra) {
  const u = ui(), from = paneRoot(u.pane), to = paneRoot(pane);
  const drafts = { ...u.drafts };
  if (from != null) drafts[from] = u.reply;
  let tabs = isDesk() ? u.tabs : [];
  if (to != null && !tabs.includes(to)) tabs = [...tabs, to];
  set({ pane, tabs, drafts, reply: to != null ? drafts[to] || '' : '', replyMode: null, replyModel: null, replyMenu: false, sheet: null, ...extra });
}

function openChat(taskId) {
  const rid = rootOf(taskId);
  showPane({ type: 'chat', taskId: rid });
  loadThread(rid).catch(() => {});
}

function closeTab(id) {
  const u = ui(), tabs = u.tabs.filter(x => x !== id);
  const drafts = { ...u.drafts };
  delete drafts[id];
  if (paneRoot(u.pane) !== id) return set({ tabs, drafts });
  // Closing the chat you're looking at: the other tabs stay up with none picked (pane 'empty'), rather than jumping into one.
  set({ pane: tabs.length ? { type: 'empty' } : null, tabs, drafts, reply: '', replyMode: null, replyModel: null, replyMenu: false });
}

function closePane() {
  if ((ui().pane || {}).type === 'voice') ACTIONS.voiceStop();     // closing the voice pane ends the voice chat
  if (!isDesk()) return set({ pane: null, tabs: [], drafts: {} });
  const id = paneRoot(ui().pane);
  id != null ? closeTab(id) : set({ pane: null });
}

// A task just sent from the Ask box / New task sheet: if the worker picks it up right away, open its chat
// so the answer streams in front of you. Gives up after AUTO_OPEN_MS (it's waiting in line) or as soon as
// you open something else, so it never yanks the view later.
const AUTO_OPEN_MS = 45000;
let autoOpen = null, autoOpenPoll = 0;
function armAutoOpen(taskId) {
  autoOpen = { id: taskId, until: Date.now() + AUTO_OPEN_MS, pane: ui().pane };
  autoOpenPoll = 0;
  checkAutoOpen();
}
function checkAutoOpen() {
  if (!autoOpen) return;
  const u = ui(), t = taskById(autoOpen.id);
  if (Date.now() > autoOpen.until || u.pane !== autoOpen.pane) { autoOpen = null; return; }
  if (!t) return;
  if (t.status === 'queued') {
    if (Date.now() - autoOpenPoll > 2500) { autoOpenPoll = Date.now(); again(); }
    return;
  }
  autoOpen = null;
  if (t.status !== 'cancelled') openChat(t.id);
}
function openApproval(approvalId) {
  const a = pendingApprovals().find(x => x.id === approvalId);
  showPane({ type: 'approval', approvalId, taskId: a ? a.task_id : null }, { denyNote: '' });
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
      const res = await more.decide(id, true, always ? { always: true } : {});
      const ctx = store.data.approvalCtx[id];
      const p = projectById(a.project_id);
      if (res && res.assistant_result) {
        // Personal-assistant action: say what really happened (it ran, or it failed / expired / was blocked).
        const r = res.assistant_result;
        toast(r.ok ? (r.text || 'Done').split('\n')[0] : (r.text || 'Not done'), { taskId: a.task_id });
        loadPersonal().catch(() => {});
      } else toast(always && ctx ? `Approved · ${ctx.allow_entry} is always allowed in ${p ? p.name : 'this project'}` : `Approved · resuming ${a.task_title || a.task_prompt}`, { taskId: a.task_id });
      afterDecision(id);
    } catch (e) { fail(e); }
  });
  again();
}
async function deny(id, note) {
  const a = pendingApprovals().find(x => x.id === id);
  try {
    const res = await more.decide(id, false, note ? { note } : {});
    toast(res && res.assistant_result ? 'Cancelled · nothing was done' : 'Denied · Octopus will wrap up without it', { taskId: a && a.task_id });
    if (res && res.assistant_result) loadPersonal().catch(() => {});
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

// The pictures being attached to a prompt: 'ask' (home box), 'palette' (New task sheet) or 'reply' (the open chat).
// Reply drafts are kept per chat, so a picture never follows you into another tab.
function imageDraft(target) {
  if (target === 'ask') return ui().ask.images;
  if (target === 'palette') { const p = ui().palette; if (!p) return null; return p.images || (p.images = []); }
  if (target === 'reply') {
    const id = paneRoot(ui().pane);
    if (id == null) return null;
    const all = ui().replyImages || (store.ui.replyImages = {});
    return all[id] || (all[id] = []);
  }
  return null;
}
function attachTo(target, files) {
  const list = imageDraft(target);
  if (!list || !files || !files.length) return;
  const left = addImages(list, files);
  if (left) toast(`Only pictures can be attached, up to ${MAX_IMAGES} per message`);
}

async function runAgain(id) {
  const t = taskById(id);
  if (!t) return;
  try {
    if (t.lane === 'api' || t.lane === 'assistant') {
      const r = await more.send(t.prompt, t.project_id, t.lane === 'assistant' ? 'auto' : t.model_id, t.priority, null, t.images);
      toast('Asked again · ' + modelLabel(t.model_id), { taskId: r.task.id });
    } else {
      const r = await endpoints.addTask(t.project_id, t.prompt, t.priority, null, null, t.images);
      toast(`Queued again · #${r.position || '?'}`, { taskId: r.id });
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
  set({ sheet: 'palette', palette: { text: '', model: 'auto', priority: s ? s.default_priority : 0, when: 'now', sending: false, error: '', projectId: pid, images: [], ...pre }, focus: 'palInput' });
}

async function submitPalette() {
  const pal = ui().palette;
  if (!pal || !pal.text.trim() || pal.sending) return;
  const images = pal.images || [];
  const problem = imageProblem(images) || (pal.when !== 'now' && images.length ? "Scheduled tasks can't carry pictures yet - send it now, or remove them" : '');
  if (problem) { pal.error = problem; return requestRender(); }
  pal.sending = true; pal.error = ''; requestRender();
  const text = pal.text.trim();
  const p = projectById(pal.projectId);
  let sentId = null;
  try {
    if (pal.when !== 'now') {
      const w = WHENS.find(x => x.k === pal.when);
      const date = w.tomorrow ? (() => { const d = new Date(Date.now() + 86400000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; })() : undefined;
      await more.addSchedule({ prompt: text, project_id: pal.projectId, cadence: w.cadence, time: w.time, date, model: pal.model, priority: pal.priority });
      toast('Scheduled · ' + w.l);
    } else {
      const r = await octoApi.send(text, { project_id: p && !p.is_general ? p.id : null, model: pal.model, priority: pal.priority,
        images: images.length ? readyIds(images) : undefined });
      clearSent(images);
      const out = octoSent(r);
      if (out.plan) { rememberProject(pal.projectId); return; }   // the plan card took over the sheet
      if (!r.task || r.status === 'done') { toast(out.answer || 'Done'); }
      else {
        sentId = r.task.id;
        toast(r.decision.lane === 'api'
          ? `→ ${r.decision.model_label} · answering · #${r.task.id}`
          : `Queued · ${p ? p.name : 'General'} · ${r.decision.model_label} · #${r.task.position || '?'} in line`, { taskId: r.task.id });
      }
    }
    rememberProject(pal.projectId);
    set({ sheet: null, palette: null });
    if (sentId != null) armAutoOpen(sentId);
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
  const problem = imageProblem(a.images);
  if (problem) return toast(problem);
  Object.assign(a, { sending: true, result: null, menu: null }); requestRender();
  try {
    const gen = generalProject();
    const pid = a.projectId != null && !(gen && a.projectId === gen.id) ? a.projectId : null;
    const agent = ui().octo.agent;
    const r = await octoApi.send(text, { project_id: pid, model: a.model, priority: pid ? (store.data.projectSettings[pid] || {}).default_priority || 0 : 0,
      permission_mode: a.mode || undefined, agent: agent === 'auto' ? undefined : agent,
      images: a.images.length ? readyIds(a.images) : undefined });
    clearSent(a.images);
    const out = octoSent(r);    // queued -> the line under the box; a plan to review -> the plan card; an instant command -> its answer
    Object.assign(a, { sending: false, text: '', result: out.result });
    const ta = document.getElementById('askInput');
    if (ta) { ta.value = ''; ta.style.height = ''; }
    if (pid) rememberProject(pid);
    if (out.taskId != null) armAutoOpen(out.taskId);
  } catch (e) {
    Object.assign(a, { sending: false, result: { error: e.message } });
  }
  requestRender();
  again();
}

const ACTIVE_STATUSES = ['queued', 'running', 'rate_limited'];

async function sendReply(text) {
  const p = ui().pane;
  const body = (text ?? ui().reply).trim();
  if (!p || !body) return;
  const th = store.data.threads[p.taskId];
  const msgs = th ? th.tasks : store.data.tasks.filter(t => (t.thread_id || t.id) === p.taskId);
  const rootTask = msgs.find(t => t.id === p.taskId) || taskById(p.taskId);
  if (!rootTask) return;
  const latest = taskById(msgs[msgs.length - 1].id) || msgs[msgs.length - 1];
  // A chat can change models between messages, so what it's on now is its latest message's, not the first's.
  const onModel = latest.model_id || rootTask.model_id;
  const model = ui().replyModel && ui().replyModel !== onModel ? ui().replyModel : null;
  const lane = model ? (store.data.models.find(m => m.id === model) || {}).lane : latest.lane;
  if (lane === 'api' && latest.status === 'running') return toast('Wait for this answer to finish');
  // The new model is handed the conversation so far, so the last answer has to exist first.
  if (model && ACTIVE_STATUSES.includes(latest.status)) return toast('Wait for the last message to finish before switching models');
  // Attached pictures go with what was typed, not with a one-tap quick reply.
  const pics = text == null ? imageDraft('reply') || [] : [];
  const problem = imageProblem(pics);
  if (problem) return toast(problem);
  await busy('reply', async () => {
    try {
      const r = await endpoints.addTask(rootTask.project_id, body, rootTask.priority || 0, rootTask.id, lane === 'agent' ? ui().replyMode : null,
        pics.length ? readyIds(pics) : null, model);
      clearSent(pics);
      // Replies queue like everything else (next in line). If Claude Code is busy
      // elsewhere, offer to start this one alongside it.
      const busyElsewhere = store.data.tasks.some(t => t.status === 'running' && t.lane === 'agent' && rootId(t) !== rootTask.id);
      if (lane === 'agent' && busyElsewhere && r) {
        toast('Reply queued · next in line', { action: () => runNow(r.id), actionLabel: 'Proceed now', taskId: r.id });
      }
      set({ replyMode: null, replyModel: null, replyMenu: false }); // the next reply starts from the mode and model this one ran under
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
// ---------------------------------------------------------------- queue items: edit / proceed now
// Everything queues by default; any queued item (new task or reply) can be edited, moved,
// or started right away alongside whatever is running.
function openEdit(id) {
  const t = taskById(id);
  if (!t) return;
  const q = queuedTasks();
  const i = q.findIndex(x => x.id === id);
  const after = i > 0 ? String(q[i - 1].id) : 'first';
  const isReply = !!t.thread_id;
  const hasReplies = store.data.tasks.some(x => x.thread_id === t.id);
  set({ sheet: 'editTask', edit: {
    id, isReply, canMoveProject: !isReply && !hasReplies, title: t.title || '', prompt: t.prompt, priority: t.priority,
    projectId: t.project_id, model: t.model_id || 'auto', mode: t.permission_mode || 'acceptEdits', after, origAfter: after, saving: false, error: '',
  } });
}

// Saves what changed; true if it all went through.
async function saveEdit({ close = true } = {}) {
  const e = ui().edit, t = e && taskById(e.id);
  if (!e || !t || e.saving) return false;
  const body = {};
  if (e.prompt.trim() && e.prompt.trim() !== t.prompt) body.prompt = e.prompt.trim();
  if (!e.isReply && e.title.trim() && e.title.trim() !== (t.title || '')) body.title = e.title.trim();
  if (e.priority !== t.priority) body.priority = e.priority;
  if (e.canMoveProject && e.projectId !== t.project_id) body.project_id = e.projectId;
  if (!e.isReply && e.model !== (t.model_id || 'auto')) body.model = e.model;
  if (e.mode !== (t.permission_mode || 'acceptEdits')) body.permission_mode = e.mode;
  set({ edit: { ...e, saving: true, error: '' } });
  try {
    if (Object.keys(body).length) await more.editTask(e.id, body);
    if (e.after !== e.origAfter) await more.moveTask(e.id, e.after === 'first' ? null : Number(e.after));
    if (close) { set({ sheet: null, edit: null }); toast('Saved', { taskId: e.id }); }
    else set({ edit: { ...ui().edit, saving: false } });
    again();
    return true;
  } catch (err) {
    if (ui().edit) set({ edit: { ...ui().edit, saving: false, error: err.message || String(err) } });
    return false;
  }
}

// Pause / play a task: flip the flag on screen straight away, then let the
// server confirm (or put it back if it refuses).
async function holdTask(id, paused) {
  const mark = v => setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, paused: v ? 1 : 0 } : t)) });
  mark(paused);
  try {
    await (paused ? more.pause(id) : more.play(id));
    toast(paused ? 'Paused' : 'Playing', { taskId: id });
  } catch (e) { mark(!paused); fail(e); }
  again();
}

// Pause all / Resume all / Dismiss all on a queue list: flip every task on
// screen straight away, ask the server for each, then reload the truth.
const idsOf = el => (el.dataset.ids || '').split(',').filter(Boolean).map(Number);
async function bulkTasks(ids, kind) {
  if (!ids.length) return;
  const call = { pause: more.pause, play: more.play, cancel: endpoints.cancel }[kind];
  const patch = { pause: { paused: 1 }, play: { paused: 0 }, cancel: { status: 'cancelled' } }[kind];
  setData({ tasks: store.data.tasks.map(t => (ids.includes(t.id) ? { ...t, ...patch } : t)) });
  const results = await Promise.allSettled(ids.map(id => call(id)));
  const ok = ids.filter((id, i) => results[i].status === 'fulfilled');
  const n = ok.length, word = n === 1 ? 'task' : 'tasks';
  const msg = { pause: `Paused ${n} ${word}`, play: `Resumed ${n} ${word}`, cancel: `Cancelled ${n} ${word}` }[kind];
  const failed = ids.length - n;
  const undo = kind === 'cancel' && n ? { action: () => Promise.allSettled(ok.map(id => more.requeue(id, false))).then(again), actionLabel: 'Undo' } : {};
  toast(failed ? `${msg} · ${failed} couldn't be changed` : msg, undo);
  again();
}

async function runNow(id) {
  const t = taskById(id);
  if (!t) return;
  // From the edit sheet: keep whatever was just changed.
  if (ui().sheet === 'editTask' && ui().edit && ui().edit.id === id && !(await saveEdit({ close: false }))) return;
  const p = projectById(t.project_id);
  const sameProject = t.lane === 'agent' && store.data.tasks.some(x => x.status === 'running' && x.lane === 'agent'
    && x.project_id === t.project_id && rootId(x) !== rootId(t));
  if (sameProject && !confirm(`Something else is already running in ${p ? p.name : 'this project'}. Running both at once means two Claude Code sessions changing the same files. Start anyway?`)) return;
  try {
    const r = await more.runNow(id);
    if (ui().sheet === 'editTask') set({ sheet: null, edit: null });
    toast(r.mode === 'now' ? 'Starting now' : r.mode === 'after_chat' ? 'Starts as soon as the earlier message in this chat finishes' : 'Moved to the front', { taskId: id });
    again();
  } catch (e) { fail(e); }
}

// Summarise button: one catch-up on the whole chat, in a sheet. The backend caches it
// until the chat changes, so reopening is instant.
async function openSummary(taskId, refresh = false) {
  const t = taskById(taskId) || (store.data.threads[taskId] || { tasks: [] }).tasks[0];
  set({ sheet: 'summary', summary: { taskId, title: t ? (t.title || t.prompt) : '', loading: true } });
  try {
    const r = await more.summarise(taskId, refresh);
    if ((ui().summary || {}).taskId !== taskId) return;  // closed or switched meanwhile
    set({ summary: { taskId, title: r.title || ui().summary.title, text: r.summary, cached: r.cached, modelId: r.model_id, loading: false } });
  } catch (e) {
    if ((ui().summary || {}).taskId !== taskId) return;
    set({ summary: { ...ui().summary, loading: false, error: e.message || String(e) } });
  }
}

async function doPolish(target) {
  const t = TEXT_TARGETS[target];
  if (!t || !t.get().trim() || ui().polishing) return;
  set({ polishing: target });
  const r = await polish(t.get(), text => endpoints.polish(text));
  set({ polishing: null });
  if (r.changed) {
    putText(target, r.text);
    toast('Text polished', { action: () => putText(target, r.original), actionLabel: 'Undo' });
  } else toast('Already reads well');
}

async function addProjectSubmit() {
  const ob = ui().onboard;
  const name = ob.name.trim(), path = ob.path.trim(), category = ob.category || 'work';
  if (!name || !path) { ob.error = 'Give it a name and the folder path on this Mac.'; return requestRender(); }
  await busy('addProject', async () => {
    try {
      const p = await endpoints.addProject(name, path, category);
      set({ onboard: { name: '', path: '', category, error: '' }, sheet: null });
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

function patchNotes(pid, fn) {
  setData({ notes: { ...store.data.notes, [pid]: (store.data.notes[pid] || []).map(fn) } });
}

function patchTodos(pid, fn) {
  const todos = store.data.todos[pid] || [];
  setData({ todos: { ...store.data.todos, [pid]: todos.map(fn) } });
}

const num = v => (v === '' || v == null ? null : Number(v));

// ---------------------------------------------------------------- header search (results: views/search.js)
const searchInput = () => document.getElementById('topSearch');
const searchIdle = { q: '', open: false, sel: 0, chats: [], chatsFor: '' };
const focusSearch = () => set({ search: { ...ui().search, open: true }, focus: 'topSearch' });

// Closes the results and drops the keyboard. On a phone the header stops being the search box, so the words go too.
function closeSearch() {
  const s = ui().search, el = searchInput();
  if (el) el.blur();
  set({ search: { ...s, open: false, sel: 0, ...(isDesk() ? {} : { q: '' }) } });
}

function pickSearch(kind, id) {
  const el = searchInput();
  if (el) el.blur();
  set({ search: searchIdle });
  loadSearchSoon();   // nothing typed: cancels a request still on its way
  if (kind === 'view') navigate(id);
  else if (kind === 'personal') navigate('personal', id);
  else if (kind === 'project') navigate('project', Number(id));
  else if (kind === 'chat') openChat(Number(id));
  else if (kind === 'recipe' || kind === 'schedule') navigate('recipes');
  else if (id === 'palette') openPalette();
  else ACTIONS.openSheet({ dataset: { sheet: id } });
}

function searchKey(e) {
  if (e.isComposing) return;
  const s = ui().search;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const n = searchItems(context()).length;
    if (!n) return;
    searchScroll = true;
    set({ search: { ...s, open: true, sel: (s.sel + (e.key === 'ArrowDown' ? 1 : n - 1)) % n } });
  } else if (e.key === 'Enter') {
    const it = searchItems(context())[s.sel];
    if (it) { e.preventDefault(); pickSearch(it.kind, it.id); }
  }
}

const ACTIONS = {
  searchOpen: focusSearch,
  searchClose: closeSearch,
  searchClear: () => {
    const el = searchInput();
    if (el) el.value = '';
    set({ search: { ...ui().search, q: '', sel: 0, chats: [], chatsFor: '' }, focus: 'topSearch' });
    loadSearchSoon();
  },
  searchPick: el => pickSearch(el.dataset.kind, el.dataset.id),
  go: el => {
    const v = el.dataset.view;
    if (el.dataset.filter) store.ui.activityFilter = el.dataset.filter;
    if (el.dataset.quick) store.ui.projectsList = { ...ui().projectsList, quick: el.dataset.quick, open: {} };
    if (el.closest('.sheet')) store.ui.sheet = null;           // a link inside a sheet (Usage → Token usage) leaves it
    navigate(v);
  },
  goBack: () => navigate('projects'),
  goPersonal: el => navigate('personal', el.dataset.tab),
  assistConnect: el => busy('conn-' + el.dataset.id, async () => {
    try {
      const r = await more.assistConnect(el.dataset.id);
      // Google's consent page opens in a new tab; the redirect lands back on this dashboard.
      window.open(r.auth_url, '_blank', 'noopener');
      toast('Finish signing in on the new tab · nothing is saved until you approve there');
    } catch (e) { fail(e); }
  }),
  assistDisconnect: el => busy('conn-' + el.dataset.id, async () => {
    try { await more.assistDisconnect(el.dataset.id); toast('Disconnected · the sign-in was revoked and forgotten'); await loadPersonal(); } catch (e) { fail(e); }
  }),
  setPolicy: el => {
    const cap = el.dataset.cap, policy = el.dataset.policy, st = store.data.personal.status;
    if (st) setData({ personal: { ...store.data.personal, status: { ...st, policies: st.policies.map(p => (p.capability === cap ? { ...p, policy } : p)) } } });
    more.setPolicy(cap, policy).then(() => loadPersonal(), e => { fail(e); loadPersonal().catch(() => {}); });
  },
  cancelReminder: el => more.cancelReminder(Number(el.dataset.id)).then(() => { toast('Reminder cancelled'); return loadPersonal(); }, fail),
  openProject: el => navigate('project', Number(el.dataset.id)),
  openPalette: el => openPalette(el.dataset.pid ? { projectId: Number(el.dataset.pid) } : {}),
  openSheet: el => set({ sheet: el.dataset.sheet, onboard: { ...ui().onboard, error: '', ...(el.dataset.sheet === 'addProject' && ui().scope !== 'all' ? { category: ui().scope } : {}) }, focus: el.dataset.sheet === 'addProject' ? 'addName' : null }),
  closeSheet: () => set({ sheet: null, palette: null, summary: null, edit: null, todoEdit: null, noteEdit: null, quickNew: null, evEdit: null }),
  scrimClose: (el, e) => { if (e.target === el) set({ sheet: null, palette: null, summary: null, edit: null, todoEdit: null, noteEdit: null, quickNew: null, evEdit: null }); },
  editTask: el => openEdit(Number(el.dataset.id)),
  editProject: el => { const e = ui().edit; if (e) set({ edit: { ...e, projectId: Number(el.dataset.id), error: '' } }); },
  editMode: el => { const e = ui().edit; if (e) set({ edit: { ...e, mode: el.dataset.id, error: '' } }); },
  editModel: el => { const e = ui().edit; if (e) set({ edit: { ...e, model: el.dataset.id, error: '' } }); },
  editPriority: el => { const e = ui().edit; if (e) set({ edit: { ...e, priority: Number(el.dataset.v) } }); },
  saveEdit: () => saveEdit(),
  runNow: el => runNow(Number(el.dataset.id)),
  summarise: el => openSummary(Number(el.dataset.id), el.dataset.refresh === '1'),
  toggleSidebar: () => {
    if (isWideDesk()) { const open = !ui().sidebarPref; ls(SIDEBAR_PREF, open ? '1' : '0'); set({ sidebarPref: open }); }
    else set({ navDrawer: !ui().navDrawer });
  },
  toggleHideDone: () => {
    const hide = !ui().hideDone;
    ls('octopus-hide-done', hide ? '1' : '0');
    set({ hideDone: hide });
  },
  toggleDensity: () => {
    const compact = ui().density !== 'compact';
    ls('octopus-compact', compact ? '1' : '0');
    set({ density: compact ? 'compact' : 'comfortable' });
    toast(compact ? 'Compact layout on' : 'Comfortable layout on');
  },
  openChat: el => openChat(Number(el.dataset.id)),
  openApproval: el => openApproval(Number(el.dataset.id)),
  openApprovalForTask: el => { const a = pendingApprovals().find(x => x.task_id === Number(el.dataset.id)); a ? openApproval(a.id) : openChat(Number(el.dataset.id)); },
  closePane,
  switchTab: el => openChat(Number(el.dataset.id)),
  closeTab: el => closeTab(Number(el.dataset.id)),
  approve: el => approve(Number(el.dataset.id)),
  approveAlways: el => approve(Number(el.dataset.id), true),
  deny: el => deny(Number(el.dataset.id)),
  retry: el => busy('rt' + el.dataset.id, async () => {
    try { await more.requeue(Number(el.dataset.id), true); toast('Retry queued at the front', { taskId: Number(el.dataset.id) }); } catch (e) { fail(e); }
  }).then(again),
  dismiss: el => {
    const id = Number(el.dataset.id);
    dismissTask(id);
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, dismissed: true } : t)) });
  },
  reviewed: el => more.reviewed(Number(el.dataset.id)).then(() => { toast('Marked as reviewed', { taskId: Number(el.dataset.id) }); again(); }, fail),
  runAgain: el => runAgain(Number(el.dataset.id)),
  toTop: el => {
    const id = Number(el.dataset.id);
    const min = Math.min(0, ...queuedTasks().map(t => t.queue_pos ?? 0));
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, queue_pos: min - 1 } : t)) });
    more.toTop(id).then(() => { toast('Moved to the top', { taskId: id }); again(); }, fail);
  },
  cancelQueued: el => {
    const id = Number(el.dataset.id);
    if (ui().sheet === 'editTask') set({ sheet: null, edit: null });
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, status: 'cancelled' } : t)) });
    endpoints.cancel(id).then(() => {
      toast('Cancelled', { action: () => more.requeue(id, false).then(again, fail), actionLabel: 'Undo', taskId: id });
      again();
    }, fail);
  },
  pauseAll: el => bulkTasks(idsOf(el), 'pause'),
  resumeAll: el => bulkTasks(idsOf(el), 'play'),
  dismissAll: el => {
    const ids = idsOf(el);
    if (ids.length && confirm(`Cancel ${ids.length === 1 ? 'the queued task' : `all ${ids.length} queued tasks`}?`)) bulkTasks(ids, 'cancel');
  },
  toggleSleep: () => {
    const sm = store.data.settings && store.data.settings.sleep_mode;
    const asleep = !!(sm && sm.until && new Date(sm.until).getTime() > Date.now());
    (asleep ? more.endSleep() : more.startSleep()).then(v => {
      setData({ settings: v });
      toast(asleep ? 'Welcome back' : `Sleep mode on until ${clock(v.sleep_mode.until)}. I'll keep the queue going.`);
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
  askMode: el => { Object.assign(ui().ask, { mode: el.dataset.id === 'acceptEdits' ? null : el.dataset.id, menu: null, result: null }); requestRender(); },
  replyMenu: () => set({ replyMenu: ui().replyMenu === 'mode' ? false : 'mode' }),
  replyMode: el => set({ replyMode: el.dataset.id, replyMenu: false }),
  replyModelMenu: () => set({ replyMenu: ui().replyMenu === 'model' ? false : 'model' }),
  replyModel: el => { if (el.getAttribute('aria-disabled') !== 'true') set({ replyModel: el.dataset.id, replyMenu: false }); },
  closeMenus: () => { ui().ask.menu = null; store.ui.replyMenu = false; store.ui.projectsList = { ...ui().projectsList, menu: null }; store.ui.messages = { ...ui().messages, menu: false }; requestRender(); },
  // Message centre: every change to the search or filters asks the server again.
  msgStatus: el => { set({ messages: { ...ui().messages, status: el.dataset.status, limit: 40 } }); loadMessages().catch(fail); },
  msgProject: el => { const id = el.dataset.id; set({ messages: { ...ui().messages, project: id === 'all' ? 'all' : Number(id), limit: 40, menu: false } }); loadMessages().catch(fail); },
  msgLane: el => { set({ messages: { ...ui().messages, lane: el.dataset.lane, limit: 40, menu: false } }); loadMessages().catch(fail); },
  msgMenu: () => set({ messages: { ...ui().messages, menu: !ui().messages.menu } }),
  msgMore: () => { set({ messages: { ...ui().messages, limit: ui().messages.limit + 40 } }); loadMessages().catch(fail); },
  msgSearchClear: () => { set({ messages: { ...ui().messages, q: '', limit: 40 }, focus: 'msgSearch' }); loadMessages().catch(fail); },
  msgClear: () => { set({ messages: { ...ui().messages, q: '', status: 'all', project: 'all', lane: 'all', limit: 40, menu: false } }); loadMessages().catch(fail); },
  submitAsk: () => submitAsk(),
  polish: el => doPolish(el.dataset.target),
  plQuick: el => set({ projectsList: { ...ui().projectsList, quick: el.dataset.q, open: {} } }),
  plMenu: el => { const pl = ui().projectsList; set({ projectsList: { ...pl, menu: pl.menu === el.dataset.menu ? null : el.dataset.menu } }); },
  plSort: el => set({ projectsList: { ...ui().projectsList, sort: el.dataset.sort, menu: null } }),
  plFilter: el => { const pl = ui().projectsList, k = el.dataset.f; set({ projectsList: { ...pl, [k]: !pl[k] } }); },
  plSearch: () => { const pl = ui().projectsList; set({ projectsList: { ...pl, searchOpen: !pl.searchOpen, q: pl.searchOpen ? '' : pl.q, menu: null }, focus: pl.searchOpen ? null : 'plSearch' }); },
  plSearchClose: () => set({ projectsList: { ...ui().projectsList, q: '', searchOpen: false } }),
  plToggle: el => { const pl = ui().projectsList; set({ projectsList: { ...pl, open: { ...pl.open, [el.dataset.id]: el.dataset.open !== 'true' } } }); },
  setScope: el => {
    const scope = el.dataset.scope;
    ls('octopus-scope', scope);
    const p = ui().view === 'project' ? projectById(ui().projectId) : null;
    set({ scope, projectsList: { ...ui().projectsList, open: {} } });
    if (p && scope !== 'all' && !p.is_general && p.category !== scope) navigate('projects');
    again();
  },
  setNewCategory: el => set({ onboard: { ...ui().onboard, category: el.dataset.scope } }),
  setCategory: el => {
    const p = projectById(ui().projectId);
    if (!p || p.is_general) return;
    const category = el.dataset.scope;
    if (category === p.category) return;
    endpoints.setCategory(p.id, category).then(() => {
      toast(`Moved ${p.name} to ${category === 'work' ? 'Work' : 'Personal'}`);
      if (ui().scope !== 'all' && ui().scope !== category) navigate('projects');
      again();
    }, fail);
  },
  setProjectStatus: el => {
    const p = projectById(ui().projectId);
    if (!p || p.is_general || el.dataset.status === p.status) return;
    const status = el.dataset.status;
    setData({ projects: store.data.projects.map(x => (x.id === p.id ? { ...x, status } : x)) });
    endpoints.setProjectStatus(p.id, status).then(() => { toast(`${p.name} · ${projectStatus({ status })[1]}`); again(); }, e => { fail(e); again(); });
  },
  plClear: () => set({ projectsList: { ...ui().projectsList, quick: 'all', q: '', searchOpen: false, showPaused: true, onlyActive: false, open: {} } }),
  projectTab: el => { set({ projectTab: el.dataset.tab }); const m = document.getElementById('main'); const tabs = root.querySelector('.tabs'); if (m && tabs && m.scrollTop > tabs.offsetTop) m.scrollTop = Math.max(0, tabs.offsetTop - 80); },
  togglePause: () => {
    const p = projectById(ui().projectId);
    if (!p) return;
    setData({ projects: store.data.projects.map(x => (x.id === p.id ? { ...x, active: p.active ? 0 : 1 } : x)) });
    endpoints.setActive(p.id, !p.active).then(() => { toast(p.active ? `Paused ${p.name} · its tasks will be skipped` : `Resumed ${p.name}`); again(); }, fail);
  },
  suggestImprovements: async () => {
    const p = projectById(ui().projectId);
    if (!p || p.is_general) return;
    await busy('suggest', async () => {
      try {
        const t = await endpoints.suggest(p.id);
        toast(`Reviewing ${p.name} · #${t.position || '?'} in line`, { taskId: t.id });
      } catch (e) { fail(e); }
    });
    again();
  },
  todoCheck: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const t = (store.data.todos[pid] || []).find(x => x.id === id);
    if (!t) return;
    patchTodos(pid, x => (x.id === id ? { ...x, checked: t.checked ? 0 : 1 } : x));
    endpoints.updateTodo(id, { checked: !t.checked }).catch(fail);
  },
  todoToggle: el => { const id = Number(el.dataset.id); set({ todoOpen: { ...ui().todoOpen, [id]: !ui().todoOpen[id] } }); },
  // How a backlog item will run once queued: model / permission_mode / priority.
  todoSet: el => {
    const pid = ui().projectId, id = Number(el.dataset.id), k = el.dataset.k;
    const v = k === 'priority' ? Number(el.dataset.v) : el.dataset.v;
    const field = { model: 'model_id', permission_mode: 'permission_mode', priority: 'priority' }[k];
    const before = (store.data.todos[pid] || []).find(x => x.id === id);
    if (!before) return;
    patchTodos(pid, x => (x.id === id ? { ...x, [field]: v } : x));
    endpoints.updateTodo(id, { [k]: v }).catch(e => { patchTodos(pid, x => (x.id === id ? { ...x, [field]: before[field] } : x)); fail(e); });
  },
  submitTodo: () => SUBMITS.addTodo(),
  submitNote: () => SUBMITS.addNote(),
  setNoteKind: el => set({ noteKind: el.dataset.kind }),
  setNoteEditKind: el => { const e = ui().noteEdit; if (e) set({ noteEdit: { ...e, kind: el.dataset.kind } }); },
  openNoteEdit: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const n = (store.data.notes[pid] || []).find(x => x.id === id);
    if (n) set({ sheet: 'editNote', noteEdit: { id, pid, text: n.text, kind: n.kind, saving: false, error: '' }, focus: 'noteEditInput' });
  },
  saveNoteEdit: async () => {
    const e = ui().noteEdit;
    if (!e || e.saving || !e.text.trim()) return;
    set({ noteEdit: { ...e, saving: true, error: '' } });
    try {
      const v = await endpoints.updateNote(e.id, { text: e.text.trim(), kind: e.kind });
      patchNotes(e.pid, x => (x.id === e.id ? { ...x, ...v } : x));
      set({ sheet: null, noteEdit: null });
      toast('Saved');
    } catch (err) {
      if (ui().noteEdit) set({ noteEdit: { ...ui().noteEdit, saving: false, error: err.message || String(err) } });
    }
  },
  deleteNoteEdit: () => {
    const e = ui().noteEdit;
    if (!e) return;
    setData({ notes: { ...store.data.notes, [e.pid]: (store.data.notes[e.pid] || []).filter(x => x.id !== e.id) } });
    set({ sheet: null, noteEdit: null });
    endpoints.deleteNote(e.id).then(() => toast('Deleted'), fail);
  },
  // Quick buttons (Home): tap fills the Ask box (and opens the picture picker if the button needs one).
  quickUse: el => {
    const b = quickButtons({ d: store.data }).find(x => x.id === el.dataset.id);
    if (!b) return;
    putText('ask', b.prompt);
    set({ focus: 'askInput' });
    if (b.pic && FEATURES.images) { const f = document.querySelector('input[data-attach="ask"]'); if (f && !f.disabled) f.click(); }
  },
  quickEditToggle: () => set({ quickEdit: !ui().quickEdit }),
  quickNew: () => set({ sheet: 'quick', quickNew: { icon: '', label: '', prompt: '', pic: false, saving: false, error: '' }, focus: 'quickLabel' }),
  quickNewPic: () => { const q = ui().quickNew; if (q) set({ quickNew: { ...q, pic: !q.pic } }); },
  quickSave: async () => {
    const q = ui().quickNew;
    if (!q || q.saving || !q.label.trim() || !q.prompt.trim()) return;
    set({ quickNew: { ...q, saving: true, error: '' } });
    const btn = { id: 'q' + Date.now().toString(36), icon: q.icon.trim() || '✨', label: q.label.trim(), prompt: q.prompt.trim(), pic: !!q.pic };
    try {
      const v = await more.setSettings({ quick_buttons: [...quickButtons({ d: store.data }), btn] });
      setData({ settings: v });
      set({ sheet: null, quickNew: null });
      toast('Added ' + btn.label);
    } catch (err) {
      if (ui().quickNew) set({ quickNew: { ...ui().quickNew, saving: false, error: err.message || String(err) } });
    }
  },
  quickRemove: el => {
    const list = quickButtons({ d: store.data });
    const gone = list.find(x => x.id === el.dataset.id);
    if (!gone) return;
    const left = list.filter(x => x !== gone);
    setData({ settings: { ...store.data.settings, quick_buttons: left } });
    if (!left.length) set({ quickEdit: false });
    more.setSettings({ quick_buttons: left }).then(v => { setData({ settings: v }); toast('Removed ' + gone.label); },
      e => { fail(e); more.settings().then(v => setData({ settings: v }), () => {}); });
  },
  openTodoEdit: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const t = (store.data.todos[pid] || []).find(x => x.id === id);
    if (t) set({ sheet: 'editTodo', todoEdit: { id, pid, text: t.text, saving: false, error: '' }, focus: 'todoEditInput' });
  },
  saveTodoEdit: async () => {
    const e = ui().todoEdit;
    if (!e || e.saving || !e.text.trim()) return;
    set({ todoEdit: { ...e, saving: true, error: '' } });
    try {
      const v = await endpoints.updateTodo(e.id, { text: e.text.trim() });
      patchTodos(e.pid, x => (x.id === e.id ? { ...x, ...v } : x));
      set({ sheet: null, todoEdit: null });
      toast('Saved');
    } catch (err) {
      if (ui().todoEdit) set({ todoEdit: { ...ui().todoEdit, saving: false, error: err.message || String(err) } });
    }
  },
  deleteTodoEdit: () => {
    const e = ui().todoEdit;
    if (!e) return;
    setData({ todos: { ...store.data.todos, [e.pid]: (store.data.todos[e.pid] || []).filter(x => x.id !== e.id) } });
    set({ sheet: null, todoEdit: null });
    endpoints.deleteTodo(e.id).then(() => toast('Deleted'), fail);
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
  stopTask: el => more.stop(Number(el.dataset.id)).then(() => { toast('Stopped · moving to the next task', { taskId: Number(el.dataset.id) }); again(); }, fail),
  pauseTask: el => holdTask(Number(el.dataset.id), true),
  playTask: el => holdTask(Number(el.dataset.id), false),
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
  toggleWeeklyPace: () => {
    const on = !(store.data.settings && store.data.settings.notify_weekly_pace !== false);
    setData({ settings: { ...store.data.settings, notify_weekly_pace: on } });
    more.setSettings({ notify_weekly_pace: on }).then(v => setData({ settings: v }), fail);
  },
  testPush: () => more.testPush().then(r => toast(r.sent ? 'Test push sent · check your phone' : "Couldn't reach ActivitySmith"), fail),
  restartOctopus: () => restartOctopus(),
  offlineDismiss: () => set({ offlineHidden: store.data.offlineSince }),
  offlineRetry: () => { set({ offlineChecking: true }); refresh().catch(onPollError).finally(() => set({ offlineChecking: false })); },
};

// Octopus: plans, routing history, providers, settings, voice, task details (octopus-ui.js).
Object.assign(ACTIONS, octopusActions({ set, toast, fail, again, armAutoOpen, requestRender, navigate, openVoicePane: () => showPane({ type: 'voice' }) }));
// Project calendar: timeline items, the Calendar page, the Good morning briefing (timeline-ui.js).
Object.assign(ACTIONS, timelineActions({ set, toast, fail, navigate, openPalette }));

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
    const el = document.getElementById('todoInput'); if (el) { el.value = ''; if (el._grow) el._grow.reset(); }
    endpoints.addTodo(pid, text).then(t => setData({ todos: { ...store.data.todos, [pid]: [...(store.data.todos[pid] || []), t] } }), fail);
  },
  addNote: () => {
    const pid = ui().projectId, text = ui().noteText.trim(), kind = ui().noteKind;
    if (!text) return;
    set({ noteText: '' });
    const el = document.getElementById('noteInput'); if (el) { el.value = ''; if (el._grow) el._grow.reset(); }
    endpoints.addNote(pid, text, kind).then(n => setData({ notes: { ...store.data.notes, [pid]: [...(store.data.notes[pid] || []), n] } }), fail);
  },
  denyWithNote: el => deny(Number(el.dataset.id), ui().denyNote.trim()),
  removeImage: el => { const list = imageDraft(el.dataset.target); if (list) { removeImage(list, Number(el.dataset.img)); requestRender(); } },
};

root.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el || !root.contains(el) || el.disabled) return;
  const fn = ACTIONS[el.dataset.action];
  if (!fn) return;
  if (el.tagName !== 'INPUT') e.preventDefault();
  if (ui().navDrawer && el.dataset.action !== 'toggleSidebar' && el.closest('.sidebar')) store.ui.navDrawer = false;
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
  else if (path === 'todoEdit.text' && ui().todoEdit) { ui().todoEdit.text = v; ui().todoEdit.error = ''; }
  else if (path.startsWith('quickNew.') && ui().quickNew) { ui().quickNew[path.slice(9)] = v; ui().quickNew.error = ''; }
  else if (path === 'noteText') store.ui.noteText = v;
  else if (path === 'noteEdit.text' && ui().noteEdit) { ui().noteEdit.text = v; ui().noteEdit.error = ''; }
  else if (path === 'palette.text') { if (ui().palette) { ui().palette.text = v; ui().palette.error = ''; } }
  else if (path.startsWith('octo.')) octoInput(path, v, { fail });
  else if (path.startsWith('ev.') || path.startsWith('set.')) timelineInput(path, v, { fail });
  else if (path === 'onboard.name') { ui().onboard.name = v; ui().onboard.error = ''; }
  else if (path === 'onboard.path') { ui().onboard.path = v; ui().onboard.error = ''; }
  else if (path === 'pl.q') store.ui.projectsList = { ...ui().projectsList, q: v };
  else if (path === 'msg.q') { store.ui.messages = { ...ui().messages, q: v, limit: 40 }; loadMessagesSoon(); }
  else if (path === 'search') { store.ui.search = { ...ui().search, q: v, open: true, sel: 0 }; loadSearchSoon(); }
  else if (path.startsWith('edit.') && ui().edit) { ui().edit[path.slice(5)] = v; ui().edit.error = ''; }
  requestRender();
});
// Attaching pictures: the 📎 buttons (a hidden file input each) and pasting a picture into a prompt box.
setRedraw(requestRender);
root.addEventListener('change', e => {
  const el = e.target;
  if (!el.dataset || !el.dataset.attach) return;
  attachTo(el.dataset.attach, el.files);
  el.value = '';                 // so picking the same file again still fires
});
const PASTE_TARGETS = { ask: 'ask', reply: 'reply', 'palette.text': 'palette' };
root.addEventListener('paste', e => {
  const target = PASTE_TARGETS[e.target.dataset && e.target.dataset.input];
  const files = e.clipboardData ? [...e.clipboardData.files].filter(f => f.type.startsWith('image/')) : [];
  if (!target || !files.length) return;
  if (!e.clipboardData.getData('text/plain')) e.preventDefault();      // a copied picture, not copied text
  attachTo(target, files);
});
// Focusing the box (click, tab, "/") shows the results; anywhere else closes them.
root.addEventListener('focusin', e => {
  if (e.target.id === 'topSearch' && !ui().search.open) set({ search: { ...ui().search, open: true } });
});
document.addEventListener('pointerdown', e => {
  if (ui().search.open && !e.target.closest('.tsearch')) closeSearch();
}, true);
root.addEventListener('keydown', e => {
  const el = e.target;
  if (el.id === 'topSearch') return searchKey(e);
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
    if (u.search.open) return closeSearch();
    if (notify && notify.isOpen()) return notify.close();
    if (u.ask.menu || u.projectsList.menu || u.replyMenu || u.messages.menu) return ACTIONS.closeMenus();
    if (u.sheet) return set({ sheet: null, palette: null });
    if (u.navDrawer) return set({ navDrawer: false });
    if (u.pane) return closePane();
    return;
  }
  if (typing || mod || e.altKey) return;
  const first = pendingApprovals()[0];
  if (e.key === 'n') { e.preventDefault(); openPalette(); }
  else if (e.key === '/') { e.preventDefault(); focusSearch(); }
  else if (e.key === 'a' && first) approve(first.id);
  else if (e.key === 'd' && first) deny(first.id);
  else if (e.key === '?') set({ sheet: 'keys' });
  else if (e.key === '[' && isDesk()) ACTIONS.toggleSidebar();
  else if (['1', '2', '3', '4', '5', '6'].includes(e.key)) navigate(VIEWS[Number(e.key) - 1]);
  else if (e.key === '7') navigate('calendar');
  else if (e.key === 'g') navigate('briefing');
});

// ---------------------------------------------------------------- clock
setInterval(() => {
  const u = ui(), d = store.data;
  checkAutoOpen();
  const live = d.tasks.some(t => t.status === 'running' || t.status === 'rate_limited') || pendingApprovals(d).length;
  if (live || !u.booted) requestRender();
}, 1000);
setInterval(requestRender, 30000);   // "2m ago" labels

// ---------------------------------------------------------------- restart Octopus (reload the service's code, then this page)
// The service drains first (finishes what's running, starts nothing new), exits, and launchd
// starts it again. A new pid means the new code is up, so reload to pick up its dashboard too.
let restartTimer = 0;
async function restartOctopus() {
  if (ui().restart) return;
  const busyNow = store.data.tasks.filter(t => t.status === 'running').length;
  if (!confirm(busyNow
    ? `Restart Octopus to load the latest code?\n\nIt's working on ${plural(busyNow, 'task')} right now. It finishes ${busyNow > 1 ? 'those' : 'that'} first and starts nothing new meanwhile, then restarts and reloads this page.`
    : 'Restart Octopus to load the latest code?\n\nIt takes a few seconds, then this page reloads.')) return;
  try {
    const before = await more.service();
    const r = await more.restartService();
    set({ restart: { pid: before.pid, waitingOn: r.waiting_on } });
    toast(r.waiting_on.length ? `Restart queued · after ${r.waiting_on.map(id => '#' + id).join(', ')}` : 'Restarting Octopus · back in a few seconds');
    watchRestart();
  } catch (e) { fail(e); }
}
function watchRestart() {
  clearTimeout(restartTimer);
  restartTimer = setTimeout(async () => {
    const r = ui().restart;
    if (!r) return;
    try {
      const s = await more.service();
      if (s.pid !== r.pid) return location.reload();
      if (String(s.waiting_on) !== String(r.waitingOn)) set({ restart: { ...r, waitingOn: s.waiting_on } });
    } catch {}   // down while it restarts
    watchRestart();
  }, 2000);
}
// A restart asked from another device: show it here too, and reload when it's done.
function checkRestarting() {
  more.service().then(s => {
    if (s.restarting && !ui().restart) { set({ restart: { pid: s.pid, waitingOn: s.waiting_on } }); watchRestart(); }
  }, () => {});
}

// ---------------------------------------------------------------- boot
let stopPolling = null;
function onPollError(e) {
  if (/X-API-Key/i.test(e.message)) {
    try { localStorage.removeItem(KEY_STORAGE); } catch {}
    if (stopPolling) { stopPolling(); stopPolling = null; }
    set({ gateError: "That key didn't work. Check API_KEY in the launchd plist." });
    return;
  }
  // Count failures in a row and note when this outage began - the offline popup waits for a second miss.
  const d = store.data;
  setData({ error: e.message, offlineSince: d.offlineSince || new Date().toISOString(), offlineFails: (d.offlineFails || 0) + 1 });
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
  checkRestarting();
  applyRoute();
  requestRender();
}

startOctopusData();
boot();
