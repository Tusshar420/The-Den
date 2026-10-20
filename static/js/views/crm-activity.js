// CRM activities and follow-ups (activities.py): the Activity card on a project's Overview, a client's page and
// a contact's page - open follow-ups on top, then the timeline newest first (calls, meetings, emails, notes,
// decisions, stage changes, follow-ups done and the project's finished tasks) - plus the follow-up rows Home
// and the Calendar show. Actions and inputs are in crm-activity-ui.js.
// Data: c.d.feeds['project:3' | 'client:1' | 'contact:7'] (GET /crm/feed) and c.d.followups.items (GET /followups).
// UI state: actNew { at, id, editId, kind, when, summary, outcome, people, saving, error } (the log form),
// fuNew { at, id, editId, title, date, time, notes, saving, error } (the follow-up form), actMore { [key]: rows shown }.

import { esc, foldTitle, folded } from '../ui.js';
import { plural } from '../format.js';
import { projectById } from '../data.js';
import { stageInfo } from '../crm-table.js';

// [key, label, icon] - same keys as activities.KINDS on the server.
export const KINDS = [['call', 'Call', '📞'], ['meeting', 'Meeting', '👥'], ['email', 'Email', '✉️'], ['note', 'Note', '📝'], ['decision', 'Decision', '⚖️']];
const kindInfo = k => KINDS.find(x => x[0] === k) || ['note', 'Note', '📝'];
const SHOWN = 12;
const HOME_DAYS = 7;

// ---------------------------------------------------------------- dates (local)
const pad = n => String(n).padStart(2, '0');
export const isoDay = dt => `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
const parseDay = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const daysBetween = (a, b) => Math.round((parseDay(b) - parseDay(a)) / 86400000);
const time12 = t => { if (!t) return ''; const [h, m] = t.split(':').map(Number); return `${((h + 11) % 12) + 1}:${pad(m)} ${h < 12 ? 'AM' : 'PM'}`; };
const clock = dt => dt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
// '2026-10-03T14:30' - what a datetime-local input holds, for a moment in local time.
export const localStamp = (dt = new Date()) => `${isoDay(dt)}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
const dayWord = s => {
  const n = daysBetween(isoDay(new Date()), s);
  if (n === 0) return 'Today';
  if (n === -1) return 'Yesterday';
  if (n === 1) return 'Tomorrow';
  const dt = parseDay(s), sameYear = dt.getFullYear() === new Date().getFullYear();
  return dt.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short', ...(sameYear ? {} : { year: 'numeric' }) });
};

// ---------------------------------------------------------------- follow-ups
// Worked out here rather than taken from the server, so a page left open over midnight stays right.
export function fuOverdue(f, now = new Date()) {
  if (f.status !== 'open') return false;
  if (f.due_time) { const [h, m] = f.due_time.split(':').map(Number); const at = parseDay(f.due_date); at.setHours(h, m, 0, 0); return now > at; }
  return f.due_date < isoDay(now);
}

// [label, colour] for a follow-up's badge: "3d late", "2:00 PM", "Today", "Tomorrow", "Mon 5 Oct".
export function fuBadge(f, now = new Date()) {
  if (f.status === 'done') return ['Done', 'var(--text-meta)'];
  const n = daysBetween(isoDay(now), f.due_date);
  if (fuOverdue(f, now)) return [n < 0 ? `${-n}d late` : 'Overdue', 'var(--err)'];
  if (n === 0) return [f.due_time ? time12(f.due_time) : 'Today', 'var(--wait)'];
  if (n === 1) return ['Tomorrow' + (f.due_time ? ' ' + time12(f.due_time) : ''), 'var(--text-2)'];
  return [dayWord(f.due_date), 'var(--text-meta)'];
}

const SUBJECT_ICON = { project: '▣', client: '◆', contact: '●' };

// One follow-up. own: on its own page's Activity card - tapping edits it (elsewhere it opens what it's on).
export function fuRow(c, f, { key = 'fu', own = false, subject = true, date = false } = {}) {
  const [b, bc] = fuBadge(f);
  const done = f.status === 'done', late = fuOverdue(f);
  const meta = [date ? esc(dayWord(f.due_date)) : '', subject && f.subject_name ? `${SUBJECT_ICON[f.subject] || ''} ${esc(f.subject_name)}` : '', f.notes ? esc(f.notes.split('\n')[0].slice(0, 80)) : '']
    .filter(Boolean).join(' · ');
  return `<div class="fu-row${done ? ' is-done' : ''}${late ? ' is-overdue' : ''}" data-key="${key}${f.id}">
    <button class="check check--sm" role="checkbox" aria-checked="${done}" data-action="fuDone" data-id="${f.id}" data-v="${done ? 'open' : 'done'}" aria-label="${done ? 'Mark not done' : 'Mark done'}">${done ? '✓' : ''}</button>
    <button class="fu-row__body" data-action="${own ? 'fuEdit' : 'fuOpen'}" data-id="${f.id}">
      <span class="fu-row__title truncate">${esc(f.title)}</span>
      <span class="meta truncate">Follow-up${meta ? ' · ' + meta : ''}</span>
    </button>
    <span class="fu-row__badge" style="color:${bc}">${esc(b)}</span>
  </div>`;
}

