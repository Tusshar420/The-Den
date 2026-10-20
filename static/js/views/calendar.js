// The project calendar (see timeline.py): the Calendar page, a project's Timeline tab, the add / edit
// sheet, Home's "Coming up" card and the "Good morning" briefing page. Items are deadlines, milestones,
// dated tasks, presentations, demos, meetings and other events, each on one project.

import { plural } from '../format.js';
import { orderedProjects, projectById, projectColor, realProjects } from '../data.js';
import { esc, octo, spinner, toggle, polishLabel, foldTitle, folded } from '../ui.js';
import { fuRow, fuOverdue, visibleFollowups, peopleChoices } from './crm-activity.js';

// Same keys as timeline.KINDS; the server's copy (with its reminder defaults) wins once loaded.
const KINDS = {
  deadline: { label: 'Deadline', icon: '⏰', due: true, remind: [7, 3, 1, 0] },
  milestone: { label: 'Milestone', icon: '🏁', due: true, remind: [3, 1, 0] },
  task: { label: 'Task', icon: '☑️', due: true, remind: [1, 0] },
  presentation: { label: 'Presentation', icon: '📊', due: false, remind: [3, 1, 0] },
  demo: { label: 'Demo', icon: '🎬', due: false, remind: [3, 1, 0] },
  meeting: { label: 'Meeting', icon: '👥', due: false, remind: [1, 0] },
  event: { label: 'Event', icon: '📌', due: false, remind: [1, 0] },
};
export const kinds = d => ({ ...KINDS, ...((d.timeline && d.timeline.kinds) || {}) });
const REMIND_CHOICES = [[0, 'On the day'], [1, '1 day before'], [3, '3 days'], [7, '1 week'], [14, '2 weeks'], [30, '1 month']];
const HEALTH = {
  on_track: ['On track', 'var(--ok)'], at_risk: ['At risk', 'var(--wait)'], overdue: ['Overdue', 'var(--err)'],
  done: ['All done', 'var(--text-meta)'], none: ['No timeline', 'var(--text-faint)'],
};
const UPCOMING_DAYS = 14;
// Kinds that can say who they're with (timeline.WITH_PEOPLE).
export const WITH_PEOPLE = ['meeting'];
// The kinds that can repeat (timeline.REPEATABLE). A repeat rule is {freq: days | weekdays | weeks | months, every, on, nth}
// (timeline.rule_of): the server sends it as repeat_rule, and says it in words as `repeat`.
export const REPEATABLE = ['meeting'];
export const ruleOf = e => e.repeat_rule || (e.repeat_days ? { freq: 'days', every: e.repeat_days } : null);
const repeatLabel = e => (e.repeat ? e.repeat.replace(/ until .*/, '').replace(/^./, ch => ch.toUpperCase()) : '');

// The names of who a meeting is with (contacts deleted since are skipped).
const peopleNames = (c, e) => [...(e.contact_ids || []).map(id => (c.d.contacts || []).find(x => x.id === id)), ...(e.employee_ids || []).map(id => (c.d.employees || []).find(x => x.id === id))].filter(Boolean).map(x => x.name);

