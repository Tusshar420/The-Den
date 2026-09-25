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
  overview:    ()             => api('/octopus/overview'),
  setModel:    (id, enabled)  => api(`/octopus/models/${encodeURIComponent(id)}`, { method: 'PATCH', body: { enabled } }),
  detail:      id             => api(`/tasks/${id}/detail`),
  diff:        id             => api(`/tasks/${id}/diff`),
  voice:       ()             => api('/voice'),
  voiceCommand: body          => api('/voice/command', { method: 'POST', body }),
  policies:    ()             => api('/assistant/policies'),
  setPolicy:   (cap, policy)  => api('/assistant/policies', { method: 'PATCH', body: { [cap]: policy } }),
  setSettings: patch          => api('/settings', { method: 'PATCH', body: patch }),
};

Object.assign(store.data, {
  octo: { agents: [], tools: [], providers: null, overview: null, routing: null, detail: {}, diffs: {}, voice: null, policies: null },
});

// UI state for the Octopus features (store.ui.octo).
Object.assign(store.ui, {
  octo: {
    plan: null,                 // the plan card: { routingId, plan, project, model, agent, busy, error, open }
    agent: 'auto',              // Ask box override: an agents.json id
    advanced: false,            // Ask box "Advanced" row open
    detailOpen: false,          // the task pane shows Details instead of the chat
    diffOpen: false,
    routing: { status: 'all', category: 'all', open: null },
    voice: { state: 'idle', text: '', error: '', heard: '', conversation: false, log: [] },     // idle | listening | working | speaking; conversation = keep listening between replies; log = the transcript in the voice pane
    speakBusy: false,
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
export const loadPolicies  = () => keep('policies', octoApi.policies);

export function loadRouting() {
  const f = store.ui.octo.routing;
  const q = new URLSearchParams({ limit: '200' });
  if (f.status !== 'all') q.set('status', f.status);
  if (f.category !== 'all') q.set('category', f.category);
  return keep('routing', () => octoApi.routing(q.toString()));
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
  if (u.view === 'routing') loadRouting();
  else if (u.view === 'providers') { loadProviders(); }
  else if (u.view === 'settings') { loadVoice(); loadPolicies(); loadProviders(); }
  else if (u.view === 'home') { loadProviders(); loadOverview(); }
  const p = u.pane;
  if (p && p.taskId != null && u.octo.detailOpen) loadDetail(latestInChat(p.taskId));
}

// Refresh whatever the open page shows, every few seconds (the queue snapshot has its own poll in data.js).
export function startOctopusData() {
  setInterval(() => { if (!document.hidden && store.ui.booted) loadForView(); }, 5000);
}

// Called by app.js on every store change: when the view, an open task page or a filter changes,
// load what that page needs right away instead of waiting for the next tick.
let lastKey = '';
export function onStoreChange() {
  const u = store.ui, p = u.pane;
  const key = [u.view, u.octo.routing.status, u.octo.routing.category, p && p.taskId, u.octo.detailOpen, u.booted].join('|');
  if (key === lastKey || !u.booted) return;
  lastKey = key;
  loadForView();
}
