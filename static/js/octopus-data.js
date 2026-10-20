// Data for the Octopus features: plans, routing history, agents, tools, providers, task detail,
// voice. Kept apart from data.js (which loads the queue snapshot); everything lands in
// store.data.octo and the page that needs it loads it - on open, and again every few seconds
// while it stays open.

import { api, store } from './api.js';

export const octoApi = {
  send:        (text, o = {}) => api('/octopus', { method: 'POST', body: { text, source: 'dashboard', ...o } }),
  plan:        (text, o = {}) => api('/octopus/plan', { method: 'POST', body: { text, ...o } }),
  runPlan:     (id, o = {})   => api(`/octopus/plans/${id}/run`, { method: 'POST', body: o }),
  cancelPlan:  id             => api(`/octopus/plans/${id}/cancel`, { method: 'POST' }),
  result:      (id, wait = 0) => api(`/octopus/result/${id}?wait=${wait}`),
  routing:     (q = 'limit=200') => api(`/routing?${q}`),
  agents:      ()             => api('/octopus/agents'),
  tools:       ()             => api('/octopus/tools'),
  providers:   ()             => api('/octopus/providers'),
  sites:       ()             => api('/octopus/sites'),
  learnSite:   url            => api('/octopus/sites/learn', { method: 'POST', body: { url } }),
  overview:   ()             => api('/octopus/overview'),
  setModel:    (id, enabled)  => api(`/octopus/models/${encodeURIComponent(id)}`, { method: 'PATCH', body: { enabled } }),
  // models added on the AI Providers page (octopus/models.py)
  addModel:    body           => api('/octopus/models', { method: 'POST', body }),
  editModel:   (id, body)     => api(`/octopus/models/${encodeURIComponent(id)}`, { method: 'PUT', body }),
  removeModel: id             => api(`/octopus/models/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // OpenAI / Gemini API keys (api_keys.py) - each returns the providers view
  addApiKey:   body           => api('/octopus/api-keys', { method: 'POST', body }),
  removeApiKey: id            => api(`/octopus/api-keys/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  defaultApiKey: id           => api(`/octopus/api-keys/${encodeURIComponent(id)}/default`, { method: 'POST' }),
  detail:      id             => api(`/tasks/${id}/detail`),
  diff:        id             => api(`/tasks/${id}/diff`),
  voice:       ()             => api('/voice'),
  tokens:      q              => api(`/usage/tokens?${q}`),
  voiceCommand: body          => api('/voice/command', { method: 'POST', body }),
  // Alexa (alexa/api.py) and the "I'm here" heartbeat (presence.py)
  alexa:       ()             => api('/alexa/status'),
  alexaPrefs:  body           => api('/alexa/prefs', { method: 'PATCH', body }),
  alexaTest:   ()             => api('/alexa/test-notification', { method: 'POST' }),
  alexaUnlink: ()             => api('/alexa/link', { method: 'DELETE' }),
  presence:    body           => api('/presence', { method: 'POST', body }),
  policies:    ()             => api('/assistant/policies'),
  setPolicy:   (cap, policy)  => api('/assistant/policies', { method: 'PATCH', body: { [cap]: policy } }),
  settings:    ()             => api('/settings'),
  setSettings: patch          => api('/settings', { method: 'PATCH', body: patch }),
  // Claude accounts (accounts.py) - each returns the settings view (or the list, for add)
  useAccount:  id             => api(`/accounts/${encodeURIComponent(id)}/activate`, { method: 'POST' }),
  checkAccount: id            => api(`/accounts/${encodeURIComponent(id)}/refresh`, { method: 'POST' }),
  addAccount:  body           => api('/accounts', { method: 'POST', body }),
  removeAccount: id           => api(`/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  aliasAccount: (id, alias)   => api(`/accounts/${encodeURIComponent(id)}/alias`, { method: 'PUT', body: { alias } }),
  colorAccount: (id, color)   => api(`/accounts/${encodeURIComponent(id)}/color`, { method: 'PUT', body: { color } }),
  editAccount: (id, body)     => api(`/accounts/${encodeURIComponent(id)}/details`, { method: 'PUT', body }),
  showMacAccount: ()          => api('/accounts/mac/show', { method: 'POST' }),
};

Object.assign(store.data, {
  octo: { agents: [], tools: [], providers: null, overview: null, routing: null, detail: {}, diffs: {}, voice: null, policies: null, tokens: null, alexa: null, sites: null },
});

// UI state for the Octopus features (store.ui.octo).
Object.assign(store.ui, {
  octo: {
    plan: null,                 // the plan card: { routingId, plan, project, model, agent, busy, error, open }
    agent: 'auto',              // Ask box override: an agents.json id
    advanced: false,            // Ask box "Advanced" row open
    diffOpen: false,
    routing: { status: 'all', category: 'all', open: null },
    voice: { state: 'idle', text: '', error: '', heard: '', conversation: false, log: [], pending: 0 },     // idle | listening | working | speaking; conversation = keep listening between replies; log = the transcript in the voice pane; pending = queued tasks whose answer hasn't come back yet
    speakBusy: false,
    acct: { label: '', email: '', token: '', busy: '', error: '' },   // Settings › Claude account form; busy: '' | 'switch' | 'refresh' | 'add' | 'remove'
    mf: null,                   // AI Providers › Add / edit a model form (null = closed); id set = editing that one
    keys: {},                   // AI Providers › Add a key forms, per provider: { label, key, busy, error }
    // Token usage page. period: today | 7d | 30d | month | lastmonth | YYYY-MM | custom (then start/end, YYYY-MM-DD);
    // project / model / lane: '' = all. group: chat | run. sort: tokens | cost | new. open: the chat row expanded.
    usage: { period: '30d', start: '', end: '', project: '', model: '', lane: '', account: '', group: 'chat', sort: 'tokens', open: null, limit: 50 },
  },
});

// Merge into store.data.octo and let the views re-render.
export function putOcto(patch) {
  store.data = { ...store.data, octo: { ...store.data.octo, ...patch } };
  store.set({});
}
const keep = (key, fn) => fn().then(v => putOcto({ [key]: v }), () => {});

export const loadAgents    = () => Promise.all([keep('agents', octoApi.agents), keep('tools', octoApi.tools)]);
export const loadProviders = () => keep('providers', octoApi.providers);
export const loadOverview  = () => keep('overview', octoApi.overview);
export const loadVoice     = () => keep('voice', octoApi.voice);
export const loadAlexa     = () => keep('alexa', octoApi.alexa);
export const loadPolicies  = () => keep('policies', octoApi.policies);
export const loadSites     = () => keep('sites', octoApi.sites);

export function loadRouting() {
  const f = store.ui.octo.routing;
  const q = new URLSearchParams({ limit: '200' });
  if (f.status !== 'all') q.set('status', f.status);
  if (f.category !== 'all') q.set('category', f.category);
  return keep('routing', () => octoApi.routing(q.toString()));
}

// The Token usage page's date range for its period, as local YYYY-MM-DD days (both included).
const ymd = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
export function usageRange(f) {
  const now = new Date(), today = ymd(now);
  const back = n => ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate() - n));
  if (f.period === 'today') return [today, today];
  if (f.period === '7d') return [back(6), today];
  if (f.period === 'month') return [ymd(new Date(now.getFullYear(), now.getMonth(), 1)), today];
  if (f.period === 'lastmonth') return [ymd(new Date(now.getFullYear(), now.getMonth() - 1, 1)), ymd(new Date(now.getFullYear(), now.getMonth(), 0))];
  if (/^\d{4}-\d{2}$/.test(f.period)) {
    const [y, m] = f.period.split('-').map(Number);
    const last = ymd(new Date(y, m, 0));
    return [`${f.period}-01`, last < today ? last : today];
  }
  if (f.period === 'custom' && f.start && f.end) return f.start <= f.end ? [f.start, f.end] : [f.end, f.start];
  return [back(29), today];
}

// The page polls like the others, but only re-asks every 30s unless a filter changed (it can carry a lot of rows).
let tokensKey = '', tokensAt = 0;
export function loadTokens(force = false) {
  const f = store.ui.octo.usage;
  const [start, end] = usageRange(f);
  const q = new URLSearchParams({ start, end });
  if (f.project) q.set('project_id', f.project);
  if (f.model) q.set('model_id', f.model);
  if (f.lane) q.set('lane', f.lane);
  if (f.account) q.set('account', f.account);
  const key = q.toString();
  if (!force && key === tokensKey && Date.now() - tokensAt < 30000) return Promise.resolve();
  tokensKey = key; tokensAt = Date.now();
  return octoApi.tokens(key).then(v => { if (key === tokensKey) putOcto({ tokens: v }); },
    e => { if (key === tokensKey) putOcto({ tokens: { error: e.message } }); });
}

export function loadDetail(id) {
  return octoApi.detail(id).then(v => putOcto({ detail: { ...store.data.octo.detail, [id]: v } }), () => {});
}
export function loadDiff(id) {
  putOcto({ diffs: { ...store.data.octo.diffs, [id]: { loading: true } } });
  return octoApi.diff(id).then(v => putOcto({ diffs: { ...store.data.octo.diffs, [id]: v } }),
    e => putOcto({ diffs: { ...store.data.octo.diffs, [id]: { error: e.message } } }));
}

// Small lookups the views share.
// A chat is a root task plus its replies; the page describes the newest one (what is happening now).
export const latestInChat = rootId => store.data.tasks.filter(t => (t.thread_id || t.id) === rootId).reduce((m, t) => Math.max(m, t.id), rootId);
export const agentById = id => store.data.octo.agents.find(a => a.id === id);
export const agentLabel = id => (agentById(id) || {}).label || (id ? id : '');

// What each view needs loaded. Called when the view (or an open task page) changes, and on a timer.
export function loadForView() {
  const u = store.ui;
  if (!store.data.octo.agents.length) loadAgents();
  if (!store.data.octo.voice) loadVoice();                       // its language table is how replies are matched to a voice
  if (u.view === 'routing') loadRouting();
  else if (u.view === 'providers') { loadProviders(); }
  else if (u.view === 'settings') { loadPolicies(); loadProviders(); if (u.settingsTab === 'sites') loadSites(); }
  else if (u.view === 'voice') { loadVoice(); loadAlexa(); }
  else if (u.view === 'usage') loadTokens();
  else if (u.view === 'home') { loadProviders(); loadOverview(); }
  if (u.sheet === 'taskDetail' && u.detailTask != null) loadDetail(latestInChat(u.detailTask));
}

// Refresh whatever the open page shows, every few seconds (the queue snapshot has its own poll in data.js).
export function startOctopusData() {
  setInterval(() => { if (!document.hidden && store.ui.booted) loadForView(); }, 5000);
  startPresence();
}

// "I'm here": while this tab is visible and has been touched in the last few minutes, tell The Den
// every 20 seconds. Approvals only go to Alexa when no dashboard is in use (presence.py).
const PRESENCE_EVERY = 20000, IDLE_AFTER = 5 * 60000;
function startPresence() {
  let touched = Date.now();
  const device = (navigator.userAgent.match(/iPhone|iPad|Android|Macintosh|Windows/) || ['browser'])[0].replace('Macintosh', 'Mac');
  const beat = () => { if (!document.hidden && store.ui.booted && Date.now() - touched < IDLE_AFTER) octoApi.presence({ device }).catch(() => {}); };
  for (const ev of ['pointerdown', 'keydown', 'wheel', 'touchstart']) addEventListener(ev, () => { touched = Date.now(); }, { passive: true, capture: true });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { touched = Date.now(); beat(); } });
  setInterval(beat, PRESENCE_EVERY);
  beat();
}

// Called by app.js on every store change: when the view, an open task page or a filter changes,
// load what that page needs right away instead of waiting for the next tick.
let lastKey = '';
export function onStoreChange() {
  const u = store.ui, p = u.pane;
  const f = u.octo.usage;
  const key = [u.view, u.octo.routing.status, u.octo.routing.category, p && p.taskId, u.settingsTab, u.sheet === 'taskDetail' && u.detailTask, u.booted,
    f.period, f.start, f.end, f.project, f.model, f.lane, f.account].join('|');
  if (key === lastKey || !u.booted) return;
  lastKey = key;
  loadForView();
}
