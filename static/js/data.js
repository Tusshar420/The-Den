// Everything the views read, on top of api.js: the endpoints the redesign added to
// the backend, one refresh that loads the whole snapshot, and shared selectors.
// api.js stays the handoff's client; this module only extends it.

import { api, endpoints, store } from './api.js';

// ---------------------------------------------------------------- endpoints added for the redesign
export const more = {
  // endpoints.send without priority - the New task sheet and Ask box pass one.
  send:            (text, project_id, model = 'auto', priority = 0, permission_mode = null, images = null) => api('/octopus', { method: 'POST', body: { text, project_id, model, priority, permission_mode, images } }),
  activeTasks:     ()            => api('/tasks?status=queued,running,rate_limited&limit=500'),
  requeue:         (id, front = true) => api(`/tasks/${id}/requeue`, { method: 'POST', body: { front } }),
  toTop:           id            => api(`/tasks/${id}/top`, { method: 'POST' }),
  reorderQueue:    ids           => api('/queue/reorder', { method: 'POST', body: { ids } }),
  stop:            id            => api(`/tasks/${id}/stop`, { method: 'POST' }),
  pause:           id            => api(`/tasks/${id}/pause`, { method: 'POST' }),
  play:            id            => api(`/tasks/${id}/play`, { method: 'POST' }),
  reviewed:        id            => api(`/tasks/${id}/reviewed`, { method: 'POST' }),
  thread:          id            => api(`/tasks/${id}/thread`),
  messages:        params        => api('/messages?' + new URLSearchParams(params)),
  decide:          (id, approved, extra = {}) => api(`/approvals/${id}/decide`, { method: 'POST', body: { approved, ...extra } }),
  approvalContext: id            => api(`/approvals/${id}/context`),
  projectSettings: pid           => api(`/projects/${pid}/settings`),
  setProjectSettings: (pid, patch) => api(`/projects/${pid}/settings`, { method: 'PATCH', body: patch }),
  settings:        ()            => api('/settings'),
  setSettings:     patch         => api('/settings', { method: 'PATCH', body: patch }),
  testPush:        ()            => api('/notify/test', { method: 'POST' }),
  startSleep:      ()            => api('/sleep', { method: 'POST', body: {} }),
  endSleep:        ()            => api('/sleep', { method: 'DELETE' }),
  service:         ()            => api('/service'),
  restartService:  ()            => api('/service/restart', { method: 'POST' }),
  spend:           ()            => api('/usage/summary'),
  summarise:       (id, refresh = false) => api(`/tasks/${id}/summary`, { method: 'POST', body: { refresh } }),
  editTask:        (id, patch)   => api(`/tasks/${id}`, { method: 'PATCH', body: patch }),
  moveTask:        (id, after_id) => api(`/tasks/${id}/move`, { method: 'POST', body: { after_id } }),
  runNow:          id            => api(`/tasks/${id}/run-now`, { method: 'POST' }),
  recipes:         ()            => api('/recipes'),
  runRecipe:       (id, body = {}) => api(`/recipes/${id}/run`, { method: 'POST', body }),
  schedules:       ()            => api('/schedules'),
  addSchedule:     body          => api('/schedules', { method: 'POST', body }),
  setSchedule:     (id, enabled) => api(`/schedules/${id}`, { method: 'PATCH', body: { enabled } }),
  // personal assistant - see README › Personal assistant. Nothing here ever returns a token.
  assistStatus:    ()            => api('/assistant/status'),
  assistEmail:     ()            => api('/assistant/email/overview'),
  assistCalendar:  ()            => api('/assistant/calendar/overview'),
  assistReminders: ()            => api('/assistant/reminders'),
  assistActivity:  (limit = 60)  => api(`/assistant/activity?limit=${limit}`),
  assistConnect:   id            => api(`/assistant/connections/${id}/connect`, { method: 'POST', body: {} }),
  assistDisconnect: id           => api(`/assistant/connections/${id}`, { method: 'DELETE' }),
  setPolicy:       (cap, policy) => api('/assistant/policies', { method: 'PATCH', body: { [cap]: policy } }),
  cancelReminder:  id            => api(`/assistant/reminders/${id}`, { method: 'DELETE' }),
  // project calendar and timelines - see timeline.py
  timeline:        ()            => api('/timeline'),
  addEvent:        (pid, body)   => api(`/projects/${pid}/timeline`, { method: 'POST', body }),
  updateEvent:     (id, patch)   => api(`/timeline/${id}`, { method: 'PATCH', body: patch }),
  deleteEvent:     id            => api(`/timeline/${id}`, { method: 'DELETE' }),
  briefing:        ()            => api('/briefing'),
};