// ---------------------------------------------------------------- dates (YYYY-MM-DD strings, local calendar days)
const pad = n => String(n).padStart(2, '0');
export const isoDay = dt => `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
export const parseDay = s => { const [y, m, dd] = s.split('-').map(Number); return new Date(y, m - 1, dd); };
export const addDays = (s, n) => { const dt = parseDay(s); dt.setDate(dt.getDate() + n); return isoDay(dt); };
export const todayOf = d => (d.timeline && d.timeline.today) || isoDay(new Date());
const between = (a, b) => Math.round((parseDay(b) - parseDay(a)) / 86400000);
const dayLabel = (s, opts = { weekday: 'short', month: 'short', day: 'numeric' }) => parseDay(s).toLocaleDateString([], opts);
export const weekdayOf = s => (parseDay(s).getDay() + 6) % 7;                 // Monday = 0, like the server
export const nthWeekdayOf = s => Math.floor((parseDay(s).getDate() - 1) / 7) + 1;   // 1-5: the 2nd Tuesday of its month
const time12 = t => { if (!t) return ''; const [h, m] = t.split(':').map(Number); return `${((h + 11) % 12) + 1}:${pad(m)} ${h < 12 ? 'AM' : 'PM'}`; };

// Does a repeat rule, counted from its first day, fall on `day`? (The same rules as timeline._hit.)
function hits(rule, first, day) {
  const every = rule.every || 1, a = parseDay(first), d = parseDay(day);
  if (rule.freq === 'days') return between(first, day) % every === 0;
  if (rule.freq === 'weekdays') return weekdayOf(day) < 5;
  if (rule.freq === 'weeks') return rule.on.includes(weekdayOf(day)) && (between(addDays(first, -weekdayOf(first)), addDays(day, -weekdayOf(day))) / 7) % every === 0;
  if (((d.getFullYear() - a.getFullYear()) * 12 + d.getMonth() - a.getMonth()) % every) return false;
  const len = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
  if (!rule.nth) return d.getDate() === Math.min(a.getDate(), len);
  if (weekdayOf(day) !== rule.on[0]) return false;
  return rule.nth === -1 ? d.getDate() + 7 > len : nthWeekdayOf(day) === rule.nth;
}

// A repeating meeting is one item (dated its next day, start_date its first); this gives a copy of it for every day it
// happens from `from` to `to` (inclusive). One-offs pass through as they are.
export function spread(list, from, to) {
  const out = [];
  for (const e of list) {
    const rule = ruleOf(e);
    if (!rule) { out.push(e); continue; }
    const first = e.start_date || e.date;
    const last = e.repeat_until && e.repeat_until < to ? e.repeat_until : to;
    for (let day = from <= first ? first : from; day <= last; day = addDays(day, 1)) if (hits(rule, first, day)) out.push({ ...e, date: day });
  }
  return out;
}

// Items on projects the Work / Personal switch shows.
export function events(d) {
  const ids = new Set(d.projects.map(p => p.id));
  return ((d.timeline && d.timeline.events) || []).filter(e => ids.has(e.project_id));
}

// Calendar items with notes written on them, last edited first - the Notes page and a project's Notes tab
// list these under a "Meeting notes" tag. Editing one opens it full-page (openMeetingNotes).
export function meetingNotes(d, pid = null) {
  return events(d).filter(e => (e.notes || '').trim() && (pid == null || e.project_id === pid))
    .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || '') || b.id - a.id);
}

// "Tue, Oct 6 · 3:00 PM" - when a meeting note's item is (its first day, for one that repeats).
export const meetingWhen = e => [dayLabel(e.start_date || e.date), time12(e.time)].filter(Boolean).join(' · ');

// Open items for the sidebar's Calendar row: overdue first, then the next two weeks in date order.
export function upcomingEvents(d) {
  const today = todayOf(d), end = addDays(today, UPCOMING_DAYS);
  const open = events(d).filter(e => e.status === 'open');
  return [...open.filter(e => e.overdue), ...open.filter(e => !e.overdue && e.date >= today && e.date <= end)];
}

// "3 days left", "Today", "2 days late" - the short badge on a row.
export function badge(e, today) {
  if (e.status === 'done') return ['Done', 'var(--text-meta)'];
  if (e.status === 'cancelled') return ['Cancelled', 'var(--text-faint)'];
  if (e.overdue) { const n = between(e.date, today); return [n <= 0 ? 'Overdue' : `${n}d late`, 'var(--err)']; }
  const n = between(today, e.date);
  if (n < 0) return ['Past', 'var(--text-faint)'];
  if (n === 0) return [e.time ? time12(e.time) : 'Today', 'var(--wait)'];
  if (n === 1) return ['Tomorrow', 'var(--text-2)'];
  return [`${n}d`, 'var(--text-meta)'];
}

// "Still open: Fix login, Write docs; 4 backlog items not started." (timeline.outstanding_words)
export function outstandingText(o) {
  if (!o) return '';
  const parts = [];
  if (o.items && o.items.length) parts.push(o.items.slice(0, 3).map(i => i.title).join(', ') + (o.items.length > 3 ? ` and ${o.items.length - 3} more` : ''));
  if (o.backlog) parts.push(plural(o.backlog, 'backlog item') + ' not started');
  if (o.in_queue) parts.push(`${o.in_queue} in the queue`);
  return parts.length ? 'Still open: ' + parts.join('; ') + '.' : '';
}

export function evRow(c, e, { project = true, key = 'ev', date = false } = {}) {
  const k = kinds(c.d)[e.kind] || KINDS.event;
  const [b, bc] = badge(e, todayOf(c.d));
  const p = projectById(e.project_id, c.d);
  const closed = e.status !== 'open';
  // On the day, the badge already shows the time.
  const when = [date ? dayLabel(e.date) : '', e.time && between(todayOf(c.d), e.date) !== 0 ? time12(e.time) : ''].filter(Boolean).join(' ');
  const names = peopleNames(c, e);
  const withWho = names.length ? 'with ' + names.slice(0, 2).join(', ') + (names.length > 2 ? ` +${names.length - 2}` : '') : '';
  const again = ruleOf(e) ? '↻ ' + repeatLabel(e) : '';
  const invited = e.google_event_id ? '✉ Invited' : e.invite_error ? '⚠ Invite not sent' : '';
  const meta = [k.label, esc(again), esc(withWho), esc(invited), project && p ? esc(p.name) : '', esc(when)].filter(Boolean).join(' · ');
  const still = !closed ? outstandingText(e.outstanding) : '';
  const lead = k.due
    ? `<button class="check check--sm" role="checkbox" aria-checked="${e.status === 'done'}" data-action="eventDone" data-id="${e.id}" aria-label="${e.status === 'done' ? 'Mark not done' : 'Mark done'}">${e.status === 'done' ? '✓' : ''}</button>`
    : `<span class="ev-row__icon" aria-hidden="true">${k.icon}</span>`;
  return `<div class="ev-row${closed ? ' is-closed' : ''}${e.overdue ? ' is-overdue' : ''}${e.meet_link && !closed ? ' ev-row--join' : ''}" style="--pc:${projectColor(p, c.d)}" data-key="${key}${e.id}${ruleOf(e) ? '-' + e.date : ''}">
    ${lead}
    <button class="ev-row__body" data-action="openEvent" data-id="${e.id}">
      <span class="ev-row__title truncate">${esc(e.title)}</span>
      <span class="meta truncate">${meta}</span>
      ${still ? `<span class="ev-row__still">${esc(still)}</span>` : ''}
    </button>
    ${e.meet_link && !closed ? `<a class="ev-row__join" href="${esc(e.meet_link)}" target="_blank" rel="noopener noreferrer" aria-label="Join the Google Meet for ${esc(e.title)}">Join</a>` : ''}
    <span class="ev-row__badge" style="color:${bc}">${esc(b)}</span>
  </div>`;
}

const healthPill = h => { const [l, col] = HEALTH[h.state] || HEALTH.none; return `<span class="health-pill" style="--c:${col}"><span class="dot" style="--c:${col};width:7px;height:7px"></span>${l}</span>`; };

// ---------------------------------------------------------------- Calendar page
// CRM follow-ups (activities.py) for the calendar: all of them, or only one project's when a project is picked.
function followupsFor(c, project = 'all') {
  return visibleFollowups(c.d).filter(f => project === 'all' || (f.subject === 'project' && f.subject_id === project));
}

function monthGrid(c, list, fus = []) {
  const cal = c.ui.cal, today = todayOf(c.d);
  const first = parseDay(cal.month + '-01');
  const start = addDays(cal.month + '-01', -((first.getDay() + 6) % 7));        // weeks start on Monday
  const byDay = new Map();
  for (const e of spread(list, start, addDays(start, 41))) { if (e.status !== 'cancelled') (byDay.get(e.date) || byDay.set(e.date, []).get(e.date)).push(e); }
  for (const f of fus) (byDay.get(f.due_date) || byDay.set(f.due_date, []).get(f.due_date)).push({ fu: true, id: f.id, title: f.title, status: f.status, overdue: fuOverdue(f), date: f.due_date });
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const day = addDays(start, i), dt = parseDay(day);
    if (i === 35 && dt.getMonth() !== first.getMonth()) break;                  // a sixth row only when the month needs it
    const items = byDay.get(day) || [];
    const cls = [dt.getMonth() !== first.getMonth() ? 'is-out' : '', day === today ? 'is-today' : '', day === cal.day ? 'is-sel' : '',
      items.some(e => e.overdue) ? 'has-overdue' : ''].filter(Boolean).join(' ');
    cells.push(`<button class="cal-cell ${cls}" data-action="calDay" data-date="${day}" aria-label="${esc(dayLabel(day, { weekday: 'long', month: 'long', day: 'numeric' }))}${items.length ? ', ' + plural(items.length, 'item') : ''}">
      <span class="cal-cell__n">${dt.getDate()}</span>
      <span class="cal-cell__items">${items.slice(0, 3).map(e => `<span class="cal-chip${e.fu ? ' cal-chip--fu' : ''}${e.status === 'done' ? ' is-done' : ''}${e.overdue ? ' is-overdue' : ''}" style="--pc:${e.fu ? 'var(--text-meta)' : projectColor(projectById(e.project_id, c.d), c.d)}">${esc(e.title)}</span>`).join('')}${items.length > 3 ? `<span class="cal-more">+${items.length - 3}</span>` : ''}</span>
      <span class="cal-cell__dots">${items.slice(0, 4).map(e => `<span class="dot" style="--c:${e.overdue ? 'var(--err)' : e.fu ? 'var(--text-meta)' : projectColor(projectById(e.project_id, c.d), c.d)};width:6px;height:6px"></span>`).join('')}</span>
    </button>`);
  }
  const title = first.toLocaleDateString([], { month: 'long', year: 'numeric' });
  return `<section class="card cal-month" data-key="cal-month">
    <div class="cal-month__head">
      <button class="icon-btn icon-btn--ghost" data-action="calMonth" data-d="-1" aria-label="Previous month">‹</button>
      <span class="cal-month__title">${esc(title)}</span>
      <button class="icon-btn icon-btn--ghost" data-action="calMonth" data-d="1" aria-label="Next month">›</button>
      <button class="btn--text cal-month__today" data-action="calMonth" data-d="0">Today</button>
    </div>
    <div class="cal-grid cal-grid--dow" aria-hidden="true">${['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(x => `<span>${x}</span>`).join('')}</div>
    <div class="cal-grid">${cells.join('')}</div>
  </section>`;
}

function selectedDay(c, list, fus = []) {
  const day = c.ui.cal.day || todayOf(c.d);
  const items = spread(list, day, day).filter(e => e.date === day);
  const dayFus = fus.filter(f => f.due_date === day);
  const pid = c.ui.cal.project !== 'all' ? c.ui.cal.project : '';
  return `<section class="section" data-key="cal-day">
    <div class="section__head"><span class="section__title">${esc(dayLabel(day, { weekday: 'long', month: 'long', day: 'numeric' }))}</span>
      <button class="btn--text" data-action="openEvent" data-date="${day}"${pid ? ` data-pid="${pid}"` : ''}>+ Add on this day</button></div>
    ${items.length || dayFus.length ? `<div class="list">${items.map(e => evRow(c, e, { key: 'cd' })).join('')}${dayFus.map(f => fuRow(c, f, { key: 'cdf' })).join('')}</div>` : `<span class="section__empty">Nothing on this day.</span>`}
  </section>`;
}

// Overdue first, then the next two weeks, grouped by day.
function agenda(c, list, { limit = 40, key = 'ag' } = {}, fus = []) {
  const today = todayOf(c.d), end = addDays(today, UPCOMING_DAYS);
  // Timeline items and follow-ups together, each drawn its own way.
  const rows = [...spread(list.filter(e => e.status === 'open'), today, end).map(e => ({ date: e.date, time: e.time || '', overdue: e.overdue, draw: (k, date) => evRow(c, e, { key: k, date }) })),
    ...fus.filter(f => f.status === 'open').map(f => ({ date: f.due_date, time: f.due_time || '', overdue: fuOverdue(f), draw: (k, date) => fuRow(c, f, { key: k + 'f', date }) }))]
    .sort((a, b) => a.date.localeCompare(b.date) || (a.time || '99').localeCompare(b.time || '99'));
  const overdue = rows.filter(e => e.overdue);
  const soon = rows.filter(e => !e.overdue && e.date >= today && e.date <= end).slice(0, limit);
  if (!overdue.length && !soon.length) return `<span class="section__empty">Nothing due in the next two weeks.</span>`;
  const groups = [];
  for (const e of soon) {
    const n = between(today, e.date);
    const label = n === 0 ? 'Today' : n === 1 ? 'Tomorrow' : dayLabel(e.date, { weekday: 'long', month: 'short', day: 'numeric' });
    const g = groups[groups.length - 1];
    if (g && g.label === label) g.items.push(e); else groups.push({ label, items: [e] });
  }
  return (overdue.length ? `<span class="ag-label ag-label--err">Overdue</span><div class="list">${overdue.map(e => e.draw(key + 'o', true)).join('')}</div>` : '')
    + groups.map(g => `<span class="ag-label">${esc(g.label)}</span><div class="list">${g.items.map(e => e.draw(key, false)).join('')}</div>`).join('');
}

function healthList(c) {
  const health = (c.d.timeline && c.d.timeline.health) || {};
  const rows = realProjects(c.d).map(p => ({ p, h: health[p.id] })).filter(x => x.h);
  if (!rows.length) return '';
  const order = { overdue: 0, at_risk: 1, on_track: 2, done: 3, none: 4 };
  rows.sort((a, b) => order[a.h.state] - order[b.h.state] || a.p.id - b.p.id);
  return `<section class="section" data-key="cal-health">
    <div class="section__head"><span class="section__title">Projects</span></div>
    <div class="list">${rows.map(({ p, h }) => `<button class="health-row" data-action="openProjectTimeline" data-id="${p.id}" data-key="hr${p.id}">
      <span class="swatch-sq" style="background:${projectColor(p, c.d)}"></span>
      <span class="health-row__text"><span class="health-row__name truncate">${esc(p.name)}</span><span class="meta health-row__why">${esc(h.reason)}</span></span>
      ${healthPill(h)}</button>`).join('')}</div>
  </section>`;
}

export function calendarPage(c) {
  const cal = c.ui.cal;
  const all = events(c.d);
  const list = cal.project === 'all' ? all : all.filter(e => e.project_id === cal.project);
  const fus = followupsFor(c, cal.project);
  const projects = realProjects(c.d);
  const chips = `<div class="chip-row" role="radiogroup" aria-label="Project" data-noswipe>
    <button class="chip${cal.project === 'all' ? ' is-on' : ''}" role="radio" aria-checked="${cal.project === 'all'}" data-action="calProject" data-id="all">All projects</button>
    ${projects.map(p => `<button class="chip${cal.project === p.id ? ' is-on' : ''}" role="radio" aria-checked="${cal.project === p.id}" data-action="calProject" data-id="${p.id}" data-key="cp${p.id}"><span class="swatch-sq" style="background:${projectColor(p, c.d)}"></span>${esc(p.name)}</button>`).join('')}</div>`;
  const loading = !c.d.timeline.loaded ? `<div class="empty">${spinner()} Loading the calendar…</div>` : '';
  return `<div class="page page--calendar" data-key="page-calendar">
    <div class="cal-top">${chips}<button class="btn btn--primary btn--40" data-action="openEvent"${cal.project !== 'all' ? ` data-pid="${cal.project}"` : ''}${cal.day ? ` data-date="${cal.day}"` : ''}>+ Add</button></div>
    ${loading}
    <div class="cal-layout">
      <div class="page__col">${monthGrid(c, list, fus)}${selectedDay(c, list, fus)}</div>
      <div class="page__col">
        ${cal.project === 'all' ? healthList(c) : ''}
        <section class="section" data-key="cal-agenda"><div class="section__head"><span class="section__title">Coming up</span><span class="section__note">next ${UPCOMING_DAYS} days</span></div>${agenda(c, list, {}, fus)}</section>
        <span class="section__hint">I push a reminder ahead of each date (a week, 3 days and a day before a deadline; an hour before a timed meeting) and tell you what's still open. Say "schedule a demo for ${esc((projects[0] || { name: 'my project' }).name)} on Friday at 3pm" to Octopus to add one by voice.</span>
      </div>
    </div>
  </div>`;
}

// Right side panel: the month, the picked day and what's coming up, for all projects.
export function calendarSide(c) {
  const list = events(c.d), fus = followupsFor(c);
  const loading = !c.d.timeline.loaded ? `<div class="empty">${spinner()} Loading the calendar…</div>` : '';
  return `${loading}${monthGrid(c, list, fus)}${selectedDay(c, list, fus)}
    <section class="section" data-key="cal-agenda-side"><div class="section__head"><span class="section__title">Coming up</span><span class="section__note">next ${UPCOMING_DAYS} days</span></div>${agenda(c, list, { key: 'sag' }, fus)}</section>`;
}

// ---------------------------------------------------------------- project Timeline tab
export function timelineTab(c, p) {
  const all = events(c.d).filter(e => e.project_id === p.id);
  const h = (c.d.timeline.health || {})[p.id] || { state: 'none', label: 'No timeline', reason: 'Nothing is scheduled for this project.' };
  const today = todayOf(c.d);
  const open = all.filter(e => e.status === 'open');
  const overdue = open.filter(e => e.overdue);
  const upcoming = open.filter(e => !e.overdue && e.date >= today);
  const past = open.filter(e => !e.overdue && e.date < today);            // meetings / demos that have happened but weren't closed
  const closed = all.filter(e => e.status !== 'open').sort((a, b) => b.date.localeCompare(a.date));
  const showDone = !!c.ui.tlShowDone;
  const [, hc] = HEALTH[h.state] || HEALTH.none;
  const addKinds = ['deadline', 'milestone', 'task', 'meeting', 'presentation', 'demo'];
  const K = kinds(c.d);
  const line = list => `<div class="tl-line">${list.map(e => `<div class="tl-item${e.overdue ? ' is-overdue' : ''}"><span class="tl-item__date"><b>${esc(dayLabel(e.date, { day: 'numeric' }))}</b>${esc(dayLabel(e.date, { month: 'short' }))}</span>${evRow(c, e, { project: false, key: 'tl' })}</div>`).join('')}</div>`;
  return `<div class="timeline" data-key="tab-timeline">
    <div class="tl-health" style="--c:${hc}">${healthPill(h)}<span class="tl-health__why">${esc(h.reason)}</span>
      ${p.is_general || !open.length ? '' : `<button class="btn btn--sm" data-action="checkProgress" data-pid="${p.id}" title="Ask Claude to look at the code and backlog against these dates">Check progress</button>`}</div>
    <div class="section">
      <div class="section__head"><span class="section__title">Add to the timeline</span></div>
      <div class="chip-row" data-noswipe>${addKinds.map(k => `<button class="chip" data-action="openEvent" data-pid="${p.id}" data-kind="${k}" data-key="tla-${k}"><span aria-hidden="true">${K[k].icon}</span> ${K[k].label}</button>`).join('')}</div>
    </div>
    <div class="tl-cols">
    ${overdue.length ? `<div class="section"><div class="section__head"><span class="section__title">Overdue <span class="section__count">${overdue.length}</span></span></div>${line(overdue)}</div>` : ''}
    <div class="section"><div class="section__head"><span class="section__title">Upcoming <span class="section__count">${upcoming.length}</span></span></div>
      ${upcoming.length ? line(upcoming) : `<span class="section__empty">${all.length ? 'Nothing else coming up.' : 'No dates yet. Add the deadline, milestones and meetings for this project and I\'ll keep an eye on them.'}</span>`}</div>
    ${past.length ? `<div class="section"><div class="section__head"><span class="section__title">Happened</span><span class="section__note">mark done or move them</span></div>${line(past)}</div>` : ''}
    ${closed.length ? `<div class="section"><div class="section__head"><span class="section__title">Done <span class="section__count">${closed.length}</span></span><button class="btn--text" data-action="tlShowDone">${showDone ? 'Hide' : 'Show'}</button></div>
      ${showDone ? `<div class="list">${closed.map(e => evRow(c, e, { project: false, key: 'tld', date: true })).join('')}</div>` : ''}</div>` : ''}
    </div>
    <span class="section__hint">Everything I run for this project sees these dates, so work that unblocks the nearest deadline goes first.</span>
  </div>`;
}

// ---------------------------------------------------------------- Home: Coming up
export function comingUp(c) {
  if (!c.d.timeline.loaded) return '';
  const list = events(c.d);
  const today = todayOf(c.d);
  const open = list.filter(e => e.status === 'open' && (e.overdue || (e.date >= today && e.date <= addDays(today, 7))));
  const risky = Object.entries(c.d.timeline.health || {}).filter(([pid, h]) => h.state === 'at_risk' && projectById(Number(pid), c.d)).length;
  const note = risky ? `<span class="section__note" style="color:var(--wait)">${plural(risky, 'project')} at risk</span>` : '';
  return `<section class="section reveal" style="--i:100" data-key="coming-up">
    <div class="section__head">${foldTitle(c, 'coming-up', 'Coming up')}<span class="qb-tools">${note}<button class="btn--text" data-action="go" data-view="calendar">Calendar</button></span></div>
    ${folded(c, 'coming-up') ? '' : open.length ? `<div class="list">${open.slice(0, 6).map(e => evRow(c, e, { key: 'cu', date: !e.overdue && e.date !== today })).join('')}</div>`
      + (open.length > 6 ? `<button class="btn--text btn--muted" data-action="go" data-view="calendar">+${open.length - 6} more on the calendar</button>` : '')
      : `<div class="empty"><span>${list.length ? 'Nothing due this week.' : 'No project dates yet.'}</span><button class="btn btn--sm" data-action="openEvent">Add a date</button></div>`}
  </section>`;
}

// ---------------------------------------------------------------- repeat editor
// e.rep is the repeat being edited: null (once) or {freq, every, on, monthly}. `on` is the chosen weekdays (Monday = 0),
// null = the weekday of the date; monthly is 'date' (the 15th), 'nth' (the 2nd Tuesday) or 'last' (the last Tuesday).
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const FULL_DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const ordinal = n => n + (n >= 11 && n <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th');
const PRESETS = [['once', 'Once'], ['daily', 'Every day'], ['other', 'Every other day'], ['weekdays', 'Weekdays'], ['weekly', 'Weekly'], ['monthly', 'Monthly']];
const PRESET_REP = { once: null, daily: { freq: 'days', every: 1 }, other: { freq: 'days', every: 2 }, weekdays: { freq: 'weekdays', every: 1 },
  weekly: { freq: 'weeks', every: 1, on: null }, monthly: { freq: 'months', every: 1, on: null, monthly: 'date' } };
export const repPreset = rep => (!rep ? 'once' : rep.freq === 'days' ? ({ 1: 'daily', 2: 'other' }[rep.every] || 'keep')
  : { weekdays: 'weekdays', weeks: 'weekly', months: 'monthly' }[rep.freq]);
export const presetRep = key => (PRESET_REP[key] ? { ...PRESET_REP[key] } : null);
// The editor state for a saved rule.
export const repOf = e => {
  const r = ruleOf(e);
  return r ? { freq: r.freq, every: r.every || 1, on: r.freq === 'weeks' ? r.on : null, monthly: r.nth === -1 ? 'last' : r.nth ? 'nth' : 'date' } : null;
};
// The rule to save for the editor's repeat and date.
export function ruleBody(e) {
  const r = e.rep;
  if (!r) return null;
  const rule = { freq: r.freq, every: r.freq === 'weekdays' ? 1 : r.every };
  if (r.freq === 'weeks' && r.on) rule.on = r.on;
  if (r.freq === 'months' && r.monthly !== 'date') Object.assign(rule, { nth: r.monthly === 'last' ? -1 : nthWeekdayOf(e.date) }, r.on ? { on: r.on } : {});
  return rule;
}

function repeatField(e, label) {
  const r = e.rep, now = repPreset(r), wd = weekdayOf(e.date), chip = (on, attrs, text) =>
    `<button class="chip chip--sm${on ? ' is-on' : ''}" ${attrs}>${esc(text)}</button>`;
  const presets = PRESETS.map(([k, l]) => chip(now === k, `role="radio" aria-checked="${now === k}" data-action="evRepeat" data-k="${k}"`, l));
  // A saved rule with another interval (every 3 days) has no preset - it shows as itself.
  if (now === 'keep') presets.push(chip(true, 'role="radio" aria-checked="true" data-action="evRepeat" data-k="keep"', `Every ${r.every} days`));
  let more = '';
  if (r && (r.freq === 'weeks' || r.freq === 'months')) {
    const unit = r.freq === 'weeks' ? 'week' : 'month', max = r.freq === 'weeks' ? 8 : 12;
    more += `<div class="ev-when"><span class="meta">Every</span><select class="input cn-pick" data-cl-change="evRepEvery" aria-label="Repeat every how many ${unit}s">${Array.from({ length: max }, (_, i) => i + 1)
      .map(n => `<option value="${n}"${r.every === n ? ' selected' : ''}>${n === 1 ? unit : n + ' ' + unit + 's'}</option>`).join('')}</select></div>`;
  }
  if (r && r.freq === 'weeks') {
    const on = r.on || [wd];
    more += `<div class="chip-row chip-row--wrap" aria-label="Days of the week">${WEEKDAYS.map((n, i) =>
      chip(on.includes(i), `aria-pressed="${on.includes(i)}" data-action="evRepDay" data-wd="${i}"`, n)).join('')}</div>`;
  }
  if (r && r.freq === 'months') {
    const nth = nthWeekdayOf(e.date), day = parseDay(e.date).getDate();
    const opts = [['date', `On the ${ordinal(day)}`], ...(nth <= 4 ? [['nth', `On the ${ordinal(nth)} ${FULL_DAYS[wd]}`]] : []), ['last', `On the last ${FULL_DAYS[wd]}`]];
    more += `<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Which day of the month">${opts.map(([k, l]) =>
      chip(r.monthly === k, `role="radio" aria-checked="${r.monthly === k}" data-action="evRepMonthly" data-k="${k}"`, l)).join('')}</div>`;
  }
  return `${label('REPEAT')}<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Repeat">${presets.join('')}</div>${more}
    ${r ? `<div class="ev-when"><input id="evUntil" class="input" type="date" data-input="ev.until" aria-label="Last day it repeats (optional)" min="${esc(e.date)}" value="${esc(e.until)}">
      <span class="meta">${e.id ? 'last day (optional) · changes apply to every repeat' : 'last day (optional)'}</span></div>` : ''}`;
}

// ---------------------------------------------------------------- add / edit sheet
// A meeting's length (what its Google Calendar invite spans) and its invite (invites.py): Google emails everyone on it.
export const DURATIONS = [15, 30, 45, 60, 90, 120];
export const DEFAULT_DURATION = 60;
const durLabel = m => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m / 60} h`);
// Everyone the invite would go to: the picked contacts that have an email, then the typed-in guests.
export function inviteEmails(c, e) {
  const picked = [...e.people.map(id => (c.d.contacts || []).find(x => x.id === id)), ...(e.staff || []).map(id => (c.d.employees || []).find(x => x.id === id))].filter(Boolean);
  const withEmail = picked.filter(x => (x.email || '').trim());
  return { emails: [...new Set([...withEmail.map(x => x.email.trim().toLowerCase()), ...e.guests])], noEmail: picked.filter(x => !(x.email || '').trim()).map(x => x.name) };
}

