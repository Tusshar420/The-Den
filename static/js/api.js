// API client + 5-second polling store. Framework-free; views subscribe and re-render.
// Auth: X-API-Key from localStorage (existing dashboard behaviour).

const KEY_STORAGE = 'cq_api_key';       // same key the pre-redesign dashboard stored, so nobody has to re-enter it
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
  addProject:      (name, path, category = 'work') => api('/projects', { method: 'POST', body: { name, path, category } }),
  // The New project form: everything at once (POST /projects). No path = a CRM-only project.
  createProject:   body          => api('/projects', { method: 'POST', body }),
  describeProject: id            => api(`/projects/${id}/describe`, { method: 'POST' }).then(r => r.text),
  linkFolder:     (id, path)    => api(`/projects/${id}/folder`, { method: 'POST', body: { path } }),
  deleteProject:   (id, confirm) => api(`/projects/${id}`, { method: 'DELETE', body: { confirm } }),
  setCategory:     (id, category) => api(`/projects/${id}`, { method: 'PATCH', body: { category } }),
  setProjectStatus: (id, status) => api(`/projects/${id}`, { method: 'PATCH', body: { status } }),
  setHidden:       (id, hidden)  => api(`/projects/${id}`, { method: 'PATCH', body: { hidden } }),
  setActive:       (id, active)  => api(`/projects/${id}`, { method: 'PATCH', body: { active } }),
  queueStatus:     ()            => api('/queue/status'),
  tasks:           (q = 'limit=200') => api(`/tasks?${q}`),
  projectTasks:    id            => api(`/projects/${id}/tasks`),
  addTask:         (pid, prompt, priority = 0, thread_id = null, permission_mode = null, images = null, model = null, account = null) => api(`/projects/${pid}/tasks`, { method: 'POST', body: { prompt, priority, thread_id, permission_mode, images, model, account } }),
  cancel:          id            => api(`/tasks/${id}/cancel`, { method: 'POST' }),
  suggest:         pid           => api(`/projects/${pid}/suggest`, { method: 'POST' }),
  approvals:       ()            => api('/approvals'),
  decide:          (id, approved) => api(`/approvals/${id}/decide`, { method: 'POST', body: { approved } }),
  todos:           pid           => api(`/projects/${pid}/todos`),
  addTodo:         (pid, text, images, opts = {}) => api(`/projects/${pid}/todos`, { method: 'POST', body: { text, images, ...opts } }),
  setTaskOnlyAccount: (id, only_account) => api(`/tasks/${id}/only-account`, { method: 'PUT', body: { only_account } }),
  updateTodo:      (id, patch)   => api(`/todos/${id}`, { method: 'PATCH', body: patch }),
  deleteTodo:      id            => api(`/todos/${id}`, { method: 'DELETE' }),
  reorderTodos:    (pid, ids)    => api(`/projects/${pid}/todos/reorder`, { method: 'POST', body: { ids } }),
  runTodos:        pid           => api(`/projects/${pid}/todos/run`, { method: 'POST' }),
  notes:           pid           => api(`/projects/${pid}/notes`),
  addNote:         (pid, text, kind, images) => api(`/projects/${pid}/notes`, { method: 'POST', body: { text, kind, images } }),
  updateNote:      (id, patch)   => api(`/notes/${id}`, { method: 'PATCH', body: patch }),
  deleteNote:      id            => api(`/notes/${id}`, { method: 'DELETE' }),
  documents:       pid           => api(`/projects/${pid}/documents`),
  renameDocument:  (id, name)    => api(`/documents/${id}`, { method: 'PATCH', body: { name } }),
  deleteDocument:  id            => api(`/documents/${id}`, { method: 'DELETE' }),
  deleteProjectImage: pid        => api(`/projects/${pid}/image`, { method: 'DELETE' }),
  // new (Octopus routing) — see README › Backend
  send:            (text, project_id, model = 'auto', images) => api('/octopus', { method: 'POST', body: { text, project_id, model, images } }),
  models:          ()            => api('/octopus/models'),
  usage:           ()            => api('/octopus/usage'),
  // new (optional) — see README › Backend gaps
  polish:          (text, kind)  => api('/octopus/polish', { method: 'POST', body: kind ? { text, kind } : { text } }).then(r => r.text),
};

