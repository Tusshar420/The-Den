// To-do checklist: the right-sidebar panel (all to-dos, filterable by project), a project's own To-dos tab,
// and either of them opened full screen. Each to-do is To do, In progress or Done; the chips on top show
// one state at a time, and in "My order" rows can be dragged.
// Actions and form inputs live in checklist-ui.js; data.js has loadChecklist / the checklist endpoints.

import { esc, icon } from '../ui.js';
import { projectById, projectColor } from '../data.js';
import { chkState, chkHistory, statusOf } from '../checklist-ui.js';

// Open (not ticked) to-dos, of one project or of all.
export const openCount = (d, pid = null) => d.checklist.items.filter(i => !i.done && (pid == null || i.project_id === pid)).length;

const STATES = [['all', 'All'], ['todo', 'To do'], ['doing', 'In progress'], ['done', 'Done']];
const GROUPS = [['doing', 'IN PROGRESS'], ['todo', 'TO DO'], ['done', 'DONE']];
const EMPTY = {
  todo: ['Nothing waiting', 'Everything is started or done.'],
  doing: ['Nothing in progress', 'Press ▶ on a to-do to start it.'],
  done: ['Nothing done yet', 'Ticked to-dos show up here.'],
};
const SORTS = [['manual', 'My order'], ['new', 'Newest first'], ['old', 'Oldest first'], ['az', 'A to Z']];
const SORT_FN = {
  manual: (a, b) => a.position - b.position || a.id - b.id,
  new: (a, b) => b.id - a.id,
  old: (a, b) => a.id - b.id,
  az: (a, b) => a.text.localeCompare(b.text, undefined, { sensitivity: 'base', numeric: true }) || a.id - b.id,
};

const lineCount = t => (t || '').split(/\r?\n/).filter(l => l.trim()).length;