function inviteField(c, e, label) {
  const connected = c.d.timeline.invites !== false, sent = !!e.gid;
  const { emails, noEmail } = inviteEmails(c, e);
  const guestChips = e.guests.map(g => `<button class="chip chip--sm is-on" data-action="evGuestDel" data-email="${esc(g)}" aria-label="Remove ${esc(g)}">${esc(g)} <span aria-hidden="true">✕</span></button>`).join('');
  const durs = [...new Set([...DURATIONS, e.duration])].sort((a, b) => a - b);
  const who = emails.length ? `Google emails ${emails.length === 1 ? emails[0] : `${emails.length} people`} the invitation, and again if it changes or is cancelled.`
    : 'Nobody here has an email yet - it just goes on your Google Calendar.';
  const row = (title, desc, on, action, off) => `<div class="set-row"><div class="grow"><span class="set-row__label">${title}</span><span class="set-row__desc">${desc}</span></div>${toggle(on, action, `aria-label="${esc(title)}"${off ? ' disabled' : ''}`)}</div>`;
  const meetDesc = e.meetLink ? `<a href="${esc(e.meetLink)}" target="_blank" rel="noopener noreferrer">${esc(e.meetLink.replace(/^https?:\/\//, ''))}</a>` : 'A Google Meet video link on the invite.';
  return `${label('GUESTS')}
    ${guestChips ? `<div class="chip-row chip-row--wrap" aria-label="Guests">${guestChips}</div>` : ''}
    <div class="ev-guest"><input id="evGuest" class="input" type="email" autocomplete="email" placeholder="Add a guest by email" data-input="ev.guestDraft" data-enter="evGuestAdd" aria-label="Guest email" value="${esc(e.guestDraft)}">
      <button class="btn btn--ghost btn--44" data-action="evGuestAdd">Add</button></div>
    ${e.time ? `${label('LENGTH')}<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="How long">${durs.map(m =>
      `<button class="chip chip--sm${e.duration === m ? ' is-on' : ''}" role="radio" aria-checked="${e.duration === m}" data-action="evDuration" data-m="${m}">${durLabel(m)}</button>`).join('')}</div>` : ''}
    ${label('INVITE')}<div class="ev-invite">
      ${!connected && !sent ? `<span class="card__desc">Connect your Google account to send invites and make Meet links. <button class="btn--text" data-action="goPersonal" data-tab="connections">Connections</button></span>` : ''}
      ${row('Send Google Calendar invite', sent && e.invite ? `Sent${e.inviteLink ? ` · <a href="${esc(e.inviteLink)}" target="_blank" rel="noopener noreferrer">open in Google Calendar</a>` : ''}. ${who}` : sent ? 'Saving cancels the invite - Google emails everyone that it\'s cancelled.' : who, e.invite, 'evInvite', !connected && !sent)}
      ${row('Google Meet link', meetDesc, e.meet || !!e.meetLink, 'evMeet', (!connected && !sent) || !!e.meetLink)}
      ${noEmail.length && e.invite ? `<span class="meta">No email saved for ${esc(noEmail.join(', '))} - add it under Contacts to invite them.</span>` : ''}
      ${e.inviteError ? `<span class="form-err">Invite not sent: ${esc(e.inviteError)}${e.invite ? ' Save to try again.' : ''}</span>` : ''}
    </div>`;
}

