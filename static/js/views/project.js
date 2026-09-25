// Project detail: header, paused banner, stats, and the Work / Activity / Recipes /
// Settings tabs. Backlog = the existing todos API.

import { money, plural, priorityLabel, agoShort, ago, statusOf, fmtCost, taskName } from '../format.js';
import { projectColor, queuedTasks, threadSummaries, finishedAt, PROJECT_STATUSES, NOTE_KINDS } from '../data.js';
import { esc, octo, chipFor, histRow, toggle, glyph, took, scopeSwitch } from '../ui.js';
import { octopusSVG } from '../octopus.js';
import { recipeCard } from './recipes.js';

const TABS = [['work', 'Work'], ['notes', 'Notes'], ['activity', 'Activity'], ['recipes', 'Recipes'], ['settings', 'Settings']];
const tildify = p => (p || '').replace(/^\/Users\/[^/]+/, '~');

function work(c, p) {
  const { d, w, ui } = c;
  const pt = d.tasks.filter(t => t.project_id === p.id);
  const live = pt.filter(t => !t.dismissed && (w.apTask.has(t.id) || ['running', 'error', 'needs_review', 'rate_limited'].includes(t.status)));
  const queue = queuedTasks(d);
  const mine = queue.map((t, i) => ({ t, pos: i + 1 })).filter(x => x.t.project_id === p.id);
  const done = threadSummaries(d).filter(t => t.project_id === p.id && (t.status === 'done' || t.status === 'cancelled'))
    .sort((a, b) => finishedAt(b).localeCompare(finishedAt(a))).slice(0, 4);
  const todos = (d.todos[p.id] || []).filter(t => t.status === 'pending');
  const sel = todos.filter(t => t.checked);
  const allSel = todos.length && sel.length === todos.length;

  const liveHTML = live.length ? `<div class="section"><div class="section__head"><span class="section__title">Live</span></div>
    ${live.map(t => {
      const st = w.uiStatus(t);
      const s = st === 'approval' ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(t);
      const a = w.apTask.get(t.id);
      const act = a ? `data-action="openApproval" data-id="${a.id}"` : `data-action="openChat" data-id="${t.id}"`;
      const meta = [ago(t.started_at || t.created_at, c.now), t.current_step && st === 'running' ? t.current_step : '', fmtCost(t.cost_usd)].filter(Boolean).join(' · ');
      return `<button class="live-card" ${act} style="--c:${s.color}" data-key="lv${t.id}"><span class="live-card__label">${s.label.toUpperCase()}</span><span class="live-card__title">${esc(taskName(t))}</span><span class="meta">${esc(meta)}</span></button>`;
    }).join('')}</div>` : '';

  const queueHTML = `<div class="section">
    <div class="section__head"><span class="section__title">In the queue <span class="section__count">${mine.length}</span></span><span class="section__note">global position</span></div>
    ${mine.length ? `<div class="list">${mine.map(({ t, pos }) => `<div class="pq-row" data-action="openChat" data-id="${t.id}" data-key="pq${t.id}">
        <span class="pq-row__pos">#${pos}</span><span class="truncate pq-row__title">${esc(taskName(t))}</span><span class="pq-row__meta">${chipFor(t)}<span class="meta">${priorityLabel(t.priority)}</span></span></div>`).join('')}</div>`
      : `<span class="section__empty">Nothing queued for this project.</span>`}
  </div>`;

  const doneHTML = `<div class="section"><div class="section__head"><span class="section__title">Done recently</span></div>
    ${done.length ? `<div class="list">${done.map(t => histRow(t, { project: false, key: 'pd' })).join('')}</div>` : `<span class="section__empty">Nothing finished yet.</span>`}</div>`;

  const backlog = `<div class="section section--backlog">
    <div class="section__head"><span class="section__title">Backlog <span class="section__count">${todos.length}</span></span>${todos.length ? `<button class="btn--text" data-action="backlogSelectAll" data-all="${allSel ? 1 : 0}">${allSel ? 'Clear' : 'Select all'}</button>` : ''}</div>
    <form class="backlog-add" data-submit="addTodo"><textarea id="todoInput" class="input grow backlog-add__input" rows="3" placeholder="Add to backlog… (Shift+Enter for a new line)" data-input="todoText" data-enter="submitTodo" data-autogrow="260" aria-label="Add to backlog">${esc(ui.todoText)}</textarea><button class="btn btn--48" type="submit">Add</button></form>
    ${todos.length ? `<div class="list list--backlog" data-drag-list="backlog" data-pid="${p.id}">${todos.map(t => `<div class="qrow brow" data-id="${t.id}" data-key="b${t.id}">
        <span class="handle" aria-label="Drag to reorder">⋮⋮</span>
        <button class="check" role="checkbox" aria-checked="${t.checked ? 'true' : 'false'}" data-action="todoCheck" data-id="${t.id}" aria-label="Select">${t.checked ? '✓' : ''}</button>
        <span class="brow__title truncate" data-action="todoCheck" data-id="${t.id}">${esc(t.text)}</span>
        <button class="icon-btn icon-btn--ghost brow__edit" data-action="openTodoEdit" data-id="${t.id}" aria-label="Edit">✎</button>
      </div>`).join('')}</div>` : ''}
    ${sel.length ? `<div class="bulkbar"><span class="bulkbar__text">${sel.length} selected · joins after ${plural(queue.length, 'task')}</span>${allSel ? '' : `<button class="bulkbar__all" data-action="backlogSelectAll" data-all="0">Select all</button>`}<button class="bulkbar__del" data-action="backlogDelete">Delete</button><button class="bulkbar__run" data-action="backlogRun"${ui.busy.backlogRun ? ' disabled' : ''}>Run selected</button></div>` : ''}
    <span class="section__hint">Drag to set the order. Selected items join the queue in this order.</span>
  </div>`;

  return `<div class="proj-work" data-key="tab-work">
    <div class="page__col">${liveHTML}${queueHTML}${doneHTML}</div>
    <div class="page__col">${backlog}</div>
  </div>`;
}

