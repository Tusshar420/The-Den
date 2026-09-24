// Everything the views read, on top of api.js: the endpoints the redesign added to
// the backend, one refresh that loads the whole snapshot, and shared selectors.
// api.js stays the handoff's client; this module only extends it.

import { api, endpoints, store } from './api.js';

// ---------------------------------------------------------------- endpoints added for the redesign
export const more = {
  // endpoints.send without priority - the New task sheet and Ask box pass one.
  send:            (text, project_id, model = 'auto', priority = 0) => api('/octopus', { method: 'POST', body: { text, project_id, model, priority } }),
  activeTasks:     ()            => api('/tasks?status=queued,running,rate_limited&limit=500'),
  requeue:         (id, front = true) => api(`/tasks/${id}/requeue`, { method: 'POST', body: { front } }),
  toTop:           id            => api(`/tasks/${id}/top`, { method: 'POST' }),
  reorderQueue:    ids           => api('/queue/reorder', { method: 'POST', body: { ids } }),
  stop:            id            => api(`/tasks/${id}/stop`, { method: 'POST' }),
  reviewed:        id            => api(`/tasks/${id}/reviewed`, { method: 'POST' }),
  thread:          id            => api(`/tasks/${id}/thread`),
  decide:          (id, approved, extra = {}) => api(`/approvals/${id}/decide`, { method: 'POST', body: { approved, ...extra } }),
  approvalContext: id            => api(`/approvals/${id}/context`),
  projectSettings: pid           => api(`/projects/${pid}/settings`),
  setProjectSettings: (pid, patch) => api(`/projects/${pid}/settings`, { method: 'PATCH', body: patch }),
  settings:        ()            => api('/settings'),
  setSettings:     patch         => api('/settings', { method: 'PATCH', body: patch }),
  testPush:        ()            => api('/notify/test', { method: 'POST' }),
  spend:           ()            => api('/usage/summary'),
  recipes:         ()            => api('/recipes'),
  runRecipe:       (id, body = {}) => api(`/recipes/${id}/run`, { method: 'POST', body }),
  schedules:       ()            => api('/schedules'),
  addSchedule:     body          => api('/schedules', { method: 'POST', body }),
  setSchedule:     (id, enabled) => api(`/schedules/${id}`, { method: 'PATCH', body: { enabled } }),
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
  projectSettings: {}, todos: {}, approvalCtx: {}, threads: {}, loaded: false, error: null,
});

let tick = 0;

// Loads the snapshot. Per-view extras (a project's backlog/settings, the open chat's
// thread) ride along so they stay live too. Emits once, at the end.
export async function refresh() {
  const ui = store.ui;
  const pane = ui.pane;
  const [projects, recent, active, approvals, status, models, usageRaw, schedules, recipes, spend, settings] = await Promise.all([
    endpoints.projects(), endpoints.tasks('limit=200'), more.activeTasks(), endpoints.approvals(), endpoints.queueStatus(),
    endpoints.models().catch(() => store.data.models), endpoints.usage().catch(() => null),
    more.schedules().catch(() => store.data.schedules), more.recipes().catch(() => store.data.recipes),
    more.spend().catch(() => store.data.spend), more.settings().catch(() => store.data.settings),
  ]);

  // /tasks is newest-first and capped; active tasks are fetched separately so an
  // old queued task never falls off the list.
  const byId = new Map();
  for (const t of [...recent, ...active]) byId.set(t.id, t);
  const names = new Map(projects.map(p => [p.id, p.name]));
  const dismissed = readDismissed();
  const tasks = [...byId.values()].sort((a, b) => b.id - a.id)
    .map(t => ({ ...t, project_name: t.project_name || names.get(t.project_id) || '?', dismissed: dismissed.has(t.id) }));

  const usage = {};
  for (const r of (usageRaw && usageRaw.by_model) || []) usage[r.key] = r;

  const d = { ...store.data, projects, tasks, approvals, status, models, usage, schedules, recipes, spend, settings, loaded: true, error: null };

  const extras = [];
  if (ui.view === 'project' && ui.projectId != null) {
    const pid = ui.projectId;
    extras.push(endpoints.todos(pid).then(v => { d.todos = { ...d.todos, [pid]: v }; }).catch(() => {}));
    extras.push(more.projectSettings(pid).then(v => { d.projectSettings = { ...d.projectSettings, [pid]: v }; }).catch(() => {}));
  }
  if (pane && pane.taskId != null) {
    extras.push(more.thread(pane.taskId).then(v => { d.threads = { ...d.threads, [pane.taskId]: v }; }).catch(() => {}));
  }
  // Approval context runs git in the project, so refresh it less often than the rest.
  for (const a of approvals) {
    if (!d.approvalCtx[a.id] || tick % 3 === 0) {
      extras.push(more.approvalContext(a.id).then(v => { d.approvalCtx = { ...d.approvalCtx, [a.id]: v }; }).catch(() => {}));
    }
  }
  await Promise.all(extras);
  tick++;
  store.data = d;
  store.set({});
}

export async function loadThread(taskId) {
  const v = await more.thread(taskId);
  store.data = { ...store.data, threads: { ...store.data.threads, [taskId]: v } };
  store.set({});
}

export async function loadProject(pid) {
  const [todos, settings] = await Promise.all([endpoints.todos(pid), more.projectSettings(pid)]);
  store.data = { ...store.data, todos: { ...store.data.todos, [pid]: todos }, projectSettings: { ...store.data.projectSettings, [pid]: settings } };
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

// The run queue in the order the worker takes it (queue_pos), both lanes.
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