export function eventSheet(c, wrap) {
  const e = c.ui.evEdit;
  if (!e) return '';
  const K = kinds(c.d);
  const title = e.id ? 'Edit timeline item' : 'Add to a timeline';
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">${title}</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (e.id && !events(c.d).some(x => x.id === e.id) && c.d.timeline.loaded && !e.saving) {
    return wrap('event', `<div class="sheet__body sheet__body--form">${head}<span class="card__desc card__desc--lg">This item is gone.</span></div>`, title);
  }
  const projects = orderedProjects(c.d).filter(p => !p.is_general);
  const days = [...new Set([...REMIND_CHOICES.map(([d]) => d), ...e.remind])].sort((a, b) => a - b);
  const remindLabel = d => (REMIND_CHOICES.find(x => x[0] === d) || [d, `${d} days`])[1];
  const ready = e.title.trim() && e.date && e.pid;
  const label = s => `<span class="pal-label">${s}</span>`;
  // Who a meeting is with: the project's contacts (and its client's) as chips, everyone else in a picker.
  let withField = '';
  if (WITH_PEOPLE.includes(e.kind)) {
    const { near, others } = e.pid ? peopleChoices(c, 'project', e.pid) : { near: [], others: c.d.contacts || [] };
    const chosen = new Set(e.people);
    const shown = [...near, ...others.filter(x => chosen.has(x.id))];
    const rest = others.filter(x => !chosen.has(x.id));
    withField = `${label('WITH')}
      ${shown.length ? `<div class="chip-row chip-row--wrap" aria-label="Who it's with">${shown.map(x => `<button class="chip chip--sm${chosen.has(x.id) ? ' is-on' : ''}" data-action="evPerson" data-id="${x.id}" aria-pressed="${chosen.has(x.id)}">${esc(x.name)}</button>`).join('')}</div>` : ''}
      ${rest.length ? `<select class="input cn-pick" data-cl-change="evPersonPick" aria-label="Add a contact to the meeting"><option value="">${shown.length ? 'Someone else…' : 'Add a contact…'}</option>${rest.map(x => `<option value="${x.id}">${esc(x.name)}${x.role ? ' · ' + esc(x.role) : ''}</option>`).join('')}</select>` : ''}
      ${!shown.length && !rest.length ? '<span class="meta">No contacts saved yet - add them under Contacts.</span>' : ''}`;
  }
  // Which employees attend: active staff as chips (anyone already picked stays shown), the rest in a picker.
  if (WITH_PEOPLE.includes(e.kind) && (c.d.employees || []).length) {
    const chosen = new Set(e.staff || []);
    const staff = (c.d.employees || []).filter(x => x.status === 'active' || chosen.has(x.id));
    const shown = staff.filter(x => chosen.has(x.id)), rest = staff.filter(x => !chosen.has(x.id));
    const tag = x => `<button class="chip chip--sm is-on" data-action="evStaff" data-id="${x.id}" aria-pressed="true" title="${esc(x.job_title || '')}">${esc(x.name)}${x.away_today ? ' · away' : ''}</button>`;
    withField += `${label('TEAM')}
      ${shown.length ? `<div class="chip-row chip-row--wrap" aria-label="Team members attending">${shown.map(tag).join('')}</div>` : ''}
      ${rest.length ? `<select class="input cn-pick" data-cl-change="evStaffPick" aria-label="Add a team member to the meeting"><option value="">${shown.length ? 'Another team member…' : 'Add team members…'}</option>${rest.map(x => `<option value="${x.id}">${esc(x.name)}${x.job_title ? ' · ' + esc(x.job_title) : ''}${x.away_today ? ' (away today)' : ''}</option>`).join('')}</select>` : ''}`;
  }
  return wrap('event', `<div class="sheet__body sheet__body--form">
    ${head}
    ${label('WHAT')}<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Kind">${Object.entries(K).map(([k, v]) =>
      `<button class="chip${e.kind === k ? ' is-on' : ''}" role="radio" aria-checked="${e.kind === k}" data-action="evKind" data-kind="${k}"><span aria-hidden="true">${v.icon}</span> ${esc(v.label)}</button>`).join('')}</div>
    <input id="evTitle" class="input" maxlength="200" placeholder="${esc(K[e.kind] && K[e.kind].due ? 'e.g. Ship the checkout flow' : 'e.g. Client presentation with Acme')}" data-input="ev.title" aria-label="Title" value="${esc(e.title)}">
    ${label('PROJECT')}${projects.length ? `<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Project">${projects.map(p =>
      `<button class="chip${e.pid === p.id ? ' is-on' : ''}" role="radio" aria-checked="${e.pid === p.id}" data-action="evProject" data-id="${p.id}"><span class="swatch-sq" style="background:${projectColor(p, c.d)}"></span>${esc(p.name)}</button>`).join('')}</div>`
      : `<span class="card__desc">Add a project first - every date belongs to one.</span>`}
    ${withField}
    ${label('WHEN')}<div class="ev-when">
      <input id="evDate" class="input" type="date" data-input="ev.date" aria-label="Date" value="${esc(e.date)}">
      <input id="evTime" class="input" type="time" data-input="ev.time" aria-label="Time (optional)" value="${esc(e.time)}">
      ${e.time ? `<button class="btn--text btn--muted" data-action="evAllDay">All day</button>` : '<span class="meta">all day</span>'}
    </div>
    ${REPEATABLE.includes(e.kind) ? repeatField(e, label) : ''}
    ${WITH_PEOPLE.includes(e.kind) ? inviteField(c, e, label) : ''}
    ${label('REMIND ME')}<div class="chip-row chip-row--wrap" aria-label="Reminders">${days.map(d =>
      `<button class="chip chip--sm${e.remind.includes(d) ? ' is-on' : ''}" aria-pressed="${e.remind.includes(d)}" data-action="evRemind" data-d="${d}">${esc(remindLabel(d))}</button>`).join('')}</div>
    ${e.id ? `<button class="btn--text" data-action="openMeetingNotes" data-id="${e.id}">Open notes full-page</button>` : ''}
    ${polishLabel('NOTES', 'evNotes', e.notes, { kind: 'note' })}<textarea id="evNotes" class="input edit-prompt" rows="3" maxlength="4000" data-input="ev.notes" data-autogrow="240" aria-label="Notes" placeholder="Agenda, who's coming, what has to be ready…">${esc(e.notes)}</textarea>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveEvent"${e.saving || !ready ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : e.id ? 'Save' : 'Add'}</button>
      ${e.id ? `<button class="btn btn--ghost btn--44" data-action="evStatus" data-status="${e.status === 'open' ? 'done' : 'open'}">${e.status === 'open' ? 'Mark done' : 'Reopen'}</button>
        <button class="btn-dismiss" data-action="deleteEvent">Delete</button>` : ''}
    </div>
  </div>`, title);
}

