// Everything the views read, on top of api.js: the endpoints the redesign added to
// the backend, one refresh that loads the whole snapshot, and shared selectors.
// api.js stays the handoff's client; this module only extends it.

import { api, endpoints, store } from './api.js';

// ---------------------------------------------------------------- endpoints added for the redesign
export const more = {
  // endpoints.send without priority - the New task sheet and Ask box pass one.
  send:            (text, project_id, model = 'auto', priority = 0, permission_mode = null, images = null) => api('/octopus', { method: 'POST', body: { text, project_id, model, priority, permission_mode, images } }),
  activeTasks:     ()            => api('/tasks?status=queued,running,rate_limited&limit=500'),
  setPriority:     (id, priority) => api(`/tasks/${id}`, { method: 'PATCH', body: { priority } }),
  requeue:         (id, front = true, prompt) => api(`/tasks/${id}/requeue`, { method: 'POST', body: prompt != null ? { front, prompt } : { front } }),
  // recycle bin: Delete moves a queue item there, Restore / Undo puts it back where it was
  deleteTask:      id            => api(`/tasks/${id}`, { method: 'DELETE' }),
  hiddenTasks:     ()            => api('/tasks?hidden=true&limit=500'),
  setHidden:       (ids, hidden) => api('/tasks/hidden', { method: 'POST', body: { ids, hidden } }),
  restoreTask:     id            => api(`/tasks/${id}/restore`, { method: 'POST' }),
  // a chat that isn't under way (finished, or still in line), all its messages, to another project
  transferTask:    (id, project_id) => api(`/tasks/${id}/transfer`, { method: 'POST', body: { project_id } }),
  // notes pad: free-form notes, filed under a project or not, optionally saved out as a .md file
  pad:             ()            => api('/pad'),
  padCreate:       body          => api('/pad', { method: 'POST', body }),
  padUpdate:       (id, body)    => api(`/pad/${id}`, { method: 'PATCH', body }),
  padDelete:       id            => api(`/pad/${id}`, { method: 'DELETE' }),
  padSaveFile:     (id, body)    => api(`/pad/${id}/save-file`, { method: 'POST', body }),
  // to-do checklist: tick-off items for the right sidebar and the project pages (one item per line added)
  checklist:       ()            => api('/checklist'),
  checklistAdd:    body          => api('/checklist', { method: 'POST', body }),
  checklistUpdate: (id, body)    => api(`/checklist/${id}`, { method: 'PATCH', body }),
  checklistDelete: id            => api(`/checklist/${id}`, { method: 'DELETE' }),
  checklistClear:  pid           => api('/checklist' + (pid != null ? `?project_id=${pid}` : ''), { method: 'DELETE' }),
  checklistApply:  body          => api('/checklist/apply', { method: 'POST', body }),   // undo / redo, reorder
  bin:             ()            => api('/bin'),
  eraseFromBin:    id            => api(`/bin/${id}`, { method: 'DELETE' }),
  emptyBin:        ()            => api('/bin', { method: 'DELETE' }),
  toTop:           id            => api(`/tasks/${id}/top`, { method: 'POST' }),
  reorderQueue:    ids           => api('/queue/reorder', { method: 'POST', body: { ids } }),
  stop:            id            => api(`/tasks/${id}/stop`, { method: 'POST' }),
  switchMode:      (id, mode)    => api(`/tasks/${id}/mode`, { method: 'POST', body: { permission_mode: mode } }),
  pause:           id            => api(`/tasks/${id}/pause`, { method: 'POST' }),
  play:            id            => api(`/tasks/${id}/play`, { method: 'POST' }),
  chatImportant:   (id, on)      => api(`/tasks/${id}/important?important=${on ? 'true' : 'false'}`, { method: 'POST' }),
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
  restartService:  (hard = false) => api('/service/restart', { method: 'POST', body: { hard } }),
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
  editSchedule:    (id, patch)   => api(`/schedules/${id}`, { method: 'PATCH', body: patch }),
  deleteSchedule:  id            => api(`/schedules/${id}`, { method: 'DELETE' }),
  // personal assistant - see README › Personal assistant. Nothing here ever returns a token.
  assistStatus:    ()            => api('/assistant/status'),
  assistEmail:     ()            => api('/assistant/email/overview'),
  assistCalendar:  ()            => api('/assistant/calendar/overview'),
  assistReminders: ()            => api('/assistant/reminders'),
  assistActivity:  (limit = 60)  => api(`/assistant/activity?limit=${limit}`),
  // id: the provider ('google'); account: a connected account to sign in again (omit to add one)
  assistConnect:   (id, account) => api(`/assistant/connections/${id}/connect`, { method: 'POST', body: account ? { account } : {} }),
  assistDisconnect: id           => api(`/assistant/connections/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  assistDefault:   (id, service) => api(`/assistant/connections/${encodeURIComponent(id)}/default`, { method: 'PUT', body: { service } }),
  setPolicy:       (cap, policy) => api('/assistant/policies', { method: 'PATCH', body: { [cap]: policy } }),
  cancelReminder:  id            => api(`/assistant/reminders/${id}`, { method: 'DELETE' }),
  // project calendar and timelines - see timeline.py
  timeline:        ()            => api('/timeline'),
  addEvent:        (pid, body)   => api(`/projects/${pid}/timeline`, { method: 'POST', body }),
  updateEvent:     (id, patch)   => api(`/timeline/${id}`, { method: 'PATCH', body: patch }),
  deleteEvent:     id            => api(`/timeline/${id}`, { method: 'DELETE' }),
  briefing:        ()            => api('/briefing'),
  // a project's CRM record (crm.py): fields + progress + schedule; PATCH takes any of the fields and sends it all back
  projectRecord:   pid           => api(`/projects/${pid}`),
  updateProject:   (pid, body)   => api(`/projects/${pid}`, { method: 'PATCH', body }),
  // project phases and autopilot - see phases.py
  phases:          pid           => api(`/projects/${pid}/phases`),
  addPhase:        (pid, body)   => api(`/projects/${pid}/phases`, { method: 'POST', body }),
  updatePhase:     (id, patch)   => api(`/phases/${id}`, { method: 'PATCH', body: patch }),
  deletePhase:     id            => api(`/phases/${id}`, { method: 'DELETE' }),
  genModels:       ()            => api('/phases/generation-models'),
  generateTasks:   (id, model_id) => api(`/phases/${id}/generate-tasks`, { method: 'POST', body: { model_id } }),
  phaseDo:         (id, verb)    => api(`/phases/${id}/${verb}`, { method: 'POST' }),
  reorderPhases:   (pid, ids)    => api(`/projects/${pid}/phases/reorder`, { method: 'POST', body: { ids } }),
  assignTodo:      (id, phase_id) => api(`/todos/${id}/phase`, { method: 'PUT', body: { phase_id } }),
  report:          (pid, body)   => api(`/projects/${pid}/report`, { method: 'POST', body }),
  // CRM clients and contacts - see clients.py. A project's client is its client_id (updateProject);
  // linkContact / unlinkContact send the project's whole record back.
  // The team - see team.py.
  employees:       ()            => api('/employees'),
  employee:        id            => api(`/employees/${id}`),
  createEmployee:  body          => api('/employees', { method: 'POST', body }),
  updateEmployee:  (id, body)    => api(`/employees/${id}`, { method: 'PATCH', body }),
  deleteEmployee:  id            => api(`/employees/${id}`, { method: 'DELETE' }),
  addEmployeeTask: (id, body)    => api(`/employees/${id}/tasks`, { method: 'POST', body }),
  updateEmployeeTask: (id, body) => api(`/employee-tasks/${id}`, { method: 'PATCH', body }),
  deleteEmployeeTask: id         => api(`/employee-tasks/${id}`, { method: 'DELETE' }),
  addEmployeeLeave: (id, body)   => api(`/employees/${id}/leave`, { method: 'POST', body }),
  deleteEmployeeLeave: id        => api(`/employee-leave/${id}`, { method: 'DELETE' }),
  clients:         ()            => api('/clients'),
  client:          id            => api(`/clients/${id}`),
  createClient:    body          => api('/clients', { method: 'POST', body }),
  updateClient:    (id, body)    => api(`/clients/${id}`, { method: 'PATCH', body }),
  deleteClient:    id            => api(`/clients/${id}`, { method: 'DELETE' }),
  deleteClientLogo: id           => api(`/clients/${id}/logo`, { method: 'DELETE' }),
  contacts:        ()            => api('/contacts'),
  createContact:   body          => api('/contacts', { method: 'POST', body }),
  updateContact:   (id, body)    => api(`/contacts/${id}`, { method: 'PATCH', body }),
  deleteContact:   id            => api(`/contacts/${id}`, { method: 'DELETE' }),
  linkContact:     (pid, contact_id, role = 'stakeholder') => api(`/projects/${pid}/contacts`, { method: 'POST', body: { contact_id, role } }),
  unlinkContact:   (pid, contact_id) => api(`/projects/${pid}/contacts/${contact_id}`, { method: 'DELETE' }),
  // CRM activities and follow-ups - see activities.py. feed: one project / client / contact's timeline
  // (at: 'project' | 'client' | 'contact'); followups: the open ones and those done lately (Home, Calendar).
  feed:            (at, id)      => api(`/crm/feed?${at}_id=${id}`),
  createActivity:  body          => api('/activities', { method: 'POST', body }),
  updateActivity:  (id, body)    => api(`/activities/${id}`, { method: 'PATCH', body }),
  deleteActivity:  id            => api(`/activities/${id}`, { method: 'DELETE' }),
  followups:       ()            => api('/followups'),
  createFollowup:  body          => api('/followups', { method: 'POST', body }),
  updateFollowup:  (id, body)    => api(`/followups/${id}`, { method: 'PATCH', body }),
  deleteFollowup:  id            => api(`/followups/${id}`, { method: 'DELETE' }),
  // The CRM page and Home's CRM card (crm_reports.py): scope all | work | personal, maybe one client.
  crmDashboard:    (scope, clientId) => api(`/crm/dashboard?scope=${scope || 'all'}${clientId ? `&client_id=${clientId}` : ''}`),
};

// ---------------------------------------------------------------- dismissed failures (client-side)
const DISMISS_KEY = 'octopus-dismissed';
const readDismissed = () => { try { return new Set(JSON.parse(localStorage.getItem(DISMISS_KEY) || '[]')); } catch { return new Set(); } };
export function dismissTask(id) {
  const s = readDismissed(); s.add(id);
  try { localStorage.setItem(DISMISS_KEY, JSON.stringify([...s].slice(-300))); } catch {}
}

// ---------------------------------------------------------------- refresh
// Whether the server keeps project records (CRM) yet - it does once its projects carry the fields. Until the
// service restarts onto that code, don't ask it for GET /projects/{id}.
const hasRecords = list => (list || []).some(p => 'priority' in p);

Object.assign(store.data, {
  schedules: [], recipes: [], spend: null, settings: null,
  personal: { status: null, email: null, calendar: null, reminders: [], activity: [] },
  projectSettings: {}, todos: {}, notes: {}, documents: {}, phases: {}, records: {}, approvalCtx: {}, threads: {}, loaded: false, error: null,
  // CRM: every client (with counts and value) and contact, and the open client pages (GET /clients/{id}).
  clients: [], contacts: [], clientPages: {},
  // The team: every employee (with open / overdue task counts) and the open employee pages (GET /employees/{id}).
  employees: [], employeePages: {},
  // CRM activity timelines by 'project:3' / 'client:1' / 'contact:7' (GET /crm/feed), and every follow-up (GET /followups).
  // feedsMissing / followups.missing: the server predates them (it restarts onto the new code).
  feeds: {}, feedsMissing: false, followups: { items: [], loaded: false, missing: false },
  // The CRM dashboard by crmKey (scope + client), and crmMissing: the server predates it.
  crm: {}, crmMissing: false,
  messages: { chats: [], total: 0, loaded: false },
  bin: { items: [], keep_days: 30, loaded: false },
  pad: { items: [], loaded: false },
  checklist: { items: [], loaded: false },
  hiddenProjects: [], hidden: { tasks: [], loaded: false },
  timeline: { events: [], health: {}, kinds: {}, today: null, loaded: false }, briefing: null,
});
// The pages that show the project calendar (Home's "Coming up", the Calendar, a project's Timeline tab).
const TIMELINE_VIEWS = new Set(['home', 'calendar', 'project', 'briefing', 'notes']);

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
  // Hidden projects and queue items never reach the views above; the Hidden page lists them.
  const hiddenProjects = allProjects.filter(p => p.hidden && !p.is_general);
  const projects = allProjects.filter(p => !p.hidden && inScope(p));
  const visibleIds = new Set(projects.map(p => p.id));
  const approvals = allApprovals.filter(a => visibleIds.has(a.project_id));

  // /tasks is newest-first and capped; active tasks are fetched separately so an
  // old queued task never falls off the list.
  const byId = new Map();
  for (const t of [...recent, ...active]) if (visibleIds.has(t.project_id) && !t.hidden) byId.set(t.id, t);
  const names = new Map(projects.map(p => [p.id, p.name]));
  const dismissed = readDismissed();
  const tasks = [...byId.values()].sort((a, b) => b.id - a.id)
    .map(t => ({ ...t, project_name: t.project_name || names.get(t.project_id) || '?', dismissed: dismissed.has(t.id) }));

  const usage = {};
  for (const r of (usageRaw && usageRaw.by_model) || []) usage[r.key] = r;

  const d = { ...store.data, projects, hiddenProjects, tasks, approvals, status, models, usage, schedules, recipes, spend, settings, loaded: true, error: null, offlineSince: null, offlineFails: 0 };

  const extras = [];
  if (ui.view === 'project' && ui.projectId != null) {
    const pid = ui.projectId;
    extras.push(endpoints.todos(pid).then(v => { d.todos = { ...d.todos, [pid]: v }; }).catch(() => {}));
    extras.push(endpoints.notes(pid).then(v => { d.notes = { ...d.notes, [pid]: v }; }).catch(() => {}));
    extras.push(endpoints.documents(pid).then(v => { d.documents = { ...d.documents, [pid]: v }; }).catch(() => { if (d.documents[pid] === undefined) d.documents = { ...d.documents, [pid]: [] }; }));
    extras.push(more.projectSettings(pid).then(v => { d.projectSettings = { ...d.projectSettings, [pid]: v }; }).catch(() => {}));
    extras.push(more.phases(pid).then(v => { d.phases = { ...d.phases, [pid]: v }; }).catch(() => {}));
    if (hasRecords(allProjects)) extras.push(more.projectRecord(pid).then(v => { d.records = { ...d.records, [pid]: v }; }).catch(() => {}));
  }
  // Clients and contacts (the sidebar, search, pickers). A server from before clients has neither: keep the empty
  // lists, and only check again once a minute until it restarts onto the new code.
  if (hasRecords(allProjects) && (!d.clientsMissing || tick % 12 === 0)) {
    extras.push(more.clients().then(v => { d.clients = v; d.clientsMissing = false; }).catch(e => { if (e && e.message === 'Not Found') d.clientsMissing = true; }));
    extras.push(more.contacts().then(v => { d.contacts = v; }).catch(() => {}));
    extras.push(more.employees().then(v => { d.employees = v; }).catch(() => {}));
    if (ui.view === 'employee' && ui.employeeId) {
      const eid = ui.employeeId;
      extras.push(more.employee(eid).then(v => { d.employeePages = { ...d.employeePages, [eid]: v }; }).catch(e => { if (e && e.message === 'Employee not found') d.employeePages = { ...d.employeePages, [eid]: null }; }));
    }
    if (ui.view === 'client' && ui.clientId) {
      const cid = ui.clientId;
      extras.push(more.client(cid).then(v => { d.clientPages = { ...d.clientPages, [cid]: v }; }).catch(e => { if (gone(e)) d.clientPages = { ...d.clientPages, [cid]: null }; }));
    }
  }
  const freshThreads = {};
  if (pane && pane.taskId != null) {
    extras.push(more.thread(pane.taskId).then(v => { freshThreads[pane.taskId] = v; }).catch(() => {}));
  }
  // The Personal page's data is only fetched while it is open (calendar calls Google, so ease off: every 3rd poll).
  if (ui.view === 'personal' && tick % 3 === 0) extras.push(personalInto(d));
  // Also while the sidebar's Calendar row is expanded (it lists the upcoming items).
  if ((TIMELINE_VIEWS.has(ui.view) || (ui.navExpand && ui.navExpand.calendar)) && (tick % 3 === 0 || !d.timeline.loaded)) {
    extras.push(more.timeline().then(v => { d.timeline = { ...v, loaded: true }; }).catch(() => {}));
  }
  // CRM follow-ups (Home, the Calendar, project / client / contact pages) and the open page's activity timeline.
  const feedKey = feedKeyFor(ui);
  if (hasRecords(allProjects) && (!d.feedsMissing || tick % 12 === 0)) {
    if ((TIMELINE_VIEWS.has(ui.view) || feedKey || (ui.sides || []).includes('calendar')) && (tick % 3 === 0 || !d.followups.loaded)) {
      extras.push(more.followups().then(v => { d.followups = { items: v, loaded: true, missing: false }; }).catch(e => { if (notFound(e)) d.followups = { ...d.followups, loaded: true, missing: true }; }));
    }
    if (feedKey) {
      const [at, id] = feedKey.split(':');
      extras.push(more.feed(at, id).then(v => { d.feeds = { ...d.feeds, [feedKey]: v }; d.feedsMissing = false; }).catch(e => { if (notFound(e)) d.feedsMissing = true; else if (e && /not found/i.test(e.message || '')) d.feeds = { ...d.feeds, [feedKey]: null }; }));
    }
  }
  // The CRM dashboard: on its page, and on Home for the CRM card (every 3rd poll there).
  if (hasRecords(allProjects) && CRM_VIEWS.has(ui.view) && (!d.crmMissing || tick % 12 === 0)) {
    const key = crmKey(ui);
    if (ui.view === 'crm' || tick % 3 === 0 || !d.crm[key]) {
      extras.push(more.crmDashboard(...key.split('|')).then(v => { d.crm = { ...d.crm, [key]: v }; d.crmMissing = false; }).catch(e => { if (notFound(e)) d.crmMissing = true; }));
    }
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
  store.data = { ...d, messages: store.data.messages, bin: store.data.bin, pad: store.data.pad, checklist: store.data.checklist, hidden: store.data.hidden, octo: store.data.octo, threads: { ...store.data.threads, ...freshThreads } };
  loadChecklist().catch(() => {});
  if (ui.view === 'messages' || ui.sides.includes('messages')) loadMessages().catch(() => {});
  if (ui.sides.includes('calendar')) loadTimeline().catch(() => {});
  if (ui.view === 'bin') loadBin().catch(() => {});
  store.set({});
}

// ---------------------------------------------------------------- hidden
export async function loadHidden() {
  const tasks = await more.hiddenTasks();
  const names = new Map(store.data.hiddenProjects.map(p => [p.id, p.name]));
  store.data = { ...store.data, hidden: { tasks: tasks.filter(t => t.hidden && !names.has(t.project_id)), loaded: true } };
  store.set({});
}

// ---------------------------------------------------------------- recycle bin
export async function loadBin() {
  const v = await more.bin();
  store.data = { ...store.data, bin: { ...v, loaded: true } };
  store.set({});
}

// ---------------------------------------------------------------- to-do checklist
// chkSync.busy counts edits in flight: a poll that started before one must not put the older list back over it.
export const chkSync = { busy: 0 };
export async function loadChecklist() {
  if (chkSync.busy) return;
  const v = await more.checklist();
  if (chkSync.busy) return;
  const old = store.data.checklist;
  if (old.loaded && JSON.stringify(old.items) === JSON.stringify(v.items)) return;
  store.data = { ...store.data, checklist: { ...v, loaded: true } };
  store.set({});
}

// ---------------------------------------------------------------- notes pad
export async function loadPad() {
  const v = await more.pad();
  store.data = { ...store.data, pad: { ...v, loaded: true } };
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
  if (m.kind && m.kind !== 'all') params.kind = m.kind;
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
  const [todos, settings, notes, record] = await Promise.all([endpoints.todos(pid), more.projectSettings(pid), endpoints.notes(pid),
    hasRecords(store.data.projects) ? more.projectRecord(pid).catch(() => store.data.records[pid]) : store.data.records[pid]]);
  store.data = { ...store.data, todos: { ...store.data.todos, [pid]: todos }, notes: { ...store.data.notes, [pid]: notes }, projectSettings: { ...store.data.projectSettings, [pid]: settings },
    records: { ...store.data.records, [pid]: record } };
  store.set({});
}

// A client's page (GET /clients/{id}); null once it's gone.
const gone = e => !!e && e.message === 'Client not found';
export async function loadClient(cid) {
  let v;
  try { v = await more.client(cid); } catch (e) { if (!gone(e)) throw e; v = null; }
  store.data = { ...store.data, clientPages: { ...store.data.clientPages, [cid]: v } };
  store.set({});
}

// The team list and the open employee's page, now (after a change, not waiting for the next poll).
export async function loadTeam() {
  const jobs = [more.employees().then(v => { store.data = { ...store.data, employees: v }; })];
  const eid = store.ui.view === 'employee' && store.ui.employeeId;
  if (eid) jobs.push(more.employee(eid).then(v => { store.data = { ...store.data, employeePages: { ...store.data.employeePages, [eid]: v } }; })
    .catch(e => { if (e && e.message === 'Employee not found') store.data = { ...store.data, employeePages: { ...store.data.employeePages, [eid]: null } }; else throw e; }));
  await Promise.all(jobs);
  store.set({});
}

// Clients and contacts lists, now (after a change, not waiting for the next poll).
export async function loadClients() {
  const [clients, contacts] = await Promise.all([more.clients(), more.contacts()]);
  store.data = { ...store.data, clients, contacts };
  store.set({});
}

// The activity timeline the open page shows: a project's (Overview tab), a client's or a contact's - null elsewhere.
export function feedKeyFor(ui) {
  if (ui.view === 'project' && ui.projectId != null && ui.projectTab === 'overview') return `project:${ui.projectId}`;
  if (ui.view === 'client' && ui.clientId) return `client:${ui.clientId}`;
  if (ui.view === 'contact' && ui.contactId) return `contact:${ui.contactId}`;
  return null;
}
const notFound = e => !!e && e.message === 'Not Found';

// One page's activity timeline, now (after a change, or on opening the page).
export async function loadFeed(at, id) {
  try {
    const v = await more.feed(at, id);
    store.data = { ...store.data, feeds: { ...store.data.feeds, [`${at}:${id}`]: v }, feedsMissing: false };
  } catch (e) {
    if (notFound(e)) store.data = { ...store.data, feedsMissing: true };
    else if (e && /not found/i.test(e.message || '')) store.data = { ...store.data, feeds: { ...store.data.feeds, [`${at}:${id}`]: null } };
    else throw e;
  }
  store.set({});
}

// Which dashboard a view shows: 'scope|clientId' (Home's card never filters by client).
const CRM_VIEWS = new Set(['crm', 'home']);
// A client filter left on a client that's since been deleted falls back to all clients.
export const crmClientOf = ui => (ui.view === 'crm' && ui.crmClient && (store.data.clients || []).some(c => String(c.id) === String(ui.crmClient)) ? String(ui.crmClient) : '');
export const crmKey = ui => `${ui.scope || 'all'}|${crmClientOf(ui)}`;
export async function loadCrm() {
  const key = crmKey(store.ui);
  try {
    const v = await more.crmDashboard(...key.split('|'));
    store.data = { ...store.data, crm: { ...store.data.crm, [key]: v }, crmMissing: false };
  } catch (e) {
    if (!notFound(e)) throw e;
    store.data = { ...store.data, crmMissing: true };
  }
  store.set({});
}

export async function loadFollowups() {
  const v = await more.followups();
  store.data = { ...store.data, followups: { items: v, loaded: true, missing: false } };
  store.set({});
}

export async function loadPhases(pid) {
  const [phases, todos, settings] = await Promise.all([more.phases(pid), endpoints.todos(pid), more.projectSettings(pid)]);
  store.data = { ...store.data, phases: { ...store.data.phases, [pid]: phases }, todos: { ...store.data.todos, [pid]: todos },
    projectSettings: { ...store.data.projectSettings, [pid]: settings } };
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
export const clientById = (id, d = store.data) => (d.clients || []).find(c => c.id === id);
// A client's logo if it has one, else its colour (its own, or one picked from its name) - for a CSS `background:`.
export const clientLogo = c => (c && c.logo ? `/static/project-images/${encodeURIComponent(c.logo)}` : null);
export function clientColor(c) {
  if (!c) return 'var(--p-muted)';
  if (c.color) return c.color;
  let h = 0;
  for (const ch of c.name || '') h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `var(--p${(h % 5) + 1})`;
}
export function clientFill(c) {
  const img = clientLogo(c);
  return img ? `url('${img}') center/cover no-repeat ${clientColor(c)}` : clientColor(c);
}
export const taskById = (id, d = store.data) => d.tasks.find(t => t.id === id);
export const isGeneral = p => !!(p && p.is_general);
// A project with no folder is a CRM record only: everything but running work (db.NO_FOLDER_HINT).
export const crmOnly = p => !!p && !p.is_general && !(p.path || '').trim();
export const NO_FOLDER_HINT = 'Link a folder to let Octopus work on this project';
export const generalProject = (d = store.data) => d.projects.find(p => p.is_general);
export const SCOPES = [['all', 'All'], ['work', 'Work'], ['personal', 'Personal']];

// Where a project stands: [key, label, dot colour]. Set by hand on the project page.
export const PROJECT_STATUSES = [
  ['not_started', 'Not started', 'var(--text-meta)'],
  ['in_progress', 'In progress', 'var(--run)'],
  ['need_info', 'Need info', 'var(--wait)'],
  ['on_hold', 'On hold', 'var(--queued)'],
  ['completed', 'Completed', 'var(--run-soft)'],
  ['archived', 'Archived', 'var(--text-meta)'],
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
  if (p.color) return p.color;
  const i = realProjects(d).sort((a, b) => a.id - b.id).findIndex(x => x.id === p.id);
  return `var(--p${(i % 5) + 1})`;
}

// The picture the user gave a project (or null): small squares and avatars show it instead of the colour.
export const projectImage = p => (p && p.image ? `/static/project-images/${encodeURIComponent(p.image)}` : null);
// The tile behind a project's logo/letter: its own logo background, else the project colour.
const logoBg = (p, d) => (p && p.logo_bg) || projectColor(p, d);
// For a CSS `background:` on an avatar tile only (bars, dots and chips use projectColor).
export function projectFill(p, d = store.data) {
  const img = projectImage(p);
  return img ? `url('${img}') center/cover no-repeat ${logoBg(p, d)}` : logoBg(p, d);
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
    msgs: g.msgs,
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


// ---------------------------------------------------------------- bulk actions
// What each bulk change can touch. Anything else in the selection is skipped (and the confirm says so).
export const BULK_KINDS = {
  cancel:   { label: 'Cancel',        ok: t => t.status === 'queued' },
  move:     { label: 'Move',          ok: t => !['running', 'deleted'].includes(t.status) },
  priority: { label: 'Set priority',  ok: t => t.status === 'queued' },
  rerun:    { label: 'Re-run',        ok: t => ['error', 'needs_review', 'cancelled', 'rate_limited'].includes(t.status) },
};
// -> { ok: tasks the change applies to, skipped: how many ticked tasks it doesn't }. Moves go by chat, once each.
export function bulkPlan(kind, ids) {
  const tasks = ids.map(id => taskById(id)).filter(Boolean);
  let ok = tasks.filter(BULK_KINDS[kind].ok);
  if (kind === 'move') {
    const roots = [...new Set(ok.map(t => t.thread_id || t.id))];
    ok = roots.map(id => taskById(id)).filter(Boolean);
  }
  return { ok, skipped: kind === 'move' ? 0 : tasks.length - ok.length };
}
