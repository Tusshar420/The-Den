// The team (team.py): the Team page (a searchable list of employees) and one employee's page - details, tasks
// assigned to them, time off, direct reports and the meetings they attend (added to a meeting from the calendar).
// Data: c.d.employees (GET /employees, every poll) and c.d.employeePages[id] (GET /employees/{id} while it's open).
// UI state (team-ui.js): tmList { q, status }, tmNew { name, job_title, department, email, phone, saving, error },
// tmEdit { id, ...fields, saving, error }, tmTask { title, due_date, priority, saving, error }, tmLeave { kind,
// start_date, end_date, note, saving, error }, tmDel { id, busy }.

import { esc, icon, emptyState } from '../ui.js';
import { plural } from '../format.js';
import { projectById, projectColor } from '../data.js';
import { initials } from './contacts.js';

// [key, label, colour] - same keys as team.STATUSES on the server.
export const EMP_STATUSES = [['active', 'Active', 'var(--run)'], ['on_leave', 'On leave', 'var(--review)'], ['inactive', 'Inactive', 'var(--text-meta)']];
export const EMP_TYPES = [['full_time', 'Full time'], ['part_time', 'Part time'], ['contractor', 'Contractor'], ['intern', 'Intern']];
export const PRIORITIES = [['low', 'Low'], ['normal', 'Normal'], ['high', 'High']];
export const LEAVE_KINDS = [['vacation', 'Vacation'], ['sick', 'Sick'], ['other', 'Other']];
export const LIST_DEFAULT = { q: '', status: '' };
export const FIELDS = ['name', 'job_title', 'department', 'email', 'phone', 'status', 'employment_type', 'manager_id', 'start_date', 'location', 'skills', 'notes'];