function projectOptions(c, sel, none) {
  return `<option value=""${sel == null || sel === '' ? ' selected' : ''}>${none}</option>${c.d.projects.map(p => `<option value="${p.id}"${String(sel) === String(p.id) ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}`;
}

// The box new to-dos are typed or pasted into. With a project (p) they always go to it; otherwise a picker chooses.
function addBox(c, ctx, p) {
  const s = chkState(c.ui), n = lineCount(s.text);
  const dest = s.dest !== null ? s.dest : typeof s.filter === 'number' ? s.filter : '';
  const pid = p ? p.id : '';
  return `<div class="chk-add">
    <textarea id="chkInput-${ctx}" class="input chk-add__text" rows="3" data-key="chk-input-${ctx}" data-input="chk.text" data-enter="chkAdd" data-pid="${pid}" data-autogrow="${ctx === 'full' ? 480 : 320}" data-live-style placeholder="Add a to-do. Paste several lines to add them all at once." aria-label="New to-do">${esc(s.text)}</textarea>
    <div class="chk-add__row">
      ${p ? '<span class="chk-add__hint grow">Enter to add · Shift+Enter for a new line</span>' : `<select class="input chk-add__proj" data-key="chk-dest-${ctx}" data-input="chk.dest" aria-label="Save under project">${projectOptions(c, dest, 'No project')}</select>`}
      <button class="btn btn--primary btn--sm" data-action="chkAdd" data-pid="${pid}"${s.text.trim() ? '' : ' disabled'}>${n > 1 ? `Add ${n} to-dos` : 'Add'}</button>
    </div>
  </div>`;
}

function filterSelect(c, ctx) {
  const f = chkState(c.ui).filter;
  return `<select class="input chk-filter" data-key="chk-filter-${ctx}" data-input="chk.filter" aria-label="Show to-dos of">
    <option value="all"${f === 'all' ? ' selected' : ''}>All to-dos</option><option value="none"${f === 'none' ? ' selected' : ''}>No project</option>
    ${c.d.projects.map(p => `<option value="${p.id}"${f === p.id ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}</select>`;
}

// State chips with counts, the sort, undo / redo and full screen. `full` is what the full-screen button opens ('side' or a project id).
function toolbar(c, all, ctx, full) {
  const s = chkState(c.ui), h = chkHistory();
  const n = st => (st === 'all' ? all.length : all.filter(i => statusOf(i) === st).length);
  const histBtn = (act, ic, verb, label, keys) => `<button class="icon-btn icon-btn--ghost chk-bar__btn" data-action="${act}" aria-label="${verb}"${label ? ` title="${verb}: ${esc(label)} (${keys})"` : ` title="Nothing to ${verb.toLowerCase()}" disabled`}>${icon(ic, 17)}</button>`;
  return `<div class="chk-bar">
    <div class="chip-row chk-states" role="tablist" aria-label="Show to-dos that are">${STATES.map(([k, l]) => `<button class="chip chk-chip${s.state === k ? ' is-on' : ''}${k === 'doing' ? ' chk-chip--doing' : ''}" role="tab" aria-selected="${s.state === k}" data-action="chkShow" data-v="${k}">${l}<span class="chip__count">${n(k)}</span></button>`).join('')}</div>
    <div class="chk-bar__row">
      <select class="input chk-sort" data-key="chk-sort-${ctx}" data-input="chk.sort" aria-label="Sort">${SORTS.map(([k, l]) => `<option value="${k}"${s.sort === k ? ' selected' : ''}>${l}</option>`).join('')}</select>
      <span class="grow"></span>
      ${histBtn('chkUndo', 'undo', 'Undo', h.undo, `${h.mod}Z`)}${histBtn('chkRedo', 'redo', 'Redo', h.redo, `${h.mod === '⌘' ? '⇧⌘' : 'Ctrl+Shift+'}Z`)}
      ${ctx === 'full' ? '' : `<button class="icon-btn icon-btn--ghost chk-bar__btn" data-action="chkFull" data-ctx="${full}" aria-label="Full screen" title="Full screen">${icon('expand', 16)}</button>`}
    </div>
  </div>`;
}

function row(c, it, showProject, drag) {
  const s = chkState(c.ui), st = statusOf(it);
  if (s.edit === it.id) {
    return `<div class="chk-row is-editing" data-id="${it.id}" data-key="chk-${it.id}">
      <textarea class="input chk-edit__text" rows="3" data-input="chk.editText" data-enter="chkSave" data-autogrow="320" aria-label="Edit to-do">${esc(s.editText)}</textarea>
      <div class="chk-add__row">
        <select class="input chk-add__proj" data-input="chk.editProject" aria-label="Project">${projectOptions(c, s.editProject, 'No project')}</select>
        <button class="btn btn--sm" data-action="chkEditCancel">Cancel</button>
        <button class="btn btn--primary btn--sm" data-action="chkSave"${s.editText.trim() ? '' : ' disabled'}>Save</button>
      </div>
    </div>`;
  }
  const p = showProject && it.project_id != null ? projectById(it.project_id) : null;
  const move = st === 'todo' ? `<button class="icon-btn icon-btn--ghost chk-row__go" data-action="chkStatus" data-id="${it.id}" data-v="doing" aria-label="Start: mark in progress" title="Start (mark in progress)">${icon('play', 16)}</button>`
    : st === 'doing' ? `<button class="icon-btn icon-btn--ghost" data-action="chkStatus" data-id="${it.id}" data-v="todo" aria-label="Stop: move back to To do" title="Not started yet (back to To do)">${icon('pause', 16)}</button>` : '';
  return `<div class="chk-row is-${st}${st === 'done' ? ' is-done' : ''}" data-id="${it.id}" data-key="chk-${it.id}">
    ${drag ? '<span class="handle chk-row__handle" aria-label="Drag to reorder" title="Drag to reorder">⋮⋮</span>' : ''}
    <button class="check chk-check" role="checkbox" aria-checked="${st === 'done' ? 'true' : st === 'doing' ? 'mixed' : 'false'}" data-action="chkToggle" data-id="${it.id}" aria-label="${st === 'done' ? 'Mark not done' : 'Mark done'}">${st === 'done' ? '✓' : ''}</button>
    <div class="chk-row__body" data-action="chkToggle" data-id="${it.id}">
      <span class="chk-row__text">${esc(it.text)}</span>
      ${st === 'doing' || p ? `<span class="chk-row__meta">${st === 'doing' ? '<span class="chk-badge">In progress</span>' : ''}${p ? `<span class="chk-row__proj"><span class="chip__dot" style="background:${projectColor(p, c.d)}"></span>${esc(p.name)}</span>` : ''}</span>` : ''}
    </div>
    <div class="chk-row__acts">
      ${move}
      <button class="icon-btn icon-btn--ghost" data-action="chkEdit" data-id="${it.id}" aria-label="Edit" title="Edit or move to a project">✎</button>
      <button class="icon-btn icon-btn--ghost" data-action="chkDelete" data-id="${it.id}" aria-label="Delete" title="Delete">${icon('trash', 16)}</button>
    </div>
  </div>`;
}

// One block per state (In progress, To do, Done), or just the state the chips pick. Done has Clear.
function list(c, all, showProject, pid) {
  const s = chkState(c.ui);
  if (!all.length) return `<div class="chk-empty"><b>No to-dos yet</b><span>Type one above, or paste a list and each line becomes its own to-do.</span></div>`;
  const drag = (s.sort === 'manual' || !SORT_FN[s.sort]) && s.edit == null;
  const of = st => all.filter(i => statusOf(i) === st).sort(SORT_FN[s.sort] || SORT_FN.manual);
  const rows = (st, its) => `<div class="chk-list"${drag && its.length > 1 ? ` data-chk-drag="${st}"` : ''}>${its.map(i => row(c, i, showProject, drag && its.length > 1)).join('')}</div>`;
  if (s.state !== 'all') {
    const its = of(s.state), [title, hint] = EMPTY[s.state] || EMPTY.todo;
    if (!its.length) return `<div class="chk-empty"><b>${title}</b><span>${hint}</span></div>`;
    return `${s.state === 'done' ? doneHead(its.length, pid) : ''}${rows(s.state, its)}`;
  }
  const doing = of('doing'), todo = of('todo'), done = of('done');
  return `${doing.length ? `<div class="chk-donehead"><span class="qp-group__label chk-label--doing">IN PROGRESS · ${doing.length}</span></div>${rows('doing', doing)}` : ''}
    ${todo.length ? `${doing.length ? `<div class="chk-donehead"><span class="qp-group__label">TO DO · ${todo.length}</span></div>` : ''}${rows('todo', todo)}`
      : doing.length ? '' : `<div class="chk-empty chk-empty--ok"><b>All done</b><span>Nothing left on this list.</span></div>`}
    ${done.length ? `${doneHead(done.length, pid)}${rows('done', done)}` : ''}`;
}

const doneHead = (n, pid) => `<div class="chk-donehead"><span class="qp-group__label">DONE · ${n}</span><button class="btn--text" data-action="chkClearDone" data-pid="${pid == null ? '' : pid}">Clear done</button></div>`;

// The sidebar's items: everything, or what its project filter picks.
const sideItems = c => {
  const f = chkState(c.ui).filter;
  return c.d.checklist.items.filter(i => f === 'all' || (f === 'none' ? i.project_id == null : i.project_id === f));
};

// Right sidebar: everything, with a project filter on top.
export function checklistSide(c) {
  const f = chkState(c.ui).filter, all = sideItems(c);
  return `<div class="chk">${addBox(c, 'side', null)}${filterSelect(c, 'side')}${toolbar(c, all, 'side', 'side')}${list(c, all, typeof f !== 'number', typeof f === 'number' ? f : null)}</div>`;
}

// A project's To-dos tab: only that project's items.
export function checklistTab(c, p) {
  const all = c.d.checklist.items.filter(i => i.project_id === p.id);
  const n = openCount(c.d, p.id);
  return `<div class="chk chk--tab">
    <div class="section__head"><span class="section__title">To-dos <span class="section__count">${n}</span></span></div>
    ${addBox(c, 'proj', p)}${toolbar(c, all, 'proj', p.id)}${list(c, all, false, p.id)}
    <span class="section__hint">Quick tick-off items. They also show in the To-dos panel on the right. To queue prompts for Claude to run, use the Backlog.</span>
  </div>`;
}

// Either list above, filling the screen. Esc or the button closes it.
export function checklistFull(c) {
  const s = chkState(c.ui);
  if (s.full == null) return '';
  const p = s.full === 'side' ? null : projectById(s.full);
  const all = p ? c.d.checklist.items.filter(i => i.project_id === p.id) : sideItems(c);
  const pid = p ? p.id : typeof s.filter === 'number' ? s.filter : null;
  return `<div class="chk-full" data-key="chk-full" role="dialog" aria-modal="true" aria-label="To-dos, full screen">
    <div class="chk chk--full">
      <div class="chk-full__head">
        <span class="section__title">${p ? `${esc(p.name)} · To-dos` : 'To-dos'} <span class="section__count">${all.filter(i => !i.done).length}</span></span>
        <button class="btn btn--sm" data-action="chkFull" title="Exit full screen (Esc)">${icon('collapse', 16)} Exit full screen</button>
      </div>
      ${addBox(c, 'full', p)}${p ? '' : filterSelect(c, 'full')}${toolbar(c, all, 'full', '')}${list(c, all, !p && typeof s.filter !== 'number', pid)}
    </div>
  </div>`;
}
