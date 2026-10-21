// The Den 2.0 - shared data layer for the design components (*.dc.html).
// Every component reads live data from window.Den instead of the mock
// constants it shipped with, and calls Den.act() where the prototype showed a
// toast. Plain script (no modules) so it is ready before support.js boots.
(function () {
  'use strict';
  const KEY_STORAGE = 'cq_api_key';           // same key the old dashboard kept - nobody re-enters it
  const C = { coral: '#FF7B5C', amber: '#FFB547', teal: '#45E0C8', err: '#FF5470', blue: '#7FB2FF', violet: '#A58BFF', green: '#6FCF97' };
  const PALETTE = ['#FF7B5C', '#7FB2FF', '#E6C07B', '#7FD1B9', '#A58BFF', '#45E0C8', '#FFB547', '#E8825A', '#4F9DE8', '#C9B8FF'];
  const POLL_MS = 5000;

  // The last errors this page hit - the Status screens show and report them (Den 2.1).
  const errors = [];
  const keepError = (msg, stack) => { errors.push({ at: new Date().toISOString(), text: String(stack || msg || 'Unknown error').slice(0, 4000) + '\n    ' + location.hash + ' · ' + navigator.userAgent }); if (errors.length > 20) errors.shift(); };
  window.addEventListener('error', e => keepError(e.message, e.error && e.error.stack));
  window.addEventListener('unhandledrejection', e => { const r = e.reason; keepError(r && r.message ? r.message : String(r), r && r.stack); });

  const subs = new Set();
  let emitQueued = false;
  const emit = () => {
    if (emitQueued) return;
    emitQueued = true;
    Promise.resolve().then(() => { emitQueued = false; subs.forEach(fn => { try { fn(); } catch (e) { console.error(e); } }); });
  };

  const store = {
    loaded: false, needKey: false, offline: false,
    projects: [], tasks: [], approvals: [], status: null, settings: null, models: [], summary: null, service: null,
  };
  // Lazily loaded resources: name -> { data, at, busy, path, ttl }
  const cache = {};

  const tint = (hex, a) => { if (!hex || hex[0] !== '#') return hex; const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };

  async function api(path, { method = 'GET', body, raw, headers } = {}) {
    // Signed out (or the server refused us): don't keep asking - 10 refusals a minute lock this device out (429).
    if (store.needKey) { const err = new Error('Sign-in needed'); err.status = 401; throw err; }
    // The session cookie goes with every request by itself; a browser that still keeps the old key sends that too.
    const h = { ...(Den.key() ? { 'X-API-Key': Den.key() } : {}), ...(headers || {}) };
    const opts = { method, headers: h };
    if (body !== undefined) {
      if (raw) opts.body = body;
      else { h['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    }
    let res;
    try { res = await fetch(Den.base() + path, opts); } catch (e) { store.offline = true; emit(); throw new Error('The Den is not reachable'); }
    if (store.offline) { store.offline = false; emit(); }
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail || j); } catch (e) {}
      // Only a refused API key means "sign in again" - a wrong PIN or a locked vault (401/403 too) must not.
      if (res.status === 401 && /X-API-Key/i.test(detail || '')) {
        store.needKey = true; emit();
        const err = new Error('Sign-in needed'); err.status = res.status; throw err;
      }
      const err = new Error(detail || ('HTTP ' + res.status)); err.status = res.status; throw err;
    }
    if (res.status === 204) return null;
    const ct = res.headers.get('content-type') || '';
    return ct.includes('json') ? res.json() : res.text();
  }

  async function refresh() {
    const [projects, tasks, approvals, status, settings, models, summary] = await Promise.all([
      api('/projects'), api('/tasks?limit=300'), api('/approvals'), api('/queue/status'),
      api('/settings').catch(() => store.settings), api('/octopus/models').catch(() => store.models),
      api('/usage/summary').catch(() => store.summary),
    ]);
    Object.assign(store, { projects, tasks, approvals, status, settings, models, summary, loaded: true, needKey: false });
    migrateWorkspaces();
    cur();                                     // window.denCur for the pages that read it directly
    emit();
  }

  // Workspaces used to be other Dens by address; they're working areas now. Turn any old entries (like
  // "Cmentor") into areas once, then forget the addresses. Waits quietly until the server has /den/areas.
  let migrating = false;
  function migrateWorkspaces() {
    const old = (prefs().workspaces || []); if (!old.length || migrating) return; migrating = true;
    Promise.all(old.map(w => api('/den/areas', { method: 'POST', body: { name: w.name } }).catch(e => { if (e.status !== 409) throw e; })))
      .then(() => { setPref('workspaces', []); invalidate('areas'); }).catch(() => {}).finally(() => { migrating = false; });
  }

  let pollId = null, hiddenTicks = 0;
  // In a background tab only approvals are checked (every 20s), so a new one can still raise a desktop alert.
  function pollApprovals() {
    api('/approvals').then(a => {
      const sig = x => (x || []).map(y => y.id + ':' + y.status).join();
      if (sig(a) !== sig(store.approvals)) { store.approvals = a; emit(); }
    }).catch(() => {});
  }
  // ---------------------------------------------------------------- signing in (login.py)
  // A username and password give this browser a session cookie the page's scripts can't read. A browser
  // that still keeps the old API key swaps it for a session as soon as a login exists, then forgets the key.
  const forgetKey = () => { try { localStorage.removeItem(KEY_STORAGE); } catch (e) {} };
  async function authPost(path, body) {
    const h = { 'Content-Type': 'application/json', ...(Den.key() ? { 'X-API-Key': Den.key() } : {}) };
    let res;
    try { res = await fetch(base() + path, { method: 'POST', headers: h, body: JSON.stringify(body || {}) }); }
    catch (e) { const err = new Error('The Den isn’t reachable right now.'); err.status = 0; throw err; }
    let j = null; try { j = await res.json(); } catch (e) {}
    if (!res.ok) { const err = new Error((j && typeof j.detail === 'string' && j.detail) || ('HTTP ' + res.status)); err.status = res.status; throw err; }
    return j;
  }
  function authState() {
    return fetch(base() + '/auth/state', { headers: Den.key() ? { 'X-API-Key': Den.key() } : {} })
      .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))).then(a => { store.auth = a; emit(); return a; });
  }
  function signedIn(j) {
    forgetKey();
    store.auth = { ...(store.auth || {}), configured: true, signed_in: true, via: 'session', username: j && j.username };
    store.needKey = false; start(); emit();
    return j;
  }
  const auth = {
    state: authState,
    login: (username, password, remember) => authPost('/auth/login', { username, password, remember: remember !== false }).then(signedIn),
    // body: { username, password, remember, key | current_password } - creates the login, or replaces it
    setup: body => authPost('/auth/setup', body).then(signedIn),
    later: () => { store.authLater = true; emit(); },
    logout: everywhere => authPost('/auth/logout', { everywhere: !!everywhere }).catch(() => {}).then(() => { forgetKey(); location.reload(); }),
  };
  function boot() {
    authState().then(a => {
      if (!a.signed_in) { store.needKey = true; emit(); return; }
      if (a.via === 'key' && a.configured) authPost('/auth/key-session').then(signedIn).catch(() => {});
      start();
    }, () => start());
  }

  function start() {
    refresh().catch(e => console.warn('[den]', e.message));
    clearInterval(pollId);
    pollId = setInterval(() => {
      if (store.needKey) return;
      if (document.hidden) { if (store.loaded && ++hiddenTicks % 4 === 0) pollApprovals(); return; }
      refresh().catch(() => {});
      // keep lazily loaded resources that are on screen fresh too
      const now = Date.now();
      Object.keys(cache).forEach(k => { const c = cache[k]; if (c.live && now - c.at > c.ttl) load(k); });
    }, POLL_MS);
  }

  // need(name, path, ttl): the cached value (undefined until first load); fetches when stale.
  function need(name, path, ttl = 30000) {
    let c = cache[name];
    if (!c) c = cache[name] = { data: undefined, at: 0, busy: false, path, ttl, live: true };
    if (path && c.path !== path) { c.path = path; c.at = 0; c.data = undefined; }
    c.ttl = ttl;
    if (!c.busy && Date.now() - c.at > ttl) load(name);
    return scoped(name, c.data);
  }
  // The workspace switcher: lists that belong to projects only show the chosen workspace's (General is in all).
  // The result is remembered per workspace so the same array comes back until the data or the workspace changes.
  const byProject = (list, f = x => x.project_id) => (Array.isArray(list) ? list.filter(x => inArea(f(x) == null ? null : projectById(f(x)))) : list);
  function scoped(name, data) {
    if (!data || area() === 'all') return data;
    const c = cache[name];
    if (c.scopedFrom === data && c.scopedArea === area() && c.scopedN === store.projects.length) return c.scopedOut;
    let out = data;
    if (name === 'timeline' && Array.isArray(data.events)) {
      out = { ...data, events: byProject(data.events), health: Object.fromEntries(Object.entries(data.health || {}).filter(([id]) => inArea(projectById(Number(id))))) };
    } else if (name === 'schedules' || name === 'followups' || name === 'wkfu') {
      out = Array.isArray(data) ? data.filter(x => (x.subject && x.subject !== 'project') || inArea(x.project_id == null && x.subject_id == null ? null : projectById(x.project_id != null ? x.project_id : x.subject_id))) : data;
    } else if (name === 'activity' && Array.isArray(data.events)) out = { ...data, events: byProject(data.events) };
    else if (name === 'rpMessages' && Array.isArray(data.chats)) out = { ...data, chats: byProject(data.chats) };
    else if (name === 'pad' && Array.isArray(data.items)) out = { ...data, items: byProject(data.items) };
    else if (name === 'checklist' && Array.isArray(data.items)) out = { ...data, items: byProject(data.items) };
    else if (name === 'wkruns' && Array.isArray(data.runs)) out = { ...data, runs: byProject(data.runs) };
    c.scopedFrom = data; c.scopedArea = area(); c.scopedN = store.projects.length; c.scopedOut = out;
    return out;
  }
  function load(name) {
    const c = cache[name];
    if (!c || c.busy || !c.path || store.needKey) return Promise.resolve(c && c.data);   // retried after sign-in
    c.busy = true;
    return api(c.path).then(d => { c.data = d; c.err = null; }).catch(e => { c.err = e.message; if (c.data === undefined) c.data = null; })
      .finally(() => { c.busy = false; c.at = Date.now(); emit(); });
  }
  function invalidate(...names) {
    names.flat().forEach(n => {
      Object.keys(cache).forEach(k => { if (k === n || k.startsWith(n + ':')) { cache[k].at = 0; load(k); } });
    });
  }

  // act: run a mutation, then refresh what it touched. Shows a toast on success/failure.
  // at: show it as the small pill at the bottom of that panel instead ('cc', 'pm', 'queue'; see Home's pill()).
  async function act(promiseOrFn, { ok, fail = 'Something went wrong', reload = [], core = true, at } = {}) {
    const say = (t, s, c) => at && Den.pill ? Den.pill(at, t, s, c) : Den.say(t, s, c);
    try {
      const r = await (typeof promiseOrFn === 'function' ? promiseOrFn() : promiseOrFn);
      if (ok) { const [t, s, c] = typeof ok === 'function' ? ok(r) : ok; say(t, s, c); }
      if (core) refresh().catch(() => {});
      if (reload.length) invalidate(reload);
      return r;
    } catch (e) {
      say(fail, e.message, C.err);
      throw e;
    }
  }

  // ---------------------------------------------------------------- formatting
  const pad2 = n => String(n).padStart(2, '0');
  const parseT = iso => (iso ? new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) || iso.length <= 10 ? iso : iso + 'Z') : null);
  function ago(iso, short = true) {
    const d = parseT(iso); if (!d) return '';
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 45) return short ? 'now' : 'just now';
    if (s < 3600) return Math.round(s / 60) + 'm' + (short ? '' : ' ago');
    if (s < 86400) return Math.round(s / 3600) + 'h' + (short ? '' : ' ago');
    if (s < 86400 * 7) return Math.round(s / 86400) + 'd' + (short ? '' : ' ago');
    return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
  }
  const clock = iso => { const d = parseT(iso); return d ? pad2(d.getHours()) + ':' + pad2(d.getMinutes()) : ''; };
  const dayClock = iso => { const d = parseT(iso); if (!d) return ''; const today = new Date(); const same = d.toDateString() === today.toDateString();
    return same ? clock(iso) : d.toLocaleDateString([], { weekday: 'short' }) + ' ' + clock(iso); };
  const dur = ms => { if (!ms && ms !== 0) return ''; const s = Math.round(ms / 1000); if (s < 60) return s + 's'; const m = Math.round(s / 60); if (m < 60) return m + 'm'; return Math.floor(m / 60) + 'h ' + (m % 60) + 'm'; };
  // Settings › Money: one currency symbol for the whole Den (symbol only - amounts are never converted).
  // A deal stored in another currency than USD keeps its own symbol.
  const CURRENCIES = { USD: '$', INR: '₹', EUR: '€', GBP: '£', AED: 'AED ', AUD: 'A$' };
  const currency = () => { const c = (prefs() || {}).currency; return CURRENCIES[c] ? c : 'USD'; };
  const cur = () => { const sym = CURRENCIES[currency()]; window.denCur = sym; return sym; };
  const money = (v, code) => { if (v == null) return ''; const sym = code && code !== 'USD' && CURRENCIES[code] ? CURRENCIES[code] : cur(); return sym + (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : Number(v).toFixed(2)); };
  const today = () => { const d = new Date(); return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); };
  const ymd = d => d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  // Model replies are Markdown; the design shows plain text, so drop the markup characters.
  const plain = t => String(t || '').replace(/```[a-z]*\n?/gi, '').replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1').replace(/`([^`]+)`/g, '$1').replace(/^#{1,6}\s+/gm, '').replace(/^\s*[-*]\s+/gm, '• ');
  const firstLine = (s, n = 90) => { const l = String(s || '').trim().split('\n')[0]; return l.length > n ? l.slice(0, n - 1) + '…' : l; };

  // A running task's live line (the worker's "Reading x", "Searching for y" - see describe_tool) split into
  // a verb for the chat's typing indicator and what it's on. No line yet, or between steps, reads "Thinking".
  const LIVE_VERBS = [[/^Running /, 'Running'], [/^Reading /, 'Reading'], [/^Editing /, 'Editing'], [/^Writing /, 'Writing'],
    [/^Searching the web for /, 'Searching the web'], [/^Searching for /, 'Searching'], [/^Looking for files matching /, 'Looking for files'],
    [/^Listing /, 'Looking around'], [/^Fetching /, 'Fetching'], [/^Delegating: /, 'Delegating'], [/^Updating its plan/, 'Planning'],
    [/^Waiting for your OK: /, 'Waiting for you'], [/^You approved · /, 'Resuming'], [/^Claude account: /, 'Starting up'], [/^Using /, 'Using']];
  const liveStep = (text, dot) => { const t = String(text || '').trim(); let verb = 'Working', what = t;
    if (!t || /^(Thinking|Working|Getting started)\b/i.test(t)) { verb = 'Thinking'; what = ''; }
    else { const m = LIVE_VERBS.find(([re]) => re.test(t)); if (m) { verb = m[1]; what = t.replace(m[0], ''); } }
    return { you:false, bot:false, step:false, live:true, verb, what:firstLine(what, 140), text:t, dot:dot || C.teal }; };
  // A running task the worker's stuck watch flagged (worker.flag_stuck): no output for a while, not paused, not waiting on you.
  const isStuck = t => !!(t && t.status === 'running' && t.stuck_reason && !t.paused);
  const stuckFor = t => dur(Date.now() - (parseT(t.stuck_since) || new Date()).getTime());
  // The live line for a running task: liveStep, or a red "Stuck" one when it's gone quiet.
  const taskLive = t => (isStuck(t) ? { ...liveStep(t.current_step, C.err), verb:'Stuck', what:'no output for ' + stuckFor(t) + ' · stop or retry it', text:t.stuck_reason } : liveStep(t.current_step));

  // ---------------------------------------------------------------- domain helpers
  const projectById = id => store.projects.find(p => p.id === id) || null;
  const projColor = p => (p ? (p.is_general ? C.green : p.color || PALETTE[(p.id || 0) % PALETTE.length]) : C.green);
  const projName = p => (p ? p.name : 'General');
  // The address names a project, not its number: #/projects/the-den/chats. Two projects with the same name get "-<id>" on the end.
  const slugify = n => String(n || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  const projSlug = p => { const b = slugify(p && p.name); if (!b) return String(p.id); return store.projects.some(q => q.id !== p.id && slugify(q.name) === b) ? b + '-' + p.id : b; };
  // A project from the address: its name, or its number (older links, notifications). null when no project matches.
  const projectFromSlug = key => { const k = decodeURIComponent(String(key || '')).toLowerCase(); if (!k) return null;
    return store.projects.find(p => projSlug(p) === k) || (/^\d+$/.test(k) ? projectById(+k) : null) || null; };
  // ---------------------------------------------------------------- workspaces (working areas in this Den)
  // The switcher shows one area at a time (or all). A project's category is its area; General is in every one.
  // The choice is per device. Approvals are never hidden by it - they always need you.
  const AREA_KEY = 'den.area';
  const DEFAULT_AREAS = [{ id: 'work', name: 'Work', color: '#7FB2FF' }, { id: 'personal', name: 'Personal', color: '#FF7B5C' }];
  const areas = () => { const a = need('areas', '/den/areas', 60000); return Array.isArray(a) && a.length ? a : DEFAULT_AREAS; };
  const area = () => { let a = 'all'; try { a = localStorage.getItem(AREA_KEY) || 'all'; } catch (e) {} return a === 'all' || areas().some(x => x.id === a) ? a : 'all'; };
  const setArea = id => { try { localStorage.setItem(AREA_KEY, id || 'all'); } catch (e) {} emit(); };
  const areaById = id => areas().find(x => x.id === id) || null;
  const inArea = p => { const a = area(); return a === 'all' || !p || !!p.is_general || (p.category || 'personal') === a; };
  const taskInArea = t => area() === 'all' || inArea(projectById(t.project_id));
  const allVisibleProjects = () => store.projects.filter(p => p.active !== 0 && p.active !== false && !p.hidden);
  const visibleProjects = () => allVisibleProjects().filter(inArea);
  const generalProject = () => store.projects.find(p => p.is_general) || null;
  const taskTitle = t => (t ? t.title || firstLine(t.prompt, 80) || ('Task #' + t.id) : '');
  const modelById = id => store.models.find(m => m.id === id) || null;
  const modelLabel = id => { if (!id || id === 'auto') return 'Auto'; const m = modelById(id); return m ? m.label : id; };
  const laneLabel = t => (t.lane === 'agent' ? 'Claude Code' : modelLabel(t.model_id) || (t.lane === 'assistant' ? 'assistant' : 'API'));
  const PRIO = { 2: 'Urgent', 1: 'High', 0: 'Normal', '-1': 'Low' };
  const PRIO_N = { Urgent: 2, High: 1, Normal: 0, Low: -1 };
  const prioLabel = n => PRIO[n] || (n > 2 ? 'Urgent' : n < -1 ? 'Low' : 'Normal');
  const pendingApprovals = () => (store.approvals || []).filter(a => a.status === 'pending');
  const taskById = id => store.tasks.find(t => t.id === id) || null;
  const roots = () => store.tasks.filter(t => !t.thread_id && taskInArea(t));
  const running = () => store.tasks.filter(t => t.status === 'running' && taskInArea(t)).sort((a, b) => (a.lane === 'agent' ? 0 : 1) - (b.lane === 'agent' ? 0 : 1));
  const queued = () => store.tasks.filter(t => t.status === 'queued' && taskInArea(t)).sort((a, b) => (a.queue_pos ?? 1e12) - (b.queue_pos ?? 1e12) || a.id - b.id);
  // Runs that ended by asking you something (awaiting_reply) aren't finished - they wait for your answer.
  const waitingOnYou = () => store.tasks.filter(t => t.status === 'done' && t.awaiting_reply && taskInArea(t));
  const finished = () => store.tasks.filter(t => ['done', 'error', 'needs_review'].includes(t.status) && !t.awaiting_reply && taskInArea(t)).sort((a, b) => (b.finished_at || b.created_at || '').localeCompare(a.finished_at || a.created_at || ''));
  const accounts = () => (store.settings && store.settings.claude_accounts) || [];
  const acctName = a => (a ? a.alias || (a.kind === 'mac' ? a.org || 'Mac login' : a.label || a.email || 'Account') : '');
  const acctWin = (a, k) => { const u = a && a.usage && a.usage.windows ? a.usage.windows[k] : null; return u || null; };
  const pct = w => (w && w.utilization != null ? Math.round(w.utilization <= 1 ? w.utilization * 100 : w.utilization) : 0);
  const usageColor = n => (n >= 95 ? C.violet : n >= 80 ? C.amber : C.teal);
  const resetLabel = (iso, weekly) => { const d = parseT(iso); if (!d) return '—'; return (weekly ? d.toLocaleDateString([], { weekday: 'short' }) + ' ' : '') + clock(iso); };

  // A task as the design's queue row expects it.
  function row(t, extra) {
    const p = projectById(t.project_id);
    const st = t.status;
    const stuck = isStuck(t);
    const sc = st === 'error' || stuck ? C.err : st === 'needs_review' ? C.blue : st === 'running' ? C.teal : st === 'cancelled' ? '#93A1B0' : C.green;
    return {
      id: '#' + t.id, tid: t.id, title: taskTitle(t), project: projName(p), pid: t.project_id, pc: projColor(p),
      l: t.lane || 'agent', lane: laneLabel(t), prio: prioLabel(t.priority || 0),
      meta: stuck ? 'Stuck · ' + stuckFor(t) : st === 'running' ? dur(Date.now() - (parseT(t.started_at) || new Date()).getTime()) : st === 'needs_review' ? 'review' : st === 'queued' ? '' : dur(t.duration_ms),
      step: t.current_step || '', sc, st: tint(sc, 0.14), status: st, stuck, task: t, ...(extra || {}),
    };
  }

  // Pictures (uploads) need the API key, so <img> can't load them directly: fetch once, show a blob URL.
  const imgCache = {};
  function imgUrl(id) {
    if (!id) return '';
    const c = imgCache[id]; if (c) return c.url;
    imgCache[id] = { url: '' };
    fetch(base() + '/uploads/' + encodeURIComponent(id), { headers: { 'X-API-Key': Den.key() } })
      .then(r => (r.ok ? r.blob() : null)).then(b => { if (b) { imgCache[id].url = URL.createObjectURL(b); emit(); } }).catch(() => {});
    return '';
  }

  // ---------------------------------------------------------------- conversations: what's new, where to scroll
  // Like the big messengers: a chat opens at the first reply you haven't seen (under a "New messages" line), or at
  // the bottom; it follows new replies while you're at the bottom, and offers "↓ new" when you've scrolled up.
  // What you've seen is remembered per chat on this device (the time of the newest reply you were shown).
  const SEEN_KEY = 'den.seen';
  const seenMap = () => { try { return JSON.parse(localStorage.getItem(SEEN_KEY) || '{}'); } catch (e) { return {}; } };
  const replyAt = t => (t.result_summary || t.error_message) ? (t.finished_at || t.started_at || t.created_at || '') : '';
  // The first reply in this chat newer than what you saw last time (null = nothing new, or never opened here).
  function firstUnread(rootId, tasks) {
    const seen = seenMap()[rootId];
    const list = (tasks || []).filter(t => replyAt(t));
    if (seen) return (list.find(t => replyAt(t) > seen) || {}).id || null;
    return (list.find(t => t.kind === 'message' && !t.read_at) || {}).id || null;     // never opened here: the Den's own read flag
  }
  function markSeen(rootId, tasks) {
    const newest = (tasks || []).map(replyAt).filter(Boolean).sort().pop(); if (!newest) return;
    const m = seenMap(); if ((m[rootId] || '') >= newest) return;
    m[rootId] = newest; const keys = Object.keys(m); if (keys.length > 400) keys.slice(0, keys.length - 400).forEach(k => delete m[k]);
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(m)); } catch (e) {}
  }
  // Call after every render with the chat's scroll box and which chat it shows. Bubbles carry data-msg; the first
  // new one data-new-start="1". onJump(n) is told how many replies arrived below the visible part (0 = none).
  function chatScroll(el, key, { top = 0, onJump, onReply } = {}) {
    if (!el || key == null) return;
    const st = el.__denChat || (el.__denChat = { key: null });
    if (!st.bound) { st.bound = true; el.addEventListener('scroll', () => { st.near = el.scrollHeight - el.scrollTop - el.clientHeight < 90; if (st.near && st.jump) { st.jump = 0; st.onJump && st.onJump(0); } }, { passive: true });
      const unpin = () => { st.pin = 0; }; el.addEventListener('wheel', unpin, { passive: true }); el.addEventListener('touchstart', unpin, { passive: true }); }
    st.onJump = onJump;
    const msgs = el.querySelectorAll('[data-msg]').length, replies = el.querySelectorAll('[data-reply]').length;
    if (st.key !== key) { st.key = key; st.opened = false; st.jump = 0; onJump && onJump(0); }
    if (!st.opened) {
      if (!msgs) return;                                               // the chat hasn't loaded yet
      st.opened = true; st.msgs = msgs; st.replies = replies;
      const mark = el.querySelector('[data-new-start="1"]');
      el.scrollTop = mark ? Math.max(0, mark.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop - top - 10) : el.scrollHeight;
      st.near = el.scrollHeight - el.scrollTop - el.clientHeight < 90; st.h = el.scrollHeight;
      return;
    }
    if (msgs > st.msgs || el.scrollHeight > st.h + 4) {
      const added = Math.max(0, msgs - st.msgs);
      const pinned = st.pin > Date.now(); if (pinned && added) st.pin = 0;              // you just sent: follow to your message even if you'd scrolled up
      if (st.near || pinned) { el.scrollTop = el.scrollHeight; st.near = true; if (replies > st.replies) onReply && onReply(replies - st.replies); }
      else if (added) { st.jump = (st.jump || 0) + added; onJump && onJump(st.jump); }
    }
    st.msgs = msgs; st.replies = replies; st.h = el.scrollHeight;
  }
  // Call when you send a message: jumps to the bottom now, and again when your message lands (for up to 15s).
  const chatSent = el => { if (!el) return; el.scrollTop = el.scrollHeight; const st = el.__denChat; if (st) { st.pin = Date.now() + 15000; st.near = true; if (st.jump) { st.jump = 0; st.onJump && st.onJump(0); } } };
  const chatToEnd = el => { if (!el) return; el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }); const st = el.__denChat; if (st) { st.jump = 0; st.near = true; st.onJump && st.onJump(0); } };

  // What an approval wants to run, in words ("git push origin main", or an assistant's sentence).
  function commandOf(a) {
    try {
      const x = JSON.parse(a.tool_input);
      if (x && typeof x === 'object' && x.describe) return String(x.describe);
      if (x && typeof x === 'object') return String(x.command ?? x.url ?? x.file_path ?? JSON.stringify(x));
    } catch (e) {}
    return a.tool_input || '';
  }

  // ---------------------------------------------------------------- layout prefs
  // Saved on the server (settings.ui_prefs) so the layout follows you across
  // devices; a local copy makes the first paint right before the server answers.
  const PREFS_LOCAL = 'den.prefs';
  let prefsCache = (() => { try { return JSON.parse(localStorage.getItem(PREFS_LOCAL) || '{}'); } catch (e) { return {}; } })();
  let prefsDirty = false, prefsTimer = null;
  function prefs() {
    const sp = store.settings && store.settings.ui_prefs;
    if (!prefsDirty && sp && typeof sp === 'object' && JSON.stringify(sp) !== JSON.stringify(prefsCache) && Object.keys(sp).length) {
      prefsCache = { ...sp };
      try { localStorage.setItem(PREFS_LOCAL, JSON.stringify(prefsCache)); } catch (e) {}
    }
    return prefsCache;
  }
  function setPref(k, v) {
    prefsCache = { ...prefsCache, [k]: v }; prefsDirty = true;
    try { localStorage.setItem(PREFS_LOCAL, JSON.stringify(prefsCache)); } catch (e) {}
    clearTimeout(prefsTimer);
    prefsTimer = setTimeout(() => {
      api('/settings', { method: 'PATCH', body: { ui_prefs: prefsCache } })
        .then(s => { if (s && store.settings) store.settings.ui_prefs = s.ui_prefs || prefsCache; })
        .catch(() => {}).finally(() => { prefsDirty = false; });
    }, 600);
    emit();
  }

  // ---------------------------------------------------------------- shortcuts (rebindable in Settings)
  const SHORTCUT_DEFAULTS = { 'Command palette': '⌘K', 'New task': 'N', 'Approve next': 'A', 'Deny next': 'D', 'Focus view': 'F', 'Toggle sidebar': '[' };
  const keyFor = name => (prefs().shortcuts || {})[name] || SHORTCUT_DEFAULTS[name] || '';
  const shortcut = (name, key) => { const k = keyFor(name); return !!k && k !== '—' && k.length === 1 && k.toLowerCase() === String(key).toLowerCase(); };

  // ---------------------------------------------------------------- workspaces
  // Other Dens (another Mac) you can jump to: just a name and an address, kept
  // in the layout prefs so every device lists them. Switching opens that Den's
  // own dashboard - its API key stays on its own page (asked once there), and
  // no Den has to accept requests from another's page.
  const workspaces = () => (prefs().workspaces || []);
  const workspace = () => null;   // this page always shows the Den it is served from
  // A workspace is another Den, on another Mac - never this one under another address.
  function workspaceUrl(url) {
    url = String(url || '').trim().replace(/\/+$/, '').replace(/\/dashboard(\/new)?$/, '');
    if (!/^https?:\/\//.test(url)) url = 'http://' + url;
    let u; try { u = new URL(url); } catch (e) { throw new Error('That isn’t a web address'); }
    const here = new Set([location.host, ...((need('den-conn') || {}).services || []).filter(x => x.id === 'tailscale' && x.url).map(x => new URL(x.url).host)]);
    const port = u.port || (u.protocol === 'https:' ? '443' : '80');
    if (here.has(u.host) || ((u.hostname === 'localhost' || u.hostname === '127.0.0.1') && port === (location.port || '80')))
      throw new Error('That’s this Den itself - a workspace is another Den on another Mac');
    return url;
  }
  function addWorkspace(name, url) {
    try { url = workspaceUrl(url); } catch (e) { return Promise.reject(e); }
    const w = { id: 'w' + Date.now().toString(36), name: name || url.replace(/^https?:\/\//, ''), url };
    setPref('workspaces', [...workspaces(), w]);
    return Promise.resolve(w);
  }
  function updateWorkspace(id, patch) {
    const list = workspaces(); const w = list.find(x => x.id === id); if (!w) return Promise.reject(new Error('No such workspace'));
    const next = { ...w };
    if (patch.name != null) { const n = String(patch.name).trim(); if (!n) return Promise.reject(new Error('Give it a name')); next.name = n.slice(0, 40); }
    if (patch.url != null) { try { next.url = workspaceUrl(patch.url); } catch (e) { return Promise.reject(e); } }
    setPref('workspaces', list.map(x => (x.id === id ? next : x)));
    return Promise.resolve(next);
  }
  function useWorkspace(id) {
    const w = workspaces().find(x => x.id === id);
    if (w) window.location.href = w.url + '/dashboard';
  }
  const removeWorkspace = id => setPref('workspaces', workspaces().filter(w => w.id !== id));
  const base = () => '';

  // ---------------------------------------------------------------- meeting notes
  // A note tied to a calendar item (pad_notes.event_id). A meeting's "Meeting notes" button opens its
  // newest note, or starts one; the Notes page picks up focusNote and opens it.
  const MEETING_KINDS = ['meeting', 'demo', 'presentation'];
  const meetingNotes = eventId => ((need('pad', '/pad', 15000) || {}).items || []).filter(n => n.event_id === eventId);
  function openNote(id) { Den.focusNote = id; Den.go('notes'); emit(); }
  function openMeetingNote(ev) {   // ev: { id, title, project_id } of a timeline event
    // Asked fresh, so a note made on another device isn't doubled.
    return api('/pad').then(r => {
      const have = ((r && r.items) || []).filter(n => n.event_id === ev.id);
      if (have.length) return have[0];   // newest first
      return api('/pad', { method: 'POST', body: { title: ev.title || 'Meeting notes', body: '', project_id: ev.project_id || null, event_id: ev.id } });
    }).then(n => { invalidate('pad'); openNote(n.id); return n; });
  }

  // ---------------------------------------------------------------- usage tracking (product analytics)
  // Records ONLY which section is open and for how long (active seconds while the window is visible),
  // plus a session count. No content ever leaves here - just section ids and times. It is sent to the
  // den_usage module, which keeps daily totals. If that module isn't installed the calls 404 and are
  // ignored. Never blocks or breaks the UI; every failure is swallowed.
  const usage = (() => {
    const pend = {};                  // section -> { opens, seconds }
    let cur = null, since = 0, timer = null, sessionNew = true;
    const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden';
    const bucket = s => (pend[s] || (pend[s] = { opens: 0, seconds: 0 }));
    function addElapsed() {
      if (cur && since && visible()) { const secs = Math.round((Date.now() - since) / 1000); if (secs > 0 && secs < 7200) bucket(cur).seconds += secs; }
      since = Date.now();
    }
    function track(section) {
      if (!section || typeof section !== 'string' || section === cur) return;
      addElapsed(); cur = section; since = Date.now(); bucket(section).opens += 1;
      if (!timer) timer = setTimeout(flush, 15000);
    }
    function flush() {
      addElapsed(); clearTimeout(timer); timer = null;
      const events = Object.entries(pend).filter(([, v]) => v.opens || v.seconds).map(([section, v]) => ({ section, opens: v.opens, seconds: v.seconds }));
      for (const k in pend) delete pend[k];
      if (store.needKey || store.offline) return;              // not signed in / unreachable: hold until next time
      if (!events.length && !sessionNew) return;
      const payload = { events, new_session: sessionNew }; sessionNew = false;
      api('/modules/commands/den-usage-track', { method: 'POST', body: { arg: JSON.stringify(payload) } }).catch(() => {});
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', () => { addElapsed(); since = Date.now(); if (document.visibilityState === 'hidden') flush(); });
      window.addEventListener('pagehide', flush);
      setInterval(() => { if (cur) flush(); }, 60000);
    }
    return { track, flush };
  })();

  const Den = {
    C, PALETTE, tint, store, api, refresh, start, need, load, invalidate, act, emit, track: usage.track,
    commandOf, imgUrl, firstUnread, markSeen, chatScroll, chatToEnd, chatSent, lastError: () => errors[errors.length - 1] || null, prefs, setPref, keyFor, shortcut, SHORTCUT_DEFAULTS, workspaces, workspace, addWorkspace, updateWorkspace, useWorkspace, removeWorkspace, base,
    auth, key: () => { try { return localStorage.getItem(KEY_STORAGE) || ''; } catch (e) { return ''; } },
    setKey: k => { try { localStorage.setItem(KEY_STORAGE, k.trim()); } catch (e) {} store.needKey = false; start(); },
    on: fn => { subs.add(fn); return () => subs.delete(fn); },
    // Subscribe a DC component to store changes for its lifetime.
    use(comp) { const off = Den.on(() => comp.forceUpdate()); const prev = comp.componentWillUnmount ? comp.componentWillUnmount.bind(comp) : null; comp.componentWillUnmount = () => { off(); prev && prev(); }; return off; },
    say: (t, s, c) => console.info('[den]', t, s || ''),    // replaced by the shell's toast
    go: () => {},                                           // replaced by the shell's router
    openChat: () => {},                                     // replaced by the shell's chat panel
    focusNote: null, openNote, openMeetingNote, meetingNotes, MEETING_KINDS,
    ago, clock, dayClock, dur, money, cur, currency, CURRENCIES, today, ymd, pad2, parseT, firstLine, plain, liveStep, isStuck, stuckFor, taskLive,
    projectById, projColor, projName, projSlug, projectFromSlug, visibleProjects, allVisibleProjects, areas, area, setArea, areaById, inArea, taskInArea, generalProject, taskTitle, taskById, modelById, modelLabel, laneLabel,
    prioLabel, PRIO_N, pendingApprovals, roots, running, queued, finished, waitingOnYou, accounts, acctName, acctWin, pct, usageColor, resetLabel, row,
  };
  window.Den = Den;

  // Dropdowns open on whichever side of their button has room. Each component's menus are
  // absolute boxes at top:calc(100% + Npx) (opens down) or bottom:calc(100% + Npx) (opens up);
  // when the designed side doesn't fit and the other side has more room, flip it. "Room" stops at
  // the window edge and at any scrolling panel the menu sits in (which would clip it). If neither
  // side fits it takes the roomier one and scrolls. Re-checked whenever the page redraws or scrolls.
  const EDGE = 8, orig = new WeakMap();
  const atCalc = v => { const m = /^calc\(100% \+ (\d+(?:\.\d+)?)px\)$/.exec(v || ''); return m ? +m[1] : null; };
  function placeMenu(el) {
    let o = orig.get(el);
    if (!o) { o = { top: el.style.top, bottom: el.style.bottom, maxHeight: el.style.maxHeight, overflowY: el.style.overflowY }; orig.set(el, o); }
    Object.assign(el.style, o);
    const btn = el.offsetParent;
    if (!btn) return;
    const down = atCalc(o.top) != null, gap = down ? atCalc(o.top) : atCalc(o.bottom);
    let top = EDGE, bottom = innerHeight - EDGE;
    for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
      if (getComputedStyle(p).overflowY === 'visible') continue;
      const r = p.getBoundingClientRect();
      top = Math.max(top, r.top); bottom = Math.min(bottom, r.bottom);
    }
    const a = btn.getBoundingClientRect(), h = el.offsetHeight;
    const below = bottom - a.bottom - gap, above = a.top - top - gap;
    const flip = down ? h > below && above > below : h > above && below > above;
    if (flip) Object.assign(el.style, down ? { top: 'auto', bottom: `calc(100% + ${gap}px)` } : { top: `calc(100% + ${gap}px)`, bottom: 'auto' });
    const room = down !== flip ? below : above;
    if (h > room && room >= 80) Object.assign(el.style, { maxHeight: `${Math.floor(room)}px`, overflowY: 'auto' });
  }
  function placeMenus() {
    for (const el of document.querySelectorAll('[style*="calc(100% +"]')) {
      if (el.style.position !== 'absolute') continue;
      const o = orig.get(el) || el.style;
      if ((atCalc(o.top) != null) !== (atCalc(o.bottom) != null)) placeMenu(el);
    }
  }
  let placeSoon = false;
  const later = () => { if (!placeSoon) { placeSoon = true; requestAnimationFrame(() => { placeSoon = false; placeMenus(); }); } };
  new MutationObserver(placeMenus).observe(document.documentElement, { childList: true, subtree: true });
  addEventListener('scroll', later, true);
  addEventListener('resize', later);

  boot();
})();
