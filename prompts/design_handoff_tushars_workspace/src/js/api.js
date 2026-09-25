// API client + 5-second polling store. Framework-free; views subscribe and re-render.
// Auth: X-API-Key from localStorage (existing dashboard behaviour).

const KEY_STORAGE = 'apiKey';           // keep whatever key name dashboard.html already uses
export const apiKey = () => localStorage.getItem(KEY_STORAGE) || '';

export async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { 'X-API-Key': apiKey(), ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// ---------------------------------------------------------------- endpoints used by the design
export const endpoints = {
  // existing
  projects:        ()            => api('/projects'),
  addProject:      (name, path)  => api('/projects', { method: 'POST', body: { name, path } }),
  setActive:       (id, active)  => api(`/projects/${id}`, { method: 'PATCH', body: { active } }),
  queueStatus:     ()            => api('/queue/status'),
  tasks:           (q = 'limit=200') => api(`/tasks?${q}`),
  projectTasks:    id            => api(`/projects/${id}/tasks`),
  addTask:         (pid, prompt, priority = 0, thread_id = null) => api(`/projects/${pid}/tasks`, { method: 'POST', body: { prompt, priority, thread_id } }),
  cancel:          id            => api(`/tasks/${id}/cancel`, { method: 'POST' }),
  approvals:       ()            => api('/approvals'),
  decide:          (id, approved) => api(`/approvals/${id}/decide`, { method: 'POST', body: { approved } }),
  todos:           pid           => api(`/projects/${pid}/todos`),
  addTodo:         (pid, text)   => api(`/projects/${pid}/todos`, { method: 'POST', body: { text } }),
  updateTodo:      (id, patch)   => api(`/todos/${id}`, { method: 'PATCH', body: patch }),
  deleteTodo:      id            => api(`/todos/${id}`, { method: 'DELETE' }),
  reorderTodos:    (pid, ids)    => api(`/projects/${pid}/todos/reorder`, { method: 'POST', body: { ids } }),
  runTodos:        pid           => api(`/projects/${pid}/todos/run`, { method: 'POST' }),
  // new (Octopus routing) — see README › Backend
  send:            (text, project_id, model = 'auto', images) => api('/octopus', { method: 'POST', body: { text, project_id, model, images } }),
  models:          ()            => api('/octopus/models'),
  usage:           ()            => api('/octopus/usage'),
  // new (optional) — see README › Backend gaps
  polish:          text          => api('/octopus/polish', { method: 'POST', body: { text } }).then(r => r.text),
};

// ---------------------------------------------------------------- store
// One plain object, replaced on every poll. UI state (view, density, open menus…) lives in store.ui.
const listeners = new Set();
export const store = {
  data: { projects: [], tasks: [], approvals: [], status: null, models: [], usage: {}, todos: {} },
  ui: {
    booted: false,            // splash dismissed (double-tap)
    view: 'home',             // home | projects | project | recipes | activity
    projectId: null,
    projectTab: 'work',       // work | activity | recipes | settings
    density: localStorage.getItem('octopus-compact') === '1' ? 'compact' : 'comfortable',
    skeleton: true,           // true for ~520ms after every view change
    pane: null,               // { type: 'chat' | 'approval', taskId }
    sheet: null,              // 'palette' | 'usage' | 'addProject' | 'keys'
    toast: null,              // { text, action?, onAction?, until }
    projectsList: { quick: 'all', sort: 'activity', q: '', searchOpen: false, menu: null, showPaused: true, onlyActive: false, open: {} },
    ask: { text: '', model: 'auto', projectId: null /* null = General */, images: [], sending: false, result: null, menu: null },
    activityFilter: 'all',
  },
  set(patch) { Object.assign(this.ui, typeof patch === 'function' ? patch(this.ui) : patch); emit(); },
};
export const subscribe = fn => (listeners.add(fn), () => listeners.delete(fn));
const emit = () => listeners.forEach(fn => fn(store));

export async function refresh() {
  const [projects, tasks, approvals, status, models, usage] = await Promise.all([
    endpoints.projects(), endpoints.tasks('limit=200'), endpoints.approvals(), endpoints.queueStatus(),
    endpoints.models().catch(() => []), endpoints.usage().catch(() => ({})),
  ]);
  store.data = { ...store.data, projects, tasks, approvals, status, models, usage };
  emit();
}

export function startPolling(ms = 5000) {
  refresh().catch(console.error);
  const id = setInterval(() => { if (!document.hidden) refresh().catch(console.error); }, ms);
  return () => clearInterval(id);
}

// ---------------------------------------------------------------- derived selectors
const approvalByTask = d => new Map(d.approvals.filter(a => a.status === 'pending').map(a => [a.task_id, a]));
export const uiStatus = (t, d = store.data) => (approvalByTask(d).has(t.id) ? 'approval' : t.status);
export const roots = d => d.tasks.filter(t => !t.thread_id);
export const running = d => d.tasks.filter(t => t.status === 'running')
  .sort((a, b) => (a.lane === 'agent' ? 0 : 1) - (b.lane === 'agent' ? 0 : 1));      // Claude Code first
export const needsYou = d => {
  const ap = approvalByTask(d);
  return [
    ...d.tasks.filter(t => ap.has(t.id)).map(t => ({ kind: 'approval', task: t, approval: ap.get(t.id) })),
    ...d.tasks.filter(t => t.status === 'error' && !t.dismissed).map(t => ({ kind: 'error', task: t })),
    ...d.tasks.filter(t => t.status === 'needs_review').map(t => ({ kind: 'review', task: t })),
  ];
};
export const queued = d => d.tasks.filter(t => t.status === 'queued')
  .sort((a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at));
export const rollups = d => {
  const c = d.status?.counts || {};
  return [
    { key: 'total', label: 'Total tasks', value: Object.values(c).reduce((a, b) => a + b, 0), color: 'var(--text)' },
    { key: 'queue', label: 'In queue',    value: c.queued || 0, color: 'var(--queued)' },
    { key: 'done',  label: 'Succeeded',   value: c.done || 0,   color: 'var(--ok)' },
    { key: 'error', label: 'Failed',      value: c.error || 0,  color: 'var(--err)' },
  ];
};