// ---------------------------------------------------------------- Good morning
function briefRow(time, title, sub, attrs = '', tag = 'button') {
  return `<${tag} class="brief-row" ${attrs}><span class="brief-row__time">${esc(time)}</span><span class="brief-row__text"><span class="brief-row__title">${esc(title)}</span>${sub ? `<span class="meta">${esc(sub)}</span>` : ''}</span></${tag}>`;
}

function todayRows(c, b) {
  const rows = [];
  for (const e of b.today) rows.push({ sort: e.time || '', html: briefRow(e.time ? time12(e.time) : 'Today', `${e.icon} ${e.title}`, [e.kind_label, e.project_name, outstandingText(e.outstanding)].filter(Boolean).join(' · '), `data-action="openEvent" data-id="${e.id}"`) });
  for (const m of b.meetings || []) {
    const t = m.all_day ? '' : new Date(m.start).toTimeString().slice(0, 5);
    const attrs = m.link ? `href="${esc(m.link)}" target="_blank" rel="noopener"` : 'data-action="goPersonal" data-tab="calendar"';
    rows.push({ sort: t, html: briefRow(m.all_day ? 'All day' : time12(t), m.title, ['Calendar', m.location].filter(Boolean).join(' · '), attrs, m.link ? 'a' : 'button') });
  }
  for (const r of b.reminders) rows.push({ sort: new Date(r.due_at).toTimeString().slice(0, 5), html: briefRow(r.time, r.text, 'Reminder', 'data-action="goPersonal" data-tab="reminders"') });
  return rows.sort((a, b2) => a.sort.localeCompare(b2.sort)).map(r => r.html).join('');
}