// The follow-ups the views may show: one on a project the Work / Personal switch (or hiding) leaves out stays out.
export function visibleFollowups(d) {
  const ids = new Set(d.projects.map(p => p.id));
  return ((d.followups && d.followups.items) || []).filter(f => f.subject !== 'project' || ids.has(f.subject_id));
}

// Home: what's overdue and what's due this week.
export function followupsHome(c) {
  if (!c.d.followups.loaded || c.d.followups.missing) return '';
  const today = isoDay(new Date()), end = isoDay(new Date(Date.now() + HOME_DAYS * 86400000));
  const open = visibleFollowups(c.d).filter(f => f.status === 'open' && (fuOverdue(f) || f.due_date <= end));
  if (!open.length) return '';
  const late = open.filter(f => fuOverdue(f));
  const list = [...late, ...open.filter(f => !fuOverdue(f))];
  const note = late.length ? `<span class="section__note" style="color:var(--err)">${late.length} overdue</span>` : '';
  return `<section class="section reveal" style="--i:110" data-key="fu-home">
    <div class="section__head">${foldTitle(c, 'fu-home', 'Follow-ups')}<span class="qb-tools">${note}<button class="btn--text" data-action="go" data-view="calendar">Calendar</button></span></div>
    ${folded(c, 'fu-home') ? '' : `<div class="list">${list.slice(0, 8).map(f => fuRow(c, f, { key: 'fuh', date: f.due_date !== today && !fuOverdue(f) })).join('')}</div>`
      + (list.length > 8 ? `<span class="section__note">+${list.length - 8} more</span>` : '')}
  </section>`;
}

// ---------------------------------------------------------------- forms
function kindChips(n) {
  return `<div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${KINDS.map(([v, l, ic]) =>
    `<button class="segmented__opt${n.kind === v ? ' is-on' : ''}" role="radio" aria-checked="${n.kind === v}" data-action="actSet" data-v="${v}"><span aria-hidden="true">${ic}</span> ${l}</button>`).join('')}</div>`;
}

// Who could have been involved: the people on this project / at this client first, then everyone else (a picker).
export function peopleChoices(c, at, id) {
  const all = c.d.contacts || [];
  let near = [];
  if (at === 'project') {
    const r = { ...(projectById(id, c.d) || {}), ...(c.d.records[id] || {}) };
    const linked = new Set((r.contacts || []).map(x => x.id));
    near = all.filter(x => linked.has(x.id) || (r.client_id && x.client_id === r.client_id));
  } else if (at === 'client') near = all.filter(x => x.client_id === id);
  else { const me = all.find(x => x.id === id); near = all.filter(x => x.id !== id && me && me.client_id && x.client_id === me.client_id); }
  const nearIds = new Set(near.map(x => x.id));
  return { near, others: all.filter(x => !nearIds.has(x.id) && !(at === 'contact' && x.id === id)) };
}