// What you've told me about this project. Every task I run here starts with these in mind.
function notes(c, p) {
  const { d, ui } = c;
  const list = d.notes[p.id] || [];
  const kindOf = k => NOTE_KINDS.find(x => x[0] === k) || NOTE_KINDS[2];
  return `<div class="notes" data-key="tab-notes">
    <div class="section">
      <div class="section__head"><span class="section__title">Project notes <span class="section__count">${list.length}</span></span></div>
      <span class="section__hint">Guidelines, requirements, anything I should know. I keep all of these in mind for every task on this project.</span>
      <form class="note-add" data-submit="addNote">
        <div class="chip-row" role="radiogroup" aria-label="Kind of note" data-noswipe>${NOTE_KINDS.map(([k, l, hint]) =>
          `<button type="button" class="chip${ui.noteKind === k ? ' is-on' : ''}" role="radio" aria-checked="${ui.noteKind === k}" title="${esc(hint)}" data-action="setNoteKind" data-kind="${k}" data-key="nk-${k}">${l}</button>`).join('')}</div>
        <div class="backlog-add"><textarea id="noteInput" class="input grow backlog-add__input" rows="3" placeholder="Add a ${kindOf(ui.noteKind)[1].toLowerCase()}… (Shift+Enter for a new line)" data-input="noteText" data-enter="submitNote" data-autogrow="260" aria-label="Add a project note">${esc(ui.noteText)}</textarea><button class="btn btn--48" type="submit">Add</button></div>
      </form>
      ${list.length ? `<div class="list list--notes">${list.map(n => `<div class="qrow note-row" data-id="${n.id}" data-key="n${n.id}">
        <span class="note-row__kind note-row__kind--${esc(n.kind)}">${kindOf(n.kind)[1]}</span>
        <span class="note-row__text">${esc(n.text)}</span>
        <button class="icon-btn icon-btn--ghost brow__edit" data-action="openNoteEdit" data-id="${n.id}" aria-label="Edit note">✎</button>
      </div>`).join('')}</div>` : `<span class="section__empty">Nothing yet. Add the first one above.</span>`}
    </div>
  </div>`;
}