// ---------------------------------------------------------------- dismissed failures (client-side)
const DISMISS_KEY = 'octopus-dismissed';
const readDismissed = () => { try { return new Set(JSON.parse(localStorage.getItem(DISMISS_KEY) || '[]')); } catch { return new Set(); } };
export function dismissTask(id) {
  const s = readDismissed(); s.add(id);
  try { localStorage.setItem(DISMISS_KEY, JSON.stringify([...s].slice(-300))); } catch {}
}

// ---------------------------------------------------------------- refresh
Object.assign(store.data, {
  schedules: [], recipes: [], spend: null, settings: null,
  personal: { status: null, email: null, calendar: null, reminders: [], activity: [] },
  projectSettings: {}, todos: {}, notes: {}, approvalCtx: {}, threads: {}, loaded: false, error: null,
  messages: { chats: [], total: 0, loaded: false },
  timeline: { events: [], health: {}, kinds: {}, today: null, loaded: false }, briefing: null,
});
// The pages that show the project calendar (Home's "Coming up", the Calendar, a project's Timeline tab).
const TIMELINE_VIEWS = new Set(['home', 'calendar', 'project', 'briefing']);

let tick = 0;

// Loads the snapshot. Per-view extras (a project's backlog/settings, the open chat's
// thread) ride along so they stay live too. Emits once, at the end.
export async function refresh() {
  const ui = store.ui;
  const pane = ui.pane;
  const [allProjects, recent, active, allApprovals, status, models, usageRaw, schedules, recipes, spend, settings] = await Promise.all([
    endpoints.projects(), endpoints.tasks('limit=200'), more.activeTasks(), endpoints.approvals(), endpoints.queueStatus(),
    endpoints.models().catch(() => store.data.models), endpoints.usage().catch(() => null),
    more.schedules().catch(() => store.data.schedules), more.recipes().catch(() => store.data.recipes),
    more.spend().catch(() => store.data.spend), more.settings().catch(() => store.data.settings),
  ]);

  // The Work / Personal switch: anything outside the chosen side never reaches the
  // views, so it can't show up in a list, count, approval or the palette. General
  // has no side and stays.
  const scope = ui.scope || 'all';
  const inScope = p => scope === 'all' || p.is_general || p.category === scope;
  const projects = allProjects.filter(inScope);
  const visibleIds = new Set(projects.map(p => p.id));
  const approvals = allApprovals.filter(a => visibleIds.has(a.project_id));

  // /tasks is newest-first and capped; active tasks are fetched separately so an
  // old queued task never falls off the list.
  const byId = new Map();
  for (const t of [...recent, ...active]) if (visibleIds.has(t.project_id)) byId.set(t.id, t);
  const names = new Map(projects.map(p => [p.id, p.name]));
  const dismissed = readDismissed();
  const tasks = [...byId.values()].sort((a, b) => b.id - a.id)
    .map(t => ({ ...t, project_name: t.project_name || names.get(t.project_id) || '?', dismissed: dismissed.has(t.id) }));

  const usage = {};
  for (const r of (usageRaw && usageRaw.by_model) || []) usage[r.key] = r;

  const d = { ...store.data, projects, tasks, approvals, status, models, usage, schedules, recipes, spend, settings, loaded: true, error: null, offlineSince: null, offlineFails: 0 };

  const extras = [];
  if (ui.view === 'project' && ui.projectId != null) {
    const pid = ui.projectId;
    extras.push(endpoints.todos(pid).then(v => { d.todos = { ...d.todos, [pid]: v }; }).catch(() => {}));
    extras.push(endpoints.notes(pid).then(v => { d.notes = { ...d.notes, [pid]: v }; }).catch(() => {}));
    extras.push(more.projectSettings(pid).then(v => { d.projectSettings = { ...d.projectSettings, [pid]: v }; }).catch(() => {}));
  }
  const freshThreads = {};
  if (pane && pane.taskId != null) {
    extras.push(more.thread(pane.taskId).then(v => { freshThreads[pane.taskId] = v; }).catch(() => {}));
  }
  // The Personal page's data is only fetched while it is open (calendar calls Google, so ease off: every 3rd poll).
  if (ui.view === 'personal' && tick % 3 === 0) extras.push(personalInto(d));
  if (TIMELINE_VIEWS.has(ui.view) && (tick % 3 === 0 || !d.timeline.loaded)) {
    extras.push(more.timeline().then(v => { d.timeline = { ...v, loaded: true }; }).catch(() => {}));
  }
  // The briefing may read Google Calendar: once a minute while it is open.
  if (ui.view === 'briefing' && tick % 12 === 0) extras.push(more.briefing().then(v => { d.briefing = v; }).catch(() => {}));
  // Approval context runs git in the project, so refresh it less often than the rest.
  for (const a of approvals) {
    if (!d.approvalCtx[a.id] || tick % 3 === 0) {
      extras.push(more.approvalContext(a.id).then(v => { d.approvalCtx = { ...d.approvalCtx, [a.id]: v }; }).catch(() => {}));
    }
  }
  await Promise.all(extras);
  tick++;
  // The message centre loads itself (loadMessages writes store.data on its own), so keep whatever it has landed meanwhile.
  // Same for chats: a tab opened mid-poll (loadThread) must survive this poll's older copy.
  store.data = { ...d, messages: store.data.messages, octo: store.data.octo, threads: { ...store.data.threads, ...freshThreads } };
  if (ui.view === 'messages') loadMessages().catch(() => {});
  store.set({});
}