function activityForm(c, n) {
  const { near, others } = peopleChoices(c, n.at, n.id);
  const chosen = new Set(n.people);
  const shown = [...near, ...others.filter(x => chosen.has(x.id))];
  const rest = others.filter(x => !chosen.has(x.id));
  return `<div class="act-form" data-key="act-form">
    <span class="cn-form__title">${n.editId ? 'Edit activity' : 'Log an activity'}</span>
    ${kindChips(n)}
    <div class="act-form__row">
      <label class="cn-f"><span class="cn-f__l">When</span><input id="act-when" class="input" type="datetime-local" value="${esc(n.when)}" data-input="act.when" aria-label="When it happened"></label>
    </div>
    <label class="cn-f"><span class="cn-f__l">Summary</span><textarea id="act-summary" class="input act-form__text" rows="3" data-input="act.summary" maxlength="4000" placeholder="What was said or decided">${esc(n.summary)}</textarea></label>
    <div class="cn-f"><span class="cn-f__l">Who was involved</span>
      ${shown.length ? `<div class="chip-row act-people" data-noswipe>${shown.map(x => `<button class="chip chip--sm${chosen.has(x.id) ? ' is-on' : ''}" data-action="actPerson" data-id="${x.id}" aria-pressed="${chosen.has(x.id)}">${esc(x.name)}</button>`).join('')}</div>` : ''}
      ${rest.length ? `<select class="input cn-pick" data-cl-change="actPersonPick" aria-label="Add someone who was involved"><option value="">${shown.length ? 'Someone else…' : 'Add someone…'}</option>${rest.map(x => `<option value="${x.id}">${esc(x.name)}${x.role ? ' · ' + esc(x.role) : ''}</option>`).join('')}</select>` : ''}
      ${!shown.length && !rest.length ? '<span class="meta">No contacts saved yet.</span>' : ''}
    </div>
    <label class="cn-f"><span class="cn-f__l">Outcome <span class="meta">optional</span></span><input class="input" value="${esc(n.outcome)}" data-input="act.outcome" data-enter="actSave" maxlength="2000" placeholder="e.g. They'll send feedback by Friday" autocomplete="off"></label>
    <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="actSave"${n.saving ? ' disabled' : ''}>${n.saving ? 'Saving…' : n.editId ? 'Save' : 'Log it'}</button>
      <button class="btn btn--ghost btn--sm" data-action="actCancel">Cancel</button></div>
    ${n.error ? `<span class="form-err">${esc(n.error)}</span>` : ''}
  </div>`;
}

function followupForm(n) {
  return `<div class="act-form" data-key="fu-form">
    <span class="cn-form__title">${n.editId ? 'Edit follow-up' : 'New follow-up'}</span>
    <label class="cn-f"><span class="cn-f__l">To do</span><input id="fu-title" class="input" value="${esc(n.title)}" data-input="fu.title" data-enter="fuSave" maxlength="200" placeholder="e.g. Send invoice, chase feedback" autocomplete="off"></label>
    <div class="act-form__row">
      <label class="cn-f"><span class="cn-f__l">Due</span><input class="input" type="date" value="${esc(n.date)}" data-input="fu.date" aria-label="Due date"></label>
      <label class="cn-f"><span class="cn-f__l">Time <span class="meta">optional</span></span><input class="input" type="time" value="${esc(n.time)}" data-input="fu.time" aria-label="Due time"></label>
    </div>
    <label class="cn-f"><span class="cn-f__l">Notes <span class="meta">optional</span></span><textarea class="input" rows="2" data-input="fu.notes" maxlength="4000">${esc(n.notes)}</textarea></label>
    <span class="meta">I'll push a reminder at the due time (9:00 AM without one), held until morning during quiet hours.</span>
    <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="fuSave"${n.saving ? ' disabled' : ''}>${n.saving ? 'Saving…' : n.editId ? 'Save' : 'Add follow-up'}</button>
      <button class="btn btn--ghost btn--sm" data-action="fuCancel">Cancel</button>
      ${n.editId ? `<button class="btn btn--ghost btn--sm crm-manage__del" data-action="fuDelete" data-id="${n.editId}">Delete</button>` : ''}</div>
    ${n.error ? `<span class="form-err">${esc(n.error)}</span>` : ''}
  </div>`;
}

// ---------------------------------------------------------------- the timeline
// Where an item was logged, when that's not the page it's shown on (a client's page lists its projects' activity too).
function onWhat(x, at, id) {
  if (!x.subject || (x.subject === at && x.subject_id === id)) return '';
  const action = { project: 'clOpenProject', client: 'openClient', contact: 'openContact' }[x.subject];
  return ` <button class="act-on" data-action="${action}" data-id="${x.subject_id}">${SUBJECT_ICON[x.subject] || ''} ${esc(x.subject_name || '')}</button>`;
}

