// Toasts + notification centre (top-right). Framework-free ES module, no build step.
// Spec: README.md › Toasts & notification centre. Styles: css/notifications.css.
// Pixel reference: reference/Workspace Prototype.dc.html (search "notifVals" / "mkNotif").
//
// Usage (see NOTIFICATIONS_PROMPT.md for full wiring):
//   const notify = new NotificationCenter({ root, bell, statusOf, onOpenTask, emptyArt, modelLabel });
//   notify.toast('Cancelled', { action: undo, actionLabel: 'Undo' });   // replaces store.ui.toast
//   subscribe(s => notify.ingest(s.data));                              // derives notifications from each poll
//
// Local addition (not in the handoff): approval notifications show what Octopus wants to run
// (describeApproval, clamped to 2 lines) with Deny / Approve right in the toast and the panel
// row while the request is still pending (approvalPending, onDecide).
import { icon } from './icons.js';
import { escapeHTML, money } from './format.js';
import { pushActive, setBadge, syncPush } from './push.js';

// ---------------------------------------------------------------- kinds
// needs = the uiStatus that keeps this notification "waiting on you" (shows the CTA + counts in "Needs you").
// ephemeral = toast only, never saved to the list.
export const KINDS = {
  done:     { c: '#6FCF97', icon: 'nCheck',  label: 'DONE' },
  answer:   { c: '#8FE0B0', icon: 'nBubble', label: 'ANSWERED' },
  approval: { c: '#FFB547', icon: 'nClock',  label: 'NEEDS APPROVAL', needs: 'approval',     cta: 'Review request' },
  approved: { c: '#FFB547', icon: 'nCheck',  label: 'APPROVED' },
  stop:     { c: '#FF8FA2', icon: 'close',   label: 'STOPPED' },
  deleted:  { c: '#FF8FA2', icon: 'trash',   label: 'DELETED' },
  restored: { c: '#6FCF97', icon: 'restart', label: 'RESTORED' },
  queue:    { c: '#FF7B5C', icon: 'nQueue',  label: 'QUEUED' },
  error:    { c: '#FF5470', icon: 'nAlert',  label: 'FAILED',         needs: 'error',        cta: 'See what broke' },
  review:   { c: '#7FB2FF', icon: 'nEye',    label: 'NEEDS REVIEW',   needs: 'needs_review', cta: 'Review changes' },
  limit:    { c: '#A58BFF', icon: 'nMoon',   label: 'RATE LIMIT' },
  weekly:   { c: '#FFB547', icon: 'nAlert',  label: 'WEEKLY LIMIT' },
  polish:   { c: '#FF7B5C', icon: 'nSpark',  label: 'POLISH', ephemeral: true },
  add:      { c: '#6FCF97', icon: 'nPlus',   label: 'PROJECT' },
  info:     { c: '#93A1B0', icon: 'nInfo',   label: 'NOTE',   ephemeral: true },
};
const kindOf = k => KINDS[k] || KINDS.info;
const hexA = (hx, a) => { const n = parseInt(hx.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };
const kvars = K => `--k:${K.c};--kt:${hexA(K.c, 0.14)}`;

export function when(at, now = Date.now()) {
  const d = (now - at) / 1000;
  if (d < 45) return 'now';
  if (d < 3600) return Math.round(d / 60) + 'm ago';
  if (d < 86400) return Math.round(d / 3600) + 'h ago';
  return Math.round(d / 86400) + 'd ago';
}

// Toast copy ("Done · Fix RSS · $0.27") → kind from the first segment.
export function toastKind(text) {
  const w = text.split(' · ')[0];
  if (/^Done/.test(w)) return 'done';
  if (/^(Answered|→|Asked again)/.test(w)) return 'answer';
  if (/^Approved/.test(w)) return 'approved';
  if (/^(Denied|Cancelled|Stopped)/.test(w)) return 'stop';
  if (/^(Deleted|Erased|Recycle bin emptied)/.test(w)) return 'deleted';
  if (/^Restored/.test(w)) return 'restored';
  if (/^Limit cleared/.test(w)) return 'limit';
  if (/^(Text polished|Already reads well)/.test(w)) return 'polish';
  if (/^Added /.test(w)) return 'add';
  if (/^(Queued|Moved to the top|Scheduled|Retry queued)/.test(w) || / in line$/.test(text)) return 'queue';
  return 'info';
}

// "Done · Fix RSS · $0.27" → { kicker: 'DONE', title: 'Fix RSS', sub: '$0.27' }
export function parseToast(text) {
  const kind = toastKind(text), segs = text.split(' · ');
  let kicker = null, title = segs[0], sub = segs.slice(1).join(' · ');
  const m = segs[0].match(/^(Done|Answered|Queued|Approved|Denied|Cancelled|Stopped|Deleted|Restored)(?:\s+(.+))?$/);
  if (m) {
    kicker = m[1].toUpperCase();
    if (m[2]) title = m[2];
    else if (segs.length > 1) { title = segs[1]; sub = segs.slice(2).join(' · '); }
  }
  if (title.startsWith('→ ')) { kicker = 'SENT'; title = title.slice(2); }
  title = title.charAt(0).toUpperCase() + title.slice(1);
  return { kind, kicker, title, sub };
}

// ---------------------------------------------------------------- persistence
const STORE = 'octopus-notifs', MAX = 60, MAX_TOASTS = 3;
const load = () => { try { return JSON.parse(localStorage.getItem(STORE)) || []; } catch { return []; } };

// System notifications. The API only exists in a secure context (localhost / https) - over plain-http
// Tailscale the phone relies on ActivitySmith instead. Over https (Tailscale Serve) allowing them also
// subscribes to Web Push (push.js), which reaches the Dock / Home Screen app even while it's closed;
// until then they only show while the dashboard is in a background tab.
const canSystemNotify = () => typeof Notification !== 'undefined' && window.isSecureContext;

export class NotificationCenter {
  /**
   * @param {Object} o
   * @param {HTMLElement} o.root        empty <div class="notif-root"> as a direct child of .app
   * @param {HTMLButtonElement} o.bell  empty <button id="bell"> in the topbar
   * @param {(taskId:number)=>string|null} o.statusOf   uiStatus(task) or null if gone
   * @param {(taskId:number)=>void} o.onOpenTask        open the approval pane or chat pane
   * @param {()=>string} [o.emptyArt]                   octopusSVG('sleep')
   * @param {(task:Object)=>string} [o.modelLabel]      model label for answer notifications
   * @param {(approval:Object)=>string} [o.describeApproval]   what an approval wants to run
   * @param {(approvalId:number)=>boolean} [o.approvalPending] still waiting for a decision?
   * @param {(approvalId:number, approved:boolean)=>any} [o.onDecide]   Approve / Deny; buttons only show when given
   * @param {(taskId:number)=>boolean} [o.isViewing]   that chat is on screen right now: it shows the news itself, so no toast
   */
  constructor({ root, bell, statusOf, onOpenTask, emptyArt = () => '', modelLabel = () => '',
    describeApproval = () => '', approvalPending = null, onDecide = null, isViewing = () => false }) {
    Object.assign(this, { root, bell, statusOf, onOpenTask, emptyArt, modelLabel, describeApproval, approvalPending, onDecide, isViewing });
    this.items = load();
    this.open = false; this.tab = 'all'; this.seq = Date.now();
    this.actions = new Map(); this.timers = new Map(); this.prevRef = null; this.prevTasks = null;
    this.decided = new Set();   // approval ids decided from here, so their buttons go away at once
    root.classList.add('notif-root');
    root.innerHTML = '<div class="toasts" aria-live="polite"></div><div class="notif-layer"></div>';
    this.stack = root.firstChild; this.layer = root.lastChild;
    root.addEventListener('click', e => this.onClick(e));
    root.addEventListener('keydown', e => {   // rows holding Approve / Deny are role="button" divs
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('div.nrow[data-act="row"]')) { e.preventDefault(); this.openItem(e.target.dataset.id); }
    });
    bell.classList.add('icon-btn', 'bell');
    bell.setAttribute('aria-label', 'Notifications'); bell.title = 'Notifications';
    bell.addEventListener('click', () => (this.open ? this.close() : this.show()));
    this.renderBell();
    setInterval(() => this.open && this.renderPanel(), 30000);
    syncPush();   // already allowed: keep this browser's push subscription current
  }

  // ---------------------------------------------------------------- public
  /** Replaces the old store.ui.toast. Every existing toast string keeps working. */
  // keep: the action button stays on the row in the panel too, until used (e.g. Undo on a delete).
  toast(text, { action, actionLabel, taskId, keep = false } = {}) {
    const p = parseToast(text);
    return this.add({ ...p, taskId, actionLabel: action ? actionLabel : undefined, keepAction: !!(action && keep) }, { toast: true, action });
  }

  /** Add one notification. key dedupes (poll-derived items use it so reloads don't repeat). */
  add(n, { toast = true, action } = {}) {
    if (n.key && this.items.some(x => x.key === n.key)) return null;
    const K = kindOf(n.kind);
    // The chat it's about is open in front of you: it lands there, so keep it in the list (already read) but don't toast.
    // Toasts carrying an action (Undo) still show - the button only lives on the toast.
    if (toast && !action && n.taskId != null && this.isViewing(n.taskId)) { toast = false; n = { ...n, read: true }; }
    const item = { id: 'n' + ++this.seq, at: Date.now(), read: !!K.ephemeral, ...n, life: action ? 10 : n.approvalId && this.onDecide ? 12 : 5 };
    if (action) this.actions.set(item.id, action);
    this.items = [item, ...this.items].slice(0, MAX);
    this.save();
    if (toast && !this.open) this.showToast(item);
    if (toast && !K.ephemeral && document.hidden && !pushActive()) this.systemNotify(item);   // pushed already: don't show it twice
    this.renderBell(); if (this.open) this.renderPanel();
    return item.id;
  }

  systemNotify(n) {
    if (!canSystemNotify() || Notification.permission !== 'granted') return;
    const K = kindOf(n.kind);
    try {
      const sn = new Notification(`${n.kicker || K.label} · ${n.title}`, { body: n.sub || '', tag: n.key || n.id, silent: false });
      sn.onclick = () => { window.focus(); sn.close(); this.openItem(n.id); };
    } catch {}
  }

  askSystemPermission() {
    if (!canSystemNotify()) return;
    Promise.resolve(Notification.requestPermission()).then(p => p === 'granted' && syncPush()).finally(() => this.renderPanel());
  }

  /** Call after every poll with store.data. First call backfills silently; later calls toast new events. */
  ingest(data) {
    if (!data || data === this.prevRef) return;
    const first = !this.prevTasks;
    const proj = id => data.projects.find(p => p.id === id)?.name || '';
    const now = Date.now();
    setBadge(data.approvals.filter(x => x.status === 'pending').length);
    for (const a of data.approvals.filter(x => x.status === 'pending')) {
      const t = data.tasks.find(x => x.id === a.task_id); if (!t) continue;
      this.add({ key: 'ap:' + a.id, kind: 'approval', title: t.prompt, sub: proj(t.project_id) + ' · waiting for your OK', taskId: t.thread_id || t.id, at: Date.parse(a.created_at) || now,
        approvalId: a.id, detail: this.describeApproval(a) }, { toast: !first });
    }
    // Weekly-limit early warning: computed server-side, one notification per level per week.
    const wp = data.settings && data.settings.notify_weekly_pace !== false && data.settings.weekly_pace;
    if (wp && wp.level) this.add({ key: `weekly:${wp.resets_at.slice(0, 13)}:${wp.level}`, kind: 'weekly', title: wp.title, sub: wp.message }, { toast: !first });
    for (const t of data.tasks) {
      const was = this.prevTasks?.get(t.id);
      if (!first && was === t.status) continue;
      const kind = t.status === 'done' ? ((t.kind || (t.lane === 'api' ? 'message' : 'task')) === 'message' ? 'answer' : 'done') : t.status === 'error' ? 'error'
        : t.status === 'needs_review' ? 'review' : t.status === 'rate_limited' ? 'limit' : null;
      if (!kind) continue;
      const at = Date.parse(t.finished_at || t.started_at || t.created_at) || now;
      if (first && now - at > 86400e3 && !kindOf(kind).needs) continue;   // backfill: last 24h + anything still waiting
      const p = proj(t.project_id);
      const sub = kind === 'done' ? p + (t.cost_usd ? ' · ' + money(t.cost_usd) : '')
        : kind === 'answer' ? [p, this.modelLabel(t)].filter(Boolean).join(' · ')
        : kind === 'error' ? p + (t.error_message ? ' · ' + t.error_message : '')
        : kind === 'limit' ? p + (t.resume_at ? ' · resumes ' + new Date(t.resume_at).toTimeString().slice(0, 5) : '')
        : p;
      this.add({ key: `t:${t.id}:${t.status}:${t.finished_at || ''}`, kind, title: t.prompt, sub, taskId: t.thread_id || t.id, at,
        read: first && !kindOf(kind).needs && now - at > 3600e3 }, { toast: !first });
    }
    this.dropResolvedApprovals(data);
    this.prevRef = data;
    this.prevTasks = new Map(data.tasks.map(t => [t.id, t.status]));
  }

  /** An approval decided anywhere (task page, another device, a push action) must not leave its toast up. */
  dropResolvedApprovals(data) {
    const pending = new Set(data.approvals.filter(a => a.status === 'pending').map(a => a.id));
    let changed = false;
    for (const n of this.items) {
      if (!n.approvalId || pending.has(n.approvalId)) continue;
      this.dropToast(n.id);
      if (!n.read) { n.read = true; changed = true; }
    }
    if (changed) { this.save(); this.renderBell(); if (this.open) this.renderPanel(); }
  }

  isOpen() { return this.open; }
  show() {
    this.open = true; this.tab = 'all';
    [...this.stack.children].forEach(el => this.dropToast(el.dataset.id));   // opening hides live toasts
    this.renderBell(); this.renderPanel();
  }
  close() { this.open = false; this.markAllRead(false); this.renderPanel(); this.renderBell(); }   // closing marks everything read
  markAllRead(render = true) { this.items = this.items.map(x => (x.read ? x : { ...x, read: true })); this.save(); if (render) { this.renderBell(); this.renderPanel(); } }
  clearAll() { this.items = this.items.filter(x => this.isLive(x)); this.save(); this.renderBell(); this.renderPanel(); }   // keeps ones still waiting on you
  clearRead() { this.items = this.items.filter(x => !x.read || this.isLive(x)); this.save(); this.renderBell(); this.renderPanel(); }

  // ---------------------------------------------------------------- internals
  save() { try { localStorage.setItem(STORE, JSON.stringify(this.items.slice(0, MAX))); } catch {} }
  isLive(n) { const K = kindOf(n.kind); return !!(K.needs && n.taskId && this.statusOf(n.taskId) === K.needs); }
  canDecide(n) {
    return !!(n.approvalId && this.onDecide && !this.decided.has(n.approvalId)
      && (this.approvalPending ? this.approvalPending(n.approvalId) : this.isLive(n)));
  }
  detailHTML(n) { return n.detail ? `<span class="nkind-detail"><span>${escapeHTML(n.detail)}</span></span>` : ''; }
  decideHTML(n) {
    return `<div class="ndecide"><button class="ndecide__btn" data-act="decide" data-id="${n.id}" data-ok="0">Deny</button>`
      + `<button class="ndecide__btn ndecide__btn--ok" data-act="decide" data-id="${n.id}" data-ok="1">Approve</button></div>`;
  }
  decide(id, ok) {
    const n = this.items.find(x => x.id === id); if (!n || !this.canDecide(n)) return;
    this.decided.add(n.approvalId);
    this.items = this.items.map(x => (x.id === id ? { ...x, read: true } : x)); this.save();
    this.dropToast(id); this.renderBell(); if (this.open) this.renderPanel();
    Promise.resolve(this.onDecide(n.approvalId, ok)).finally(() => { if (this.open) this.renderPanel(); });
  }
  listed() { return this.items.filter(n => !kindOf(n.kind).ephemeral); }

  renderBell() {
    const unread = this.listed().filter(n => !n.read).length;
    this.bell.classList.toggle('is-open', this.open);
    this.bell.innerHTML = icon('bell', 18) + (unread ? `<span class="bell__badge">${unread > 9 ? '9+' : unread}</span>` : '');
  }

  showToast(n) {
    const K = kindOf(n.kind), el = document.createElement('div');
    el.className = 'toast' + (n.taskId ? ' is-link' : '');
    el.dataset.act = 'toast-open'; el.dataset.id = n.id;
    el.style.cssText = kvars(K) + `;--life:${n.life}s`;
    el.innerHTML = `<div class="nkind-ic">${icon(K.icon, 18, { strokeWidth: 1.8 })}</div>`
      + `<div class="nkind-body"><span class="nkind-kicker">${escapeHTML(n.kicker || K.label)}</span><span class="toast__title">${escapeHTML(n.title)}</span>${n.sub ? `<span class="nkind-sub">${escapeHTML(n.sub)}</span>` : ''}`
      + `${this.detailHTML(n)}${this.canDecide(n) ? this.decideHTML(n) : ''}</div>`
      + `<div class="toast__actions">${n.actionLabel ? `<button class="toast__action" data-act="toast-act" data-id="${n.id}">${escapeHTML(n.actionLabel)}</button>` : ''}`
      + `<button class="toast__close" data-act="toast-close" data-id="${n.id}" aria-label="Dismiss">${icon('close', 14, { strokeWidth: 2 })}</button></div>`
      + '<span class="toast__bar"></span>';
    this.stack.appendChild(el);                                   // newest at the bottom of the stack
    while (this.stack.children.length > MAX_TOASTS) this.dropToast(this.stack.firstChild.dataset.id);
    this.timers.set(n.id, setTimeout(() => this.dropToast(n.id), n.life * 1000));
  }
  dropToast(id) {
    clearTimeout(this.timers.get(id)); this.timers.delete(id);
    this.stack.querySelector(`[data-id="${id}"]`)?.remove();
  }

  renderPanel() {
    if (!this.open) { this.layer.innerHTML = ''; this.panel = null; return; }
    if (!this.panel) {
      this.layer.innerHTML = '<div class="notif-scrim" data-act="close"></div><div class="notif" role="dialog" aria-label="Notifications"></div>';
      this.panel = this.layer.lastChild;
    }
    const desk = !!this.root.closest('.app--desk');
    const list = this.listed(), unread = list.filter(n => !n.read).length, needs = list.filter(n => this.isLive(n));
    const groups = this.tab === 'needs'
      ? (needs.length ? [['WAITING ON YOU', needs]] : [])
      : [['NEW', list.filter(n => !n.read)], ['EARLIER', list.filter(n => n.read)]].filter(g => g[1].length);
    const tab = (k, l, n) => `<button class="notif__tab${this.tab === k ? ' is-active' : ''}" data-act="tab" data-tab="${k}">${l}${n ? `<span class="notif__tab-n${k === 'needs' ? ' is-wait' : ''}">${n}</span>` : ''}</button>`;
    const scroll = this.panel.querySelector('.notif__list')?.scrollTop || 0;
    this.panel.innerHTML = `
      <div class="notif__head"><span class="notif__title">Notifications</span>${unread ? `<span class="notif__new">${unread} new</span>` : ''}<span class="notif__spacer"></span>
        ${unread ? `<button class="notif__link" data-act="mark">${desk ? 'Mark all read' : 'Read all'}</button>` : ''}
        ${list.some(n => !this.isLive(n)) ? '<button class="notif__link" data-act="clearall">Clear all</button>' : ''}
        <button class="icon-btn" data-act="close" aria-label="Close">${icon('close', 14, { strokeWidth: 1.8 })}</button></div>
      <div class="notif__tabs">${tab('all', 'All', list.length)}${tab('needs', 'Needs you', needs.length)}</div>
      <div class="notif__list">${groups.map(([l, items]) => `<span class="notif__group">${l}</span>${items.map(n => this.row(n)).join('')}`).join('')
        || `<div class="notif__empty"><div class="notif__empty-art">${this.emptyArt()}</div><span class="notif__empty-title">${this.tab === 'needs' ? 'Nothing waiting on you' : 'All caught up'}</span>
            <span class="notif__empty-sub">${this.tab === 'needs' ? 'Approvals, failures and reviews land here.' : "I'll ping you when something finishes or needs a decision."}</span></div>`}</div>
      <div class="notif__foot"><span class="notif__count">${list.length === 1 ? '1 notification' : list.length + ' notifications'}</span><span class="notif__spacer"></span>
        ${canSystemNotify() && Notification.permission === 'default' ? '<button class="notif__link" data-act="allow">Allow desktop alerts</button>' : ''}
        <button class="notif__link" data-act="clear">Clear read</button></div>`;
    this.panel.querySelector('.notif__list').scrollTop = scroll;
  }

  row(n) {
    const K = kindOf(n.kind), live = this.isLive(n), decide = this.canDecide(n);
    const act = n.keepAction && n.actionLabel && this.actions.has(n.id);   // the function only lives until the page reloads
    // A row holding Approve / Deny (or an Undo) can't itself be a <button>, so it becomes a focusable div.
    const tag = decide || act ? 'div' : 'button', a11y = decide || act ? ' role="button" tabindex="0"' : '';
    return `<${tag} class="nrow${n.read ? '' : ' is-unread'}" data-act="row" data-id="${n.id}"${a11y} style="${kvars(K)}">`
      + `<div class="nkind-ic">${icon(K.icon, 18, { strokeWidth: 1.8 })}</div>`
      + `<div class="nrow__body"><div class="nrow__top"><span class="nkind-kicker">${escapeHTML(n.kicker || K.label)}</span><span class="nrow__when">${when(n.at)}</span></div>`
      + `<span class="nrow__title">${escapeHTML(n.title)}</span>${n.sub ? `<span class="nkind-sub">${escapeHTML(n.sub)}</span>` : ''}${live || decide ? this.detailHTML(n) : ''}`
      + `${decide ? this.decideHTML(n) : live && K.cta ? `<span class="nrow__cta">${K.cta} →</span>` : ''}`
      + `${act ? `<div class="ndecide"><button class="ndecide__btn" data-act="toast-act" data-id="${n.id}">${escapeHTML(n.actionLabel)}</button></div>` : ''}</div><span class="nrow__dot"></span></${tag}>`;
  }

  openItem(id) {
    const n = this.items.find(x => x.id === id); if (!n) return;
    this.items = this.items.map(x => (x.id === id ? { ...x, read: true } : x)); this.save();
    this.dropToast(id);
    if (this.open) { this.open = false; this.renderPanel(); }
    this.renderBell();
    if (n.taskId && this.statusOf(n.taskId) != null) this.onOpenTask(n.taskId);
  }

  onClick(e) {
    const el = e.target.closest('[data-act]'); if (!el || !this.root.contains(el)) return;
    const { act, id } = el.dataset;
    if (act === 'decide') { e.stopPropagation(); this.decide(id, el.dataset.ok === '1'); }
    else if (act === 'toast-act') {
      e.stopPropagation(); const run = this.actions.get(id); this.actions.delete(id); this.dropToast(id); run?.();
      if (this.open) this.renderPanel();
    }
    else if (act === 'toast-close') { e.stopPropagation(); this.dropToast(id); }
    else if (act === 'toast-open') { if (this.items.find(x => x.id === id)?.taskId) this.openItem(id); }
    else if (act === 'row') this.openItem(id);
    else if (act === 'tab') { this.tab = el.dataset.tab; this.renderPanel(); }
    else if (act === 'mark') this.markAllRead();
    else if (act === 'clear') this.clearRead();
    else if (act === 'clearall') this.clearAll();
    else if (act === 'allow') this.askSystemPermission();
    else if (act === 'close') this.close();
  }
}