const label = (list, k) => (list.find(x => x[0] === k) || [])[1] || '';
const statusOf = k => EMP_STATUSES.find(s => s[0] === k) || EMP_STATUSES[0];
export const statusChip = k => { const [, l, col] = statusOf(k); return `<span class="cl-status" style="--c:${col}">${l}</span>`; };
const TILE_COLORS = ['#e8825a', '#e8c24f', '#4fbf8a', '#4f9de8', '#b583e8', '#e86a8f', '#5fc4c9', '#9aa3b2'];
const tint = e => e.color || TILE_COLORS[(e.id || 0) % TILE_COLORS.length];
export const avatar = (e, cls = '') => `<span class="cn-av ${cls}" style="background:${tint(e)};color:var(--bg);border-color:transparent" aria-hidden="true">${esc(initials(e.name))}</span>`;
const fmtDay = d => (d ? new Date(d + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '');
const today = c => (c.d.timeline && c.d.timeline.today) || new Date().toISOString().slice(0, 10);

export function employeeMatches(e, q) {
  const terms = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = [e.name, e.job_title, e.department, e.email, e.phone, e.location, e.skills].filter(Boolean).join(' ').toLowerCase();
  return terms.every(t => hay.includes(t));
}

function chips(group, k, options, cur) {
  return `<div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${options.map(([v, l]) =>
    `<button class="segmented__opt${cur === v ? ' is-on' : ''}" role="radio" aria-checked="${cur === v}" data-action="tmSet" data-f="${group}" data-k="${k}" data-v="${v}">${l}</button>`).join('')}</div>`;
}

// The add / edit form for an employee. group: 'new' (tmNew) or 'edit' (tmEdit).
function employeeForm(c, f, group) {
  const act = group === 'new' ? 'tmNewSave' : 'tmUpdate';
  const inp = (k, lab, attrs = '') => `<label class="cn-f"><span class="cn-f__l">${lab}</span><input${k === 'name' ? ` id="tm-${group}-name"` : ''} class="input" value="${esc(f[k] || '')}" data-input="tm.${group}.${k}" data-enter="${act}" autocomplete="off" ${attrs}></label>`;
  const mgrs = (c.d.employees || []).filter(x => x.id !== f.id);
  const depts = [...new Set((c.d.employees || []).map(x => x.department).filter(Boolean))];
  return `<div class="cn-form" data-key="tm-form-${group}${f.id ? '-' + f.id : ''}">
    <span class="cn-form__title">${group === 'new' ? 'New employee' : 'Edit details'}</span>
    <div class="cn-form__grid">
      ${inp('name', 'Name', 'maxlength="120" placeholder="Full name" aria-required="true"')}
      ${inp('job_title', 'Job title', 'maxlength="120" placeholder="e.g. Product designer"')}
      ${inp('department', 'Department', `maxlength="120" list="tm-depts" placeholder="e.g. Engineering"`)}
      ${inp('email', 'Email', 'type="email" inputmode="email" maxlength="254" autocapitalize="off" spellcheck="false" placeholder="name@company.com"')}
      ${inp('phone', 'Phone', 'type="tel" inputmode="tel" maxlength="40" placeholder="+91 98765 43210"')}
      ${inp('location', 'Location', 'maxlength="120" placeholder="City or Remote"')}
      ${inp('start_date', 'Start date', 'type="date"')}
      ${inp('skills', 'Skills', 'maxlength="500" placeholder="Comma separated, e.g. Figma, React"')}
    </div>
    <datalist id="tm-depts">${depts.map(d => `<option value="${esc(d)}">`).join('')}</datalist>
    <div class="cn-f"><span class="cn-f__l">Status</span>${chips(group, 'status', EMP_STATUSES.map(s => [s[0], s[1]]), f.status || 'active')}</div>
    <div class="cn-f"><span class="cn-f__l">Employment</span>${chips(group, 'employment_type', EMP_TYPES, f.employment_type || 'full_time')}</div>
    <label class="cn-f"><span class="cn-f__l">Reports to</span><select class="input" data-cl-change="tmPick" data-f="${group}" data-k="manager_id" aria-label="Manager">
      <option value="">Nobody</option>${mgrs.map(m => `<option value="${m.id}"${String(f.manager_id || '') === String(m.id) ? ' selected' : ''}>${esc(m.name)}${m.job_title ? ' · ' + esc(m.job_title) : ''}</option>`).join('')}</select></label>
    <label class="cn-f"><span class="cn-f__l">Notes</span><textarea class="input cn-f__notes" rows="2" data-input="tm.${group}.notes" maxlength="20000" placeholder="Anything worth remembering">${esc(f.notes || '')}</textarea></label>
    <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="${act}"${f.saving ? ' disabled' : ''}>${f.saving ? 'Saving…' : group === 'new' ? 'Add employee' : 'Save'}</button>
      <button class="btn btn--ghost btn--sm" data-action="tmCancel" data-f="${group}">Cancel</button></div>
    ${f.error ? `<span class="form-err">${esc(f.error)}</span>` : ''}
  </div>`;
}

// ---------------------------------------------------------------- the list
function employeeRow(e, i) {
  const where = [e.job_title, e.department].filter(Boolean).map(esc).join(' · ');
  const work = e.open_tasks ? `<span class="meta">${plural(e.open_tasks, 'open task')}</span>` : '';
  return `<button class="cl-row" data-action="openEmployee" data-id="${e.id}" data-key="em${e.id}" style="animation-delay:${Math.min(i, 8) * 40}ms">
    ${avatar(e)}
    <span class="cl-row__text"><span class="cl-row__name truncate">${esc(e.name)}</span>
      <span class="cl-row__meta">${statusChip(e.status)}${where ? `<span class="truncate">${where}</span>` : ''}${e.away_today ? '<span class="tm-away">Away today</span>' : ''}</span></span>
    <span class="cl-row__value">${work}${e.overdue_tasks ? `<span class="tm-late">${e.overdue_tasks} overdue</span>` : ''}</span>
  </button>`;
}

export function teamPage(c) {
  const { d, ui } = c;
  const st = { ...LIST_DEFAULT, ...(ui.tmList || {}) };
  const all = d.employees || [];
  const n = ui.tmNew;
  if (!all.length && !n) {
    return `<div class="page page--narrow" data-key="team">${emptyState({ title: 'No employees yet', body: 'Keep your team here: their details, the tasks you give them, their time off, and the meetings they attend.', label: '+ Add employee', attrs: 'data-action="tmNewOpen"', key: 'tm-empty' })}</div>`;
  }
  const counts = Object.fromEntries(EMP_STATUSES.map(([k]) => [k, all.filter(x => x.status === k).length]));
  const list = all.filter(e => (!st.status || e.status === st.status) && employeeMatches(e, st.q));
  const away = all.filter(e => e.away_today).length, open = all.reduce((a, e) => a + (e.open_tasks || 0), 0), late = all.reduce((a, e) => a + (e.overdue_tasks || 0), 0);
  const chip = (k, l, cnt) => `<button class="chip${st.status === k ? ' is-on' : ''}" data-action="tmFilter" data-v="${k}">${l}${cnt != null ? ` <span class="chip__count">${cnt}</span>` : ''}</button>`;
  const stat = (v, l, cls = '') => `<div class="tm-stat${cls}"><b>${v}</b><span class="meta">${l}</span></div>`;
  return `<div class="page page--narrow cl-page" data-key="team">
    <div class="tm-stats">${stat(all.length, 'people')}${stat(counts.active, 'active')}${stat(away, 'away today')}${stat(open, 'open tasks')}${late ? stat(late, 'overdue', ' tm-stat--late') : ''}</div>
    <div class="cl-tools">
      <div class="search cl-search">${icon('search', 16, { strokeWidth: 1.8 })}<input id="tmSearch" type="search" placeholder="Search name, title, department, skills" value="${esc(st.q)}" data-input="tm.q" aria-label="Search employees" autocomplete="off"></div>
      ${n ? '' : `<button class="btn btn--primary btn--sm" data-action="tmNewOpen">+ Add employee</button>`}
    </div>
    <div class="chip-row" data-noswipe>${chip('', 'All', all.length)}${EMP_STATUSES.map(([k, l]) => chip(k, l, counts[k])).join('')}</div>
    ${n ? `<div class="card card--pad cl-new" data-key="tm-new">${employeeForm(c, n, 'new')}</div>` : ''}
    <div class="cl-list">${list.length ? list.map(employeeRow).join('') : `<span class="card__desc cl-none">No employee matches${st.q.trim() ? ` “${esc(st.q.trim())}”` : ''}.</span>`}</div>
  </div>`;
}

// ---------------------------------------------------------------- one employee
function tasksCard(c, e) {
  const t = c.ui.tmTask, tod = today(c);
  const row = x => {
    const done = x.status === 'done', late = !done && x.due_date && x.due_date < tod;
    const p = x.project_id ? projectById(x.project_id, c.d) : null;
    return `<div class="tm-task${done ? ' is-done' : ''}" data-key="tt${x.id}">
      <button class="tm-check${done ? ' is-on' : ''}" data-action="tmTaskDone" data-id="${x.id}" role="checkbox" aria-checked="${done}" aria-label="${done ? 'Reopen' : 'Mark done'}: ${esc(x.title)}">${done ? '✓' : ''}</button>
      <div class="tm-task__body"><span class="tm-task__title">${esc(x.title)}</span>
        <span class="tm-task__meta">${x.status === 'in_progress' ? '<span class="tm-pill">In progress</span>' : ''}${x.priority === 'high' ? '<span class="tm-pill tm-pill--high">High</span>' : x.priority === 'low' ? '<span class="tm-pill">Low</span>' : ''}${x.due_date ? `<span class="${late ? 'tm-late' : 'meta'}">${late ? 'Overdue · ' : 'Due '}${esc(fmtDay(x.due_date))}</span>` : ''}${p ? `<span class="meta"><span class="nav-proj__swatch" style="background:${projectColor(p)}"></span> ${esc(p.name)}</span>` : ''}</span>
        ${x.notes ? `<span class="cn-notes">${esc(x.notes)}</span>` : ''}</div>
      <div class="cn-acts">${done ? '' : `<button class="btn btn--ghost btn--sm" data-action="tmTaskStart" data-id="${x.id}" data-v="${x.status === 'in_progress' ? 'open' : 'in_progress'}">${x.status === 'in_progress' ? 'Pause' : 'Start'}</button>`}
        <button class="icon-btn icon-btn--ghost" data-action="tmTaskDel" data-id="${x.id}" aria-label="Delete task" title="Delete task">✕</button></div>
    </div>`;
  };
  const projects = (c.d.projects || []).filter(p => !p.is_general);
  const form = t ? `<div class="cn-form" data-key="tm-task-form">
      <label class="cn-f"><span class="cn-f__l">Task</span><input id="tm-task-title" class="input" value="${esc(t.title)}" data-input="tm.task.title" data-enter="tmTaskSave" maxlength="200" placeholder="What needs doing?" autocomplete="off"></label>
      <div class="cn-form__grid">
        <label class="cn-f"><span class="cn-f__l">Due</span><input class="input" type="date" value="${esc(t.due_date || '')}" data-input="tm.task.due_date"></label>
        <label class="cn-f"><span class="cn-f__l">Project</span><select class="input" data-cl-change="tmPick" data-f="task" data-k="project_id"><option value="">None</option>${projects.map(p => `<option value="${p.id}"${String(t.project_id || '') === String(p.id) ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>
      </div>
      <div class="cn-f"><span class="cn-f__l">Priority</span>${chips('task', 'priority', PRIORITIES, t.priority || 'normal')}</div>
      <label class="cn-f"><span class="cn-f__l">Notes</span><textarea class="input cn-f__notes" rows="2" data-input="tm.task.notes" maxlength="20000" placeholder="Optional details">${esc(t.notes || '')}</textarea></label>
      <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="tmTaskSave"${t.saving ? ' disabled' : ''}>${t.saving ? 'Adding…' : 'Assign task'}</button>
        <button class="btn btn--ghost btn--sm" data-action="tmTaskCancel">Cancel</button></div>
      ${t.error ? `<span class="form-err">${esc(t.error)}</span>` : ''}</div>` : '';
  const tasks = e.tasks || [], open = tasks.filter(x => x.status !== 'done').length;
  return `<section class="card card--pad crm-card" data-key="tm-tasks"><div class="crm-card__head"><span class="card__title">Tasks${tasks.length ? ` <span class="meta">${open} open · ${tasks.length - open} done</span>` : ''}</span>
      ${t ? '' : `<button class="btn--text" data-action="tmTaskOpen">+ Assign task</button>`}</div>
    ${tasks.length ? `<div class="tm-tasks">${tasks.map(row).join('')}</div>` : t ? '' : `<span class="card__desc">Nothing assigned yet. Give ${esc(e.name.split(' ')[0])} something to do.</span>`}
    ${form}</section>`;
}

function meetingsCard(c, e) {
  const row = m => {
    const p = projectById(m.project_id, c.d);
    return `<button class="cl-proj tm-meet" data-action="openEvent" data-id="${m.id}" data-key="tmm${m.id}"><span class="nav-proj__swatch" style="background:${p ? projectColor(p) : 'var(--text-meta)'}"></span>
      <span class="cl-proj__name truncate">${esc(m.title)}</span><span class="meta">${esc(m.when || m.date)}${p ? ' · ' + esc(p.name) : ''}</span></button>`;
  };
  const up = e.meetings || [];
  return `<section class="card card--pad crm-card" data-key="tm-meetings"><div class="crm-card__head"><span class="card__title">Meetings${up.length ? ` <span class="meta">${up.length} upcoming</span>` : ''}</span>
      <button class="btn--text" data-action="openEvent" data-kind="meeting">+ New meeting</button></div>
    ${up.length ? `<div class="cl-projs">${up.map(row).join('')}</div>` : `<span class="card__desc">Not in any upcoming meeting. Add ${esc(e.name.split(' ')[0])} when you create a meeting on the calendar.</span>`}
    ${(e.past_meetings || []).length ? `<span class="tm-sub">Recent</span><div class="cl-projs">${e.past_meetings.slice().reverse().map(row).join('')}</div>` : ''}</section>`;
}

function leaveCard(c, e) {
  const l = c.ui.tmLeave, tod = today(c);
  const rows = (e.leave || []).filter(x => x.end_date >= tod).map(x => `<div class="tm-leave" data-key="tl${x.id}"><span class="tm-pill">${esc(label(LEAVE_KINDS, x.kind))}</span>
    <span class="grow">${esc(fmtDay(x.start_date))}${x.end_date !== x.start_date ? ' – ' + esc(fmtDay(x.end_date)) : ''}${x.note ? `<span class="meta"> · ${esc(x.note)}</span>` : ''}</span>
    <button class="icon-btn icon-btn--ghost" data-action="tmLeaveDel" data-id="${x.id}" aria-label="Remove time off" title="Remove">✕</button></div>`).join('');
  const form = l ? `<div class="cn-form" data-key="tm-leave-form">
      <div class="cn-f"><span class="cn-f__l">Kind</span>${chips('leave', 'kind', LEAVE_KINDS, l.kind || 'vacation')}</div>
      <div class="cn-form__grid">
        <label class="cn-f"><span class="cn-f__l">From</span><input class="input" type="date" value="${esc(l.start_date || '')}" data-input="tm.leave.start_date"></label>
        <label class="cn-f"><span class="cn-f__l">To</span><input class="input" type="date" value="${esc(l.end_date || '')}" data-input="tm.leave.end_date"></label>
      </div>
      <label class="cn-f"><span class="cn-f__l">Note</span><input class="input" value="${esc(l.note || '')}" data-input="tm.leave.note" data-enter="tmLeaveSave" maxlength="120" placeholder="Optional" autocomplete="off"></label>
      <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="tmLeaveSave"${l.saving ? ' disabled' : ''}>${l.saving ? 'Saving…' : 'Add time off'}</button>
        <button class="btn btn--ghost btn--sm" data-action="tmLeaveCancel">Cancel</button></div>
      ${l.error ? `<span class="form-err">${esc(l.error)}</span>` : ''}</div>` : '';
  return `<section class="card card--pad crm-card" data-key="tm-leave"><div class="crm-card__head"><span class="card__title">Time off</span>
      ${l ? '' : `<button class="btn--text" data-action="tmLeaveOpen">+ Add time off</button>`}</div>
    ${rows ? `<div class="tm-leaves">${rows}</div>` : l ? '' : '<span class="card__desc">No time off coming up.</span>'}${form}</section>`;
}

function detailsCard(c, e) {
  const ed = c.ui.tmEdit;
  if (ed && ed.id === e.id) return `<section class="card card--pad crm-card" data-key="tm-details">${employeeForm(c, ed, 'edit')}</section>`;
  const row = (l, v) => (v ? `<div class="tm-kv"><span class="tm-kv__l">${l}</span><span class="tm-kv__v">${v}</span></div>` : '');
  const mail = e.email ? `<a class="cn-link" href="mailto:${esc(e.email)}">${esc(e.email)}</a>` : '';
  const tel = e.phone ? `<a class="cn-link" href="tel:${esc(e.phone.replace(/[^0-9+]/g, ''))}">${esc(e.phone)}</a>` : '';
  const mgr = e.manager ? `<button class="cn-link tm-link" data-action="openEmployee" data-id="${e.manager.id}">${esc(e.manager.name)}</button>` : '';
  const reports = (e.reports || []).map(r => `<button class="cn-link tm-link" data-action="openEmployee" data-id="${r.id}">${esc(r.name)}</button>`).join(', ');
  const skills = e.skills ? e.skills.split(',').map(s => s.trim()).filter(Boolean).map(s => `<span class="tm-pill">${esc(s)}</span>`).join(' ') : '';
  return `<section class="card card--pad crm-card" data-key="tm-details"><div class="crm-card__head"><span class="card__title">Details</span>
      <button class="btn--text" data-action="tmEdit">Edit</button></div>
    ${row('Email', mail)}${row('Phone', tel)}${row('Department', esc(e.department || ''))}${row('Employment', esc(label(EMP_TYPES, e.employment_type)))}
    ${row('Location', esc(e.location || ''))}${row('Started', esc(fmtDay(e.start_date)))}${row('Reports to', mgr)}${row('Manages', reports)}${row('Skills', skills)}
    ${e.notes ? `<span class="cn-notes">${esc(e.notes)}</span>` : ''}</section>`;
}

export function employeePage(c) {
  const id = c.ui.employeeId, e = (c.d.employeePages || {})[id];
  if (e === undefined) return `<div class="page page--narrow" data-key="employee-loading"><span class="meta">Loading…</span></div>`;
  if (!e) return `<div class="page page--narrow" data-key="employee-gone">${emptyState({ title: 'This employee is gone', body: 'They were deleted, maybe from another device.', label: 'Team', attrs: 'data-action="go" data-view="team"', key: 'tm-gone' })}</div>`;
  const del = c.ui.tmDel && c.ui.tmDel.id === e.id;
  return `<div class="page page--wide cl-page" data-key="employee-${e.id}">
    <div class="cl-head" data-key="tm-head">${avatar(e, 'cn-av--lg')}
      <div class="cl-head__text"><span class="cl-head__name">${esc(e.name)}</span>
        <span class="cl-row__meta">${statusChip(e.status)}${e.job_title ? `<span>${esc(e.job_title)}</span>` : ''}${e.away_today ? '<span class="tm-away">Away today</span>' : ''}</span></div>
      <button class="btn btn--ghost btn--sm cl-head__back" data-action="go" data-view="team">Team</button></div>
    <div class="crm-grid">
      <div class="crm-col">${tasksCard(c, e)}${meetingsCard(c, e)}</div>
      <div class="crm-col">${detailsCard(c, e)}${leaveCard(c, e)}
        <section class="card card--pad crm-card" data-key="tm-danger">${del
          ? `<span class="card__desc">Delete ${esc(e.name)}? Their tasks and time off go too, and they're taken off meetings.</span>
             <div class="crm-ed__acts"><button class="btn btn--danger btn--sm" data-action="tmDelete"${c.ui.tmDel.busy ? ' disabled' : ''}>Delete for good</button><button class="btn btn--ghost btn--sm" data-action="tmDelCancel">Keep</button></div>`
          : `<button class="btn--text" data-action="tmDelOpen">Delete employee…</button>`}</section></div>
    </div>
  </div>`;
}