function activity(c, p) {
  const tasks = c.d.tasks.filter(t => t.project_id === p.id).sort((a, b) => b.id - a.id).slice(0, 60);
  if (!tasks.length) return `<div class="empty" data-key="tab-activity">No activity yet.</div>`;
  return `<div class="list list--720" data-key="tab-activity">${tasks.map(t => {
    const st = c.w.uiStatus(t);
    const s = st === 'approval' ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(t);
    const [g, gc] = glyph(t);
    const meta = [s.label, fmtCost(t.cost_usd), took(t)].filter(Boolean).join(' · ');
    return `<div class="event-row" data-key="ev${t.id}" data-action="openChat" data-id="${t.id}">
      <span class="meta">${esc(agoShort(t.finished_at || t.created_at, c.now))}</span>
      <span class="row__glyph" style="color:${g === '·' ? s.color : gc}">${g === '·' ? '●' : g}</span>
      <div class="event-row__body"><span class="row__title">${esc(taskName(t))}</span><span class="meta">${esc(meta)}</span></div>
    </div>`;
  }).join('')}</div>`;
}

function recipes(c, p) {
  const list = c.d.recipes || [];
  if (!list.length) return `<div class="empty" data-key="tab-recipes">No recipes yet.</div>`;
  return `<div class="recipe-grid" data-key="tab-recipes">${list.map(r => recipeCard(c, r, { here: p })).join('')}</div>`;
}

function settings(c, p) {
  const s = c.d.projectSettings[p.id];
  if (!s) return `<div class="empty" data-key="tab-settings">Loading settings…</div>`;
  const spent = s.month_spend_usd || 0;
  const pct = s.budget_usd > 0 ? Math.min(100, Math.round(spent / s.budget_usd * 100)) : 0;
  const notifs = [['notify_approvals', 'Approvals', 'Urgent push that breaks through Focus'], ['notify_failures', 'Failures', 'Push right away'],
    ['notify_completions', 'Completions', 'Quiet push for each one that finishes'], ['quiet_hours', 'Quiet hours 23:00 to 08:00', 'Hold everything except approvals']];
  return `<div class="settings" data-key="tab-settings">
    <div class="card card--pad"><span class="card__title">Default priority</span>
      <div class="segmented segmented--start">${[[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']].map(([v, l]) =>
        `<button class="segmented__opt${s.default_priority === v ? ' is-on' : ''}" data-action="setProjectSetting" data-k="default_priority" data-v="${v}">${l}</button>`).join('')}</div></div>
    <div class="card card--list"><div class="set-row"><div class="grow"><span class="set-row__label">Mark in progress automatically</span><span class="set-row__desc">When a new task under this project starts, set its status to In progress</span></div>${toggle(!!s.auto_in_progress, 'setProjectSetting', `data-k="auto_in_progress" data-v="${s.auto_in_progress ? 'false' : 'true'}" aria-label="Mark in progress automatically"`)}</div></div>
    <div class="card card--list">${notifs.map(([k, l, desc]) => `<div class="set-row"><div class="grow"><span class="set-row__label">${l}</span><span class="set-row__desc">${desc}</span></div>${toggle(!!s[k], 'setProjectSetting', `data-k="${k}" data-v="${s[k] ? 'false' : 'true'}" aria-label="${l}"`)}</div>`).join('')}</div>
    <div class="card card--pad"><span class="card__title">Always allowed commands</span>
      <span class="card__desc">These run without asking. Add to the list with "Always allow" on an approval.</span>
      ${s.allow_commands.length ? `<div class="allow-chips">${s.allow_commands.map((cmd, i) => `<span class="allow-chip">${esc(cmd)}<button data-action="removeAllowed" data-i="${i}" aria-label="Remove ${esc(cmd)}">✕</button></span>`).join('')}</div>` : '<span class="card__desc">Nothing yet - everything that needs permission asks first.</span>'}</div>
    <div class="card card--pad">
      <div class="budget-head"><span class="card__title">Monthly budget</span><div class="stepper"><button data-action="budgetStep" data-d="-5" aria-label="Lower budget">−</button><span>${money(s.budget_usd).replace(/\.00$/, '')}</span><button data-action="budgetStep" data-d="5" aria-label="Raise budget">+</button></div></div>
      <div class="bar6"><div style="width:${pct}%;background:${spent / (s.budget_usd || 1) > 0.8 ? 'var(--wait)' : 'var(--run)'}"></div></div>
      <span class="card__desc">${money(spent)} API spend this month. At 100% I'll pause this project and ask before continuing.</span>
    </div>
  </div>`;
}