export function briefingPage(c) {
  const b = c.d.briefing;
  if (!b) return `<div class="page page--narrow page--briefing" data-key="page-briefing"><div class="brief-hero">${octo('dance', 72)}<div class="brief-hero__text"><span class="brief-hero__hello">Putting your day together…</span><span class="meta">${spinner()} reading your timelines and calendar</span></div></div></div>`;
  const today = todayRows(c, b);
  const sec = (title, body, key, right = '') => `<section class="section" data-key="bf-${key}"><div class="section__head"><span class="section__title">${title}</span>${right}</div>${body}</section>`;
  const needs = [
    ...b.approvals.map(a => briefRow('Approve', a.title || 'An action is waiting', [a.project_name, a.tool].filter(Boolean).join(' · '), `data-action="openApproval" data-id="${a.id}"`)),
    ...b.attention.map(t => briefRow(t.status === 'error' ? 'Failed' : 'Review', t.title, t.project_name || '', `data-action="openChat" data-id="${t.task_id}"`)),
  ];
  const busy = c.ui.busy.briefing;
  return `<div class="page page--narrow page--briefing" data-key="page-briefing">
    <div class="brief-hero">${octo(b.overdue.length || b.approvals.length ? 'wait' : 'idle', 72)}
      <div class="brief-hero__text"><span class="topbar__eyebrow">${esc(b.day.toUpperCase())}</span><span class="brief-hero__hello">${esc(b.greeting)}, Tushar</span><span class="brief-hero__line">${esc(b.headline)}</span></div>
    </div>
    <div class="brief-acts"><button class="btn btn--40" data-action="briefingSpeak">🔊 Read it to me</button><button class="btn btn--ghost btn--40" data-action="briefingRefresh"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Refreshing…' : 'Refresh'}</button><button class="btn btn--ghost btn--40" data-action="go" data-view="calendar">Calendar</button></div>
    ${b.overdue.length ? sec(`Overdue <span class="section__count">${b.overdue.length}</span>`, `<div class="list">${b.overdue.map(e => evRow(c, e, { key: 'bo', date: true })).join('')}</div>`, 'overdue') : ''}
    ${sec('Today', today ? `<div class="list">${today}</div>` : `<span class="section__empty">No deadlines, meetings or reminders today.</span>`, 'today',
      b.calendar_connected ? '' : `<button class="btn--text" data-action="goPersonal" data-tab="connections">Connect calendar</button>`)}
    ${b.calendar_error ? `<span class="section__hint" style="color:var(--wait)">Couldn't read your calendar: ${esc(b.calendar_error)}</span>` : ''}
    ${needs.length ? sec(`Needs you <span class="section__count">${needs.length}</span>`, `<div class="list">${needs.join('')}</div>`, 'needs') : ''}
    ${b.upcoming.length ? sec('Coming up this week', `<div class="list">${b.upcoming.map(e => evRow(c, e, { key: 'bu', date: true })).join('')}</div>`, 'upcoming') : ''}
    ${b.at_risk.length ? sec('Keep an eye on', `<div class="list">${b.at_risk.map(r => `<button class="health-row" data-action="openProjectTimeline" data-id="${r.project_id}" data-key="bar${r.project_id}"><span class="swatch-sq" style="background:${projectColor(projectById(r.project_id, c.d), c.d)}"></span><span class="health-row__text"><span class="health-row__name">${esc(r.name)}</span><span class="meta health-row__why">${esc(r.reason)}</span></span>${healthPill(r)}</button>`).join('')}</div>`, 'risk') : ''}
    ${b.queue.running || b.queue.queued ? `<button class="brief-queue" data-action="go" data-view="home">Queue: ${b.queue.running} running, ${b.queue.queued} waiting →</button>` : ''}
  </div>`;
}

// ---------------------------------------------------------------- Settings section
export function timelineSettings(c) {
  const s = c.d.settings || {};
  const row = (k, l, desc, on) => `<div class="set-row"><div class="grow"><span class="set-row__label">${l}</span><span class="set-row__desc">${desc}</span></div>${toggle(on, 'timelineSetting', `data-k="${k}" aria-label="${esc(l)}"`)}</div>`;
  return `<div class="card card--list">
    ${row('notify_timeline', 'Deadline and meeting alerts', 'Push ahead of each date on a project timeline, and once when something goes overdue. Held during quiet hours (23:00 to 08:00).', s.notify_timeline !== false)}
    ${row('briefing_push', 'Good morning push', 'Your day in one notification: deadlines, meetings, approvals. Tap it for the full briefing.', s.briefing_push !== false)}
    <div class="set-row"><div class="grow"><span class="set-row__label">Morning push at</span><span class="set-row__desc">Skipped if the Mac is asleep for more than 3 hours past this time.</span></div>
      <input class="input set-time" type="time" data-input="set.briefing_time" aria-label="Morning push time" value="${esc(s.briefing_time || '08:30')}"${s.briefing_push === false ? ' disabled' : ''}></div>
  </div>`;
}