// ---------------------------------------------------------------- message centre
let messagesSeq = 0, messagesTimer = 0;

// Fetches the chats for the current search and filters. Only the newest request may land.
export async function loadMessages() {
  clearTimeout(messagesTimer);
  const m = store.ui.messages, seq = ++messagesSeq;
  const params = { q: m.q.trim(), status: m.status, limit: m.limit };
  if (m.lane !== 'all') params.lane = m.lane;
  // A project picked in the menu wins; otherwise the Work / Personal switch decides which projects count.
  if (m.project !== 'all') params.project_ids = String(m.project);
  else if (store.ui.scope !== 'all') params.project_ids = store.data.projects.map(p => p.id).join(',');
  const v = await more.messages(params);
  if (seq !== messagesSeq) return;
  store.data = { ...store.data, messages: { ...v, loaded: true } };
  store.set({});
}

// While typing, wait for a pause before asking again.
export function loadMessagesSoon(ms = 250) {
  clearTimeout(messagesTimer);
  messagesTimer = setTimeout(() => loadMessages().catch(() => {}), ms);
}

// ---------------------------------------------------------------- header search
// Chats whose messages contain the typed words (GET /messages looks inside every message). Waits for a pause
// in typing, and only the newest request may land. Everything else the search finds is matched locally.
let searchSeq = 0, searchTimer = 0;
export function loadSearchSoon(ms = 250) {
  clearTimeout(searchTimer);
  const q = store.ui.search.q.trim(), seq = ++searchSeq;
  if (!q) return;
  searchTimer = setTimeout(async () => {
    const params = { q, limit: 8 };
    if (store.ui.scope !== 'all') params.project_ids = store.data.projects.map(p => p.id).join(',');
    try {
      const v = await more.messages(params);
      if (seq !== searchSeq) return;
      store.ui.search = { ...store.ui.search, chats: v.chats, chatsFor: q };
      store.set({});
    } catch { /* the local matches still show */ }
  }, ms);
}

// What each Personal tab needs. Every tab also needs the status (connections, permissions, push).
const PERSONAL_FETCH = {
  email: [['email', () => more.assistEmail()]],
  calendar: [['calendar', () => more.assistCalendar()]],
  reminders: [['reminders', () => more.assistReminders()]],
  connections: [['activity', () => more.assistActivity()]],
};

async function personalInto(d) {
  const tab = store.ui.personalTab || 'email';
  const wanted = [['status', () => more.assistStatus()], ...(PERSONAL_FETCH[tab] || [])];
  const got = await Promise.all(wanted.map(([, fn]) => fn().catch(() => undefined)));
  const next = { ...(d.personal || {}) };
  wanted.forEach(([key], i) => { if (got[i] !== undefined) next[key] = got[i]; });
  d.personal = next;
}

export async function loadPersonal() {
  const d = { ...store.data };
  await personalInto(d);
  store.data = d;
  store.set({});
}

export async function loadThread(taskId) {
  const v = await more.thread(taskId);
  store.data = { ...store.data, threads: { ...store.data.threads, [taskId]: v } };
  store.set({});
}

export async function loadProject(pid) {
  const [todos, settings, notes] = await Promise.all([endpoints.todos(pid), more.projectSettings(pid), endpoints.notes(pid)]);
  store.data = { ...store.data, todos: { ...store.data.todos, [pid]: todos }, notes: { ...store.data.notes, [pid]: notes }, projectSettings: { ...store.data.projectSettings, [pid]: settings } };
  store.set({});
}

export async function loadTimeline() {
  const v = await more.timeline();
  store.data = { ...store.data, timeline: { ...v, loaded: true } };
  store.set({});
}

export async function loadBriefing() {
  const v = await more.briefing();
  store.data = { ...store.data, briefing: v };
  store.set({});
}