// Documents are raw file bodies (not JSON), and a download needs the API key, so both go through fetch here.
export async function uploadDocument(pid, file) {
  const res = await fetch(`/projects/${pid}/documents?name=${encodeURIComponent(file.name)}`, {
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

// A project's picture: a raw image body, like documents. Resolves to the whole updated record.
export async function uploadProjectImage(pid, blob) {
  const res = await fetch(`/projects/${pid}/image`, { method: 'POST', headers: { 'X-API-Key': apiKey(), 'Content-Type': blob.type || 'application/octet-stream' }, body: blob });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

export async function downloadDocument(doc) {
  const res = await fetch(`/documents/${doc.id}`, { headers: { 'X-API-Key': apiKey() } });
  if (!res.ok) throw new Error(res.status === 404 ? 'That file is gone' : res.statusText);
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement('a'), { href: url, download: doc.name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// ---------------------------------------------------------------- store
// One plain object, replaced on every poll. UI state (view, density, open menus…) lives in store.ui.
const listeners = new Set();
export const store = {
  data: { projects: [], tasks: [], approvals: [], status: null, models: [], usage: {}, todos: {} },
  ui: {
    booted: false,            // splash dismissed (double-tap)
    view: 'home',             // home | projects | project | recipes | activity
    projectId: null,
    projectTab: 'work',       // work | backlog | phases | timeline | notes | activity | recipes | settings
    density: localStorage.getItem('octopus-compact') === '1' ? 'compact' : 'comfortable',
    skeleton: true,           // true for ~520ms after every view change
    pane: null,               // { type: 'chat' | 'approval', taskId }
    pinned: (() => { try { return JSON.parse(localStorage.getItem('octopus-pinned-tabs') || '[]'); } catch { return []; } })(),   // desktop: pinned chat tabs (kept by Close others / Close all)
    linkMenu: null,           // right-click "Open in new tab" menu { url, x, y }
    tabMenu: null,            // desktop: right-click menu on a tab { id, x, y }
    paneFloat: false,   // desktop: chat as a floating window bottom-right instead of docked on the side
    // desktop: which right sidebar is docked open from the edge rail - 'queue' | 'messages' | null
    // sides: every open sidebar, left to right (several can be open at once, drag a header to reorder); side: the one that hosts a docked chat
    sides: (() => {
      const ok = k => k === 'queue' || k === 'messages' || k === 'calendar' || k === 'todos';
      let list;
      try { list = JSON.parse(localStorage.getItem('octopus-sides')); } catch { list = null; }
      if (!Array.isArray(list)) {
        const old = localStorage.getItem('octopus-side') ?? (localStorage.getItem('octopus-queue-open') === '1' && localStorage.getItem('octopus-queue-min') !== '1' ? 'queue' : '');
        list = old ? [old] : [];
      }
      return list.filter((k, i) => ok(k) && list.indexOf(k) === i);
    })(),
    // cols: the same sidebars grouped into columns, left to right; a column with several keys stacks them top to bottom (sides is cols flattened)
    cols: (() => {
      let cols;
      try { cols = JSON.parse(localStorage.getItem('octopus-cols')); } catch { cols = null; }
      return Array.isArray(cols) ? cols : null;   // null: built from sides just below
    })(),
    side: null,               // set just below from sides
    sideTab: 'queue',         // Queue sidebar: In queue | Completed
    sideProject: 'all',       // Queue sidebar project filter: 'all' or a project id
    paneMin: false,           // desktop: chat window minimised to a chip bottom-right
    sheet: null,              // 'palette' | 'usage' | 'addProject' | 'keys'
    // toasts live in NotificationCenter (notifications.js), not in store.ui
    projectsList: { quick: 'all', sort: 'activity', q: '', searchOpen: false, menu: null, showPaused: true, onlyActive: false, open: {} },
    ask: { text: '', model: 'auto', mode: null /* null = Accept edits */, projectId: null /* null = General */, images: [], sending: false, result: null, menu: null },
    activityFilter: 'all',
    homeTab: localStorage.getItem('octopus-home-tab') === 'done' ? 'done' : 'queue',   // Home: In queue | Completed
    // Message centre. status: all | needs | active | done | error. project: 'all' or a project id. lane: all | agent | api | assistant. kind: all | task | message.
    messages: { q: '', status: 'all', project: 'all', lane: 'all', kind: 'all', limit: 40, menu: false, full: false },
  },
  set(patch) { Object.assign(this.ui, typeof patch === 'function' ? patch(this.ui) : patch); emit(); },
};
{
  const u = store.ui, seen = new Set();
  const cols = (Array.isArray(u.cols) ? u.cols : []).map(c => (Array.isArray(c) ? c : []).filter(k => u.sides.includes(k) && !seen.has(k) && seen.add(k))).filter(c => c.length);
  for (const k of u.sides) if (!seen.has(k)) cols.push([k]);
  u.cols = cols; u.sides = cols.flat();
}
store.ui.side = store.ui.sides.find(k => k !== 'calendar' && k !== 'todos') || store.ui.sides[0] || null;
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
// Run order is the backend's queue_pos (priority on insert, then manual reorders).
export const queued = d => d.tasks.filter(t => t.status === 'queued')
  .sort((a, b) => (a.queue_pos ?? 1e12) - (b.queue_pos ?? 1e12) || a.id - b.id);
export const rollups = d => {
  const c = d.status?.counts || {};
  return [
    { key: 'total', label: 'Total tasks', value: Object.values(c).reduce((a, b) => a + b, 0), color: 'var(--text)' },
    { key: 'queue', label: 'In queue',    value: c.queued || 0, color: 'var(--queued)' },
    { key: 'done',  label: 'Succeeded',   value: c.done || 0,   color: 'var(--ok)' },
    { key: 'error', label: 'Failed',      value: c.error || 0,  color: 'var(--err)' },
  ];
};