function item(c, x, at, id) {
  const when = clock(new Date(x.at));
  const body = (icon, label, text, extra = '', acts = '', cls = '') => `<div class="act-item${cls}" data-key="ai-${x.type}${x.id}">
    <span class="act-item__icon" aria-hidden="true">${icon}</span>
    <div class="act-item__body"><span class="act-item__head"><b>${label}</b><span class="meta">${esc(when)}</span>${x.type !== 'task' ? onWhat(x, at, id) : ''}</span>
      ${text}${extra}</div>${acts}</div>`;
  if (x.type === 'task') {
    return body(x.status === 'done' ? '🐙' : '👀', x.status === 'done' ? 'Octopus finished' : 'Finished, needs a review',
      `<button class="act-item__link" data-action="openChat" data-id="${x.thread_id || x.id}">${esc(x.title)}</button>`, '', '', ' act-item--task');
  }
  if (x.type === 'followup') {
    return body('✅', 'Follow-up done', `<span class="act-item__text">${esc(x.title)}</span>`, `<span class="meta">was due ${esc(dayWord(x.due_date))}</span>`, '', ' act-item--fu');
  }
  const del = `<button class="icon-btn icon-btn--ghost" data-action="actDelete" data-id="${x.id}" aria-label="Delete" title="Delete">✕</button>`;
  if (x.kind === 'stage') {
    const d = x.data || {}, [, fl, fc] = stageInfo(d.from), [, tl, tc] = stageInfo(d.to);
    return body('➜', 'Stage', `<span class="act-item__text"><span class="cl-stage" style="--c:${fc}">${fl}</span> → <span class="cl-stage" style="--c:${tc}">${tl}</span>${d.by === 'auto' ? ' <span class="meta">work started</span>' : ''}</span>`,
      '', `<div class="act-item__acts">${del}</div>`, ' act-item--stage');
  }
  const [, label, icon] = kindInfo(x.kind);
  const people = (x.people || []).map(p => `<button class="act-on" data-action="openContact" data-id="${p.id}">${esc(p.name)}</button>`).join('');
  return body(icon, label, `<span class="act-item__text">${esc(x.summary)}</span>`,
    (x.outcome ? `<span class="act-item__outcome"><b>Outcome</b> ${esc(x.outcome)}</span>` : '') + (people ? `<span class="act-item__people"><span class="meta">With</span>${people}</span>` : ''),
    `<div class="act-item__acts"><button class="icon-btn icon-btn--ghost" data-action="actEdit" data-id="${x.id}" aria-label="Edit" title="Edit">✎</button>${del}</div>`);
}

export function activityCard(c, { at, id }) {
  const key = `${at}:${id}`;
  if (c.d.feedsMissing) {
    return `<section class="card card--pad crm-card" data-key="act-card"><span class="card__title">Activity</span>
      <span class="card__desc">Restart the Den from the dashboard to start logging calls, meetings and follow-ups here.</span></section>`;
  }
  const f = c.d.feeds[key];
  const n = c.ui.actNew && c.ui.actNew.at === at && c.ui.actNew.id === id ? c.ui.actNew : null;
  const fu = c.ui.fuNew && c.ui.fuNew.at === at && c.ui.fuNew.id === id ? c.ui.fuNew : null;
  const head = `<div class="crm-card__head"><span class="card__title">Activity</span>
    <span class="act-card__btns">${n ? '' : `<button class="btn--text" data-action="actOpen" data-at="${at}" data-id="${id}">+ Log activity</button>`}
      ${fu ? '' : `<button class="btn--text" data-action="fuNewOpen" data-at="${at}" data-id="${id}">+ Follow-up</button>`}</span></div>`;
  if (!f) return `<section class="card card--pad crm-card act-card" data-key="act-card">${head}${n ? activityForm(c, n) : ''}${fu ? followupForm(fu) : ''}<span class="meta">${f === null ? 'Not found.' : 'Loading…'}</span></section>`;
  const open = f.open || [];
  const shown = (c.ui.actMore && c.ui.actMore[key]) || SHOWN;
  const items = (f.items || []).slice(0, shown);
  let day = '', rows = '';
  for (const x of items) {
    const d = isoDay(new Date(x.at));
    if (d !== day) { day = d; rows += `<span class="ag-label act-day">${esc(dayWord(d))}</span>`; }
    rows += item(c, x, at, id);
  }
  const editing = fu && fu.editId;
  const late = open.filter(x => fuOverdue(x)).length;
  return `<section class="card card--pad crm-card act-card" data-key="act-card">${head}
    ${n ? activityForm(c, n) : ''}${fu && !editing ? followupForm(fu) : ''}
    ${open.length ? `<div class="act-open"><span class="ag-label${late ? ' ag-label--err' : ''}">Follow-ups${late ? ` · ${late} overdue` : ''}</span>
      <div class="list">${open.map(x => (editing === x.id ? followupForm(fu) : fuRow(c, x, { key: 'fuo', own: true, subject: x.subject !== at || x.subject_id !== id }))).join('')}</div></div>` : ''}
    ${items.length ? `<div class="act-feed">${rows}</div>` : n || fu ? '' : `<span class="card__desc">${at === 'project' ? 'Calls, meetings, emails, notes and decisions about this project, its stage changes and the work Octopus finishes all show up here.' : 'Log calls, meetings, emails, notes and decisions, and add follow-ups so nothing slips.'}</span>`}
    ${(f.items || []).length > shown ? `<button class="btn--text btn--muted" data-action="actMore" data-k="${key}">Show ${plural(Math.min(SHOWN * 2, f.items.length - shown), 'more item')}</button>` : ''}
  </section>`;
}