export function startPolling(onError, ms = 5000) {
  const run = () => refresh().catch(onError);
  run();
  const id = setInterval(() => { if (!document.hidden) run(); }, ms);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) run(); });
  return () => clearInterval(id);
}

// ---------------------------------------------------------------- selectors
export const projectById = (id, d = store.data) => d.projects.find(p => p.id === id);
export const taskById = (id, d = store.data) => d.tasks.find(t => t.id === id);
export const isGeneral = p => !!(p && p.is_general);
export const generalProject = (d = store.data) => d.projects.find(p => p.is_general);
export const SCOPES = [['all', 'All'], ['work', 'Work'], ['personal', 'Personal']];

// Where a project stands: [key, label, dot colour]. Set by hand on the project page.
export const PROJECT_STATUSES = [
  ['not_started', 'Not started', 'var(--text-meta)'],
  ['in_progress', 'In progress', 'var(--run)'],
  ['need_info', 'Need info', 'var(--wait)'],
  ['on_hold', 'On hold', 'var(--queued)'],
  ['completed', 'Completed', 'var(--run-soft)'],
];
// What a project note is: [key, label, what it's for]. Same keys as db.NOTE_KINDS.
export const NOTE_KINDS = [
  ['guideline', 'Guideline', 'How to work on this project'],
  ['requirement', 'Requirement', 'What it has to do'],
  ['note', 'Note', 'Anything else worth knowing'],
];
export const projectStatus = p =>PROJECT_STATUSES.find(s => s[0] === p.status) || PROJECT_STATUSES[0];
export const realProjects = (d = store.data) => d.projects.filter(p => !p.is_general);

// Projects in display order: General pinned first, then by id.
export const orderedProjects = (d = store.data) =>
  [...d.projects].sort((a, b) => (b.is_general ? 1 : 0) - (a.is_general ? 1 : 0) || a.id - b.id);

// Round-robin project colours (--p1…--p5), General green - the backend doesn't store one.
export function projectColor(p, d = store.data) {
  if (!p) return 'var(--p-muted)';
  if (p.is_general) return 'var(--p-general)';
  const i = realProjects(d).sort((a, b) => a.id - b.id).findIndex(x => x.id === p.id);
  return `var(--p${(i % 5) + 1})`;
}

export const pendingApprovals = (d = store.data) => d.approvals.filter(a => a.status === 'pending');
export const approvalForTask = (taskId, d = store.data) => pendingApprovals(d).find(a => a.task_id === taskId);

// The run queue in the order the worker takes it (queue_pos), both lanes. Chat replies are
// queue items too - next in line by default (see db._reply_insert_pos), movable like any other.
export const queuedTasks = (d = store.data) => d.tasks.filter(t => t.status === 'queued')
  .sort((a, b) => (a.queue_pos ?? 1e12) - (b.queue_pos ?? 1e12) || a.id - b.id);

export const rootId = t => t.thread_id || t.id;

// One history entry per chat: its root's title, the latest message's status.
export function threadSummaries(d = store.data) {
  const groups = new Map();
  for (const t of d.tasks) {
    const r = rootId(t);
    const g = groups.get(r) || { root: null, latest: null, msgs: [] };
    g.msgs.push(t);
    if (t.id === r) g.root = t;
    if (!g.latest || t.id > g.latest.id) g.latest = t;
    groups.set(r, g);
  }
  return [...groups.values()].filter(g => g.root).map(g => ({
    ...g.latest,
    id: g.root.id,
    latest_id: g.latest.id,
    prompt: g.root.prompt,
    title: g.root.title,
    thread_id: null,
    lane: g.root.lane,
    model_id: g.root.model_id,
    route_reason: g.root.route_reason,
    category: g.root.category,
    priority: g.root.priority,
    cost_usd: g.msgs.some(m => m.cost_usd != null) ? g.msgs.reduce((s, m) => s + (m.cost_usd || 0), 0) : null,
    turns: g.msgs.length,
  }));
}

export const finishedAt = t => t.finished_at || t.started_at || t.created_at;

// Latest Claude usage window (utilization 0–1), or null if unknown/stale.
export function usageWindow(name, d = store.data) {
  const w = d.settings && d.settings.claude_usage && d.settings.claude_usage.windows && d.settings.claude_usage.windows[name];
  if (!w || w.utilization == null) return null;
  const resets = w.resets_at ? new Date(w.resets_at) : null;
  // Past its reset time, the window has started over.
  if (resets && resets.getTime() < Date.now()) return { pct: 0, resets: null, stale: true };
  return { pct: Math.max(0, Math.min(100, Math.round(w.utilization * 100))), resets, stale: false };
}