export function project(c) {
  const p = c.project;
  if (!p) return `<div class="page page--narrow" data-key="page-project-missing"><div class="empty"><span>That project doesn't exist any more.</span><button class="btn btn--sm" data-action="go" data-view="projects">All projects</button></div></div>`;
  const { d, ui } = c;
  const tab = ui.projectTab;
  const s = d.projectSettings[p.id];
  const weekAgo = Date.now() - 7 * 86400000;
  const doneWeek = d.tasks.filter(t => t.project_id === p.id && t.status === 'done' && new Date(t.finished_at || t.created_at).getTime() > weekAgo).length;
  const inQueue = queuedTasks(d).filter(t => t.project_id === p.id).length;
  const avatar = p.is_general ? `<span class="avatar avatar--general">${octopusSVG('idle')}</span>` : `<span class="avatar" style="background:${projectColor(p)}">${esc(p.name.charAt(0).toUpperCase())}</span>`;
  const body = tab === 'notes' ? notes(c, p) : tab === 'activity' ? activity(c, p) : tab === 'recipes' ? recipes(c, p) : tab === 'settings' ? settings(c, p) : work(c, p);
  return `<div class="page page--project" data-key="page-project-${p.id}">
    <div class="proj-head">${avatar}
      <div class="proj-head__text"><span class="proj-head__name">${esc(p.name)}</span><span class="proj-head__path">${esc(p.is_general ? 'no folder · answers only' : tildify(p.path))}</span></div>
      <div class="proj-head__acts">${p.is_general ? '' : `<button class="btn btn--40" data-action="suggestImprovements"${ui.busy.suggest ? ' disabled' : ''}>${ui.busy.suggest ? 'Reviewing…' : 'Suggest'}</button>`}<button class="btn btn--40" data-action="togglePause">${p.active ? 'Pause' : 'Resume'}</button><button class="btn btn--primary btn--40" data-action="openPalette" data-pid="${p.id}">+ Task</button></div>
    </div>
    ${p.is_general ? '' : scopeSwitch(p.category, 'setCategory', [['work', 'Work'], ['personal', 'Personal']], 'proj-head__cat')}
    ${p.is_general ? '' : `<div class="chip-row" role="radiogroup" aria-label="Project status" data-noswipe>${PROJECT_STATUSES.map(([k, l, col]) =>
      `<button class="chip${p.status === k ? ' is-on' : ''}" role="radio" aria-checked="${p.status === k}" data-action="setProjectStatus" data-status="${k}" data-key="pstatus-${k}"><span class="dot" style="--c:${col};width:7px;height:7px"></span>${l}</button>`).join('')}</div>`}
    ${p.active ? '' : `<div class="paused-banner">${octo('sleep', 34)}Paused. Queued tasks for this project are skipped but kept. Resume and I'll pick them up in order.</div>`}
    <div class="stats3">
      <div class="stat"><span class="stat__v">${money(s ? s.month_spend_usd : 0)}</span><span class="stat__l">spent this month</span></div>
      <div class="stat"><span class="stat__v">${doneWeek}</span><span class="stat__l">done this week</span></div>
      <div class="stat"><span class="stat__v">${inQueue}</span><span class="stat__l">in the queue</span></div>
    </div>
    <div class="tabs" data-noswipe>${TABS.map(([k, l]) => `<button class="tabs__opt${tab === k ? ' is-on' : ''}" data-action="projectTab" data-tab="${k}">${l}</button>`).join('')}</div>
    ${body}
  </div>`;
}
