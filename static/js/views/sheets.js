// Bottom sheets (centred modals on desktop): New task, Usage, Add project, Shortcuts.

import { money, modelState, plural, taskName, PERMISSION_MODES } from '../format.js';
import { orderedProjects, projectById, projectColor, threadSummaries, queuedTasks, NOTE_KINDS } from '../data.js';
import { esc, octo, icon, spinner, toggle, clock, modelLabel, scopeSwitch } from '../ui.js';
import { recipeTarget } from './recipes.js';
import { spendBars } from './home.js';
import { sessionColor, restartText } from './shell.js';
import { FEATURES } from '../features.js';
import { imageStrip } from '../attach.js';
import { planSheet } from './plan-card.js';
import { quickSheet } from './quick.js';
import { eventSheet } from './calendar.js';

export const WHENS = [
  { k: 'now', l: 'Now' },
  { k: 'tonight', l: 'Tonight 18:00', cadence: 'once', time: '18:00' },
  { k: 'tomorrow', l: 'Tomorrow 09:00', cadence: 'once', time: '09:00', tomorrow: true },
  { k: 'daily', l: 'Every day 09:00', cadence: 'daily', time: '09:00' },
  { k: 'weekdays', l: 'Weekdays 18:00', cadence: 'weekdays', time: '18:00' },
];

function wrap(name, inner, label) {
  return `<div class="scrim" data-action="scrimClose" data-key="sheet-${name}"><div class="sheet sheet--${name}" role="dialog" aria-modal="true" aria-label="${label}">${inner}</div></div>`;
}

// ---------------------------------------------------------------- New task
const short = (s, n = 40) => (s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s);

function eta(c, pal) {
  const { w } = c;
  const p = projectById(pal.projectId);
  const pos = queuedTasks(c.d).filter(t => t.priority >= pal.priority).length;
  const pick = pal.model !== 'auto' ? c.d.models.find(m => m.id === pal.model) : null;
  if (pal.error) return [pal.error, 'var(--err-soft)'];
  if (pal.when !== 'now') return [`Runs ${WHENS.find(x => x.k === pal.when).l.toLowerCase()} in ${p ? p.name : 'this project'}`, 'var(--sleep)'];
  if (pick && pick.lane === 'api') return [`${pick.label} answers right away · no queue`, 'var(--ok-soft)'];
  if (p && !p.active) return [`${p.name} is paused. It queues but won't run until you resume.`, 'var(--wait)'];
  if (w.approval) return [`Waits behind ${short(taskName(w.approvalTask || w.approval))} (needs you)${pos ? ` + ${pos} more` : ''}`, 'var(--wait)'];
  if (w.rateLimited && w.rateLimited.resume_at) return [`Starts after the limit resets at ${clock(w.rateLimited.resume_at)}`, 'var(--sleep)'];
  if (w.agentRun) return [`Starts after ${short(taskName(w.agentRun))}${pos ? ` + ${pos} more` : ''}`, 'var(--text-meta)'];
  return [pos ? `#${pos + 1} in line` : 'Starts right away', 'var(--text-meta)'];
}

function palette(c) {
  const pal = c.ui.palette;
  const f = pal.text.trim().toLowerCase();
  const p = projectById(pal.projectId);
  const recipes = (c.d.recipes || []).filter(r => !f || r.name.toLowerCase().includes(f) || r.prompt.toLowerCase().includes(f));
  const seen = new Set();
  const recents = threadSummaries(c.d).sort((a, b) => b.id - a.id)
    .filter(t => { if (seen.has(t.prompt)) return false; seen.add(t.prompt); return !f || (taskName(t) + ' ' + t.prompt).toLowerCase().includes(f); }).slice(0, 4);
  const ms = c.d.models.filter(m => m.lane !== 'control').map(m => modelState(m, c.now));
  const [etaText, etaColor] = eta(c, pal);
  const scheduled = pal.when !== 'now';
  const mood = pal.sending ? 'dance' : c.w.approval ? 'wait' : 'idle';
  const polishBusy = c.ui.polishing === 'palette';
  const inner = `
    <div class="pal-top">${octo(mood, 34)}<textarea id="palInput" class="palette__input" rows="2" placeholder="What should Octopus do?" aria-label="What should Octopus do?" data-input="palette.text" data-enter="submitPalette">${esc(pal.text)}</textarea></div>
    ${imageStrip(pal.images, 'palette')}
    <div class="pal-chips" data-noswipe>${orderedProjects(c.d).map(x => `<button class="pal-chip${x.id === pal.projectId ? ' is-on' : ''}" style="opacity:${x.active ? 1 : 0.6}" data-action="palProject" data-id="${x.id}"><span class="swatch-sq" style="background:${projectColor(x)}"></span>${esc(x.name)}</button>`).join('')}</div>
    <div class="pal-chips" data-noswipe>${[{ id: 'auto', label: 'Auto', dot: 'var(--text)', ok: true }, ...ms.map(m => ({ id: m.id, label: m.label, dot: m.dot, ok: m.available || m.rate }))].map(m =>
      `<button class="pal-model${pal.model === m.id ? ' is-on' : ''}" style="opacity:${m.ok ? 1 : 0.5}" data-action="palModel" data-id="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>
    <div class="pal-segs">
      <div class="segmented">${[[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']].map(([v, l]) => `<button class="segmented__opt${pal.priority === v ? (v === 1 ? ' is-urgent' : ' is-on') : ''}" data-action="palPriority" data-v="${v}">${l}</button>`).join('')}</div>
      <div class="segmented segmented--scroll" data-noswipe>${WHENS.map(x => `<button class="segmented__opt${pal.when === x.k ? (x.k === 'now' ? ' is-on' : ' is-scheduled') : ''}" data-action="palWhen" data-k="${x.k}">${x.l}</button>`).join('')}</div>
    </div>
    <div class="pal-lists">
      ${recipes.length ? `<span class="pal-label">RECIPES · TAP TO ${scheduled ? 'USE' : 'QUEUE'}</span>${recipes.map(r => `<button class="pal-item" data-action="palRecipe" data-id="${r.id}"><span class="pal-item__text"><span class="pal-item__name">${esc(r.name)}</span><span class="pal-item__sub truncate">${esc(r.prompt)}</span></span><span class="meta">${esc(r.target === 'all' ? 'all projects' : r.target === 'any' ? (p ? p.name : '') : recipeTarget(r))}</span></button>`).join('')}` : ''}
      ${recents.length ? `<span class="pal-label">RECENT PROMPTS</span>${recents.map(t => { const tp = projectById(t.project_id); return `<button class="pal-item pal-item--row" data-action="palRecent" data-id="${t.id}"><span class="pal-item__again">↻</span><span class="grow truncate">${esc(taskName(t))}</span><span class="meta">${esc(tp ? tp.name : '')}</span></button>`; }).join('')}` : ''}
    </div>
    <div class="palette__foot">
      ${FEATURES.images ? `<label class="tool-btn tool-btn--boxed" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="palette">${icon('image', 20)}</label>` : ''}
      <button class="tool-btn tool-btn--boxed" data-action="polish" data-target="palette" title="Polish text" aria-label="Polish text"${!pal.text.trim() || polishBusy ? ' disabled' : ''}>${polishBusy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 19, { strokeWidth: 1.6 })}</button>
      <span class="palette__eta" style="--c:${etaColor}">${esc(etaText)}</span>
      <button class="pal-cta" data-action="submitPalette"${!pal.text.trim() || pal.sending ? ' disabled' : ''}>${pal.sending ? spinner() + (scheduled ? 'Scheduling…' : 'Routing…') : `${scheduled ? 'Schedule' : 'Queue'}<span>↵</span>`}</button>
    </div>`;
  return wrap('palette', inner, 'New task');
}

// ---------------------------------------------------------------- Usage
function usage(c) {
  const { w, d } = c;
  const s = d.spend;
  const stat = (label, u, color, reset) => `<div class="stat-card"><span class="stat-card__l">${label}</span>
      <span class="stat-card__v" style="color:${color}">${u ? u.pct + '%' : '—'}</span>
      <div class="bar5"><div style="width:${u ? u.pct : 0}%;background:${color}"></div></div>
      <span class="meta">${u ? reset : 'after the next Claude Code run'}</span></div>`;
  const max = Math.max(0.0001, ...((s && s.by_project) || []).map(x => x.cost_usd));
  const inner = `
    <div class="sheet__head"><span class="sheet__title">Usage</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <div class="sheet__body">
      <div class="stat-cards">
        ${stat('Claude session · 5h', w.session, w.session ? sessionColor(w.session.pct) : 'var(--text-meta)', w.session && w.session.resets ? 'resets ' + clock(w.session.resets.toISOString()) : 'window reset')}
        ${stat('Weekly limit', w.week, 'var(--text)', w.week && w.week.resets ? 'resets ' + w.week.resets.toLocaleDateString([], { weekday: 'short' }) + ' ' + clock(w.week.resets.toISOString()) : 'window reset')}
      </div>
      <div class="card card--pad"><div class="usage-row__head"><span class="card__title">API spend</span><span class="meta">${money(s ? s.month_usd : 0)} / ${money(s ? s.monthly_budget_usd : 100)} this month</span></div>
        <div class="bigbars">${spendBars(s, true)}</div></div>
      <button class="btn btn--ghost" data-action="go" data-view="usage">See token usage in detail →</button>
      <div class="card card--list"><span class="card__title card__title--list">By project · this month</span>
        ${s && s.by_project.length ? s.by_project.map(x => { const p = projectById(x.project_id); return `<div class="byproj"><span class="truncate">${esc(x.name)}</span><div class="bar6 bar6--soft"><div style="width:${Math.round(x.cost_usd / max * 100)}%;background:${projectColor(p)}"></div></div><span class="mono">${money(x.cost_usd)}</span></div>`; }).join('') : '<span class="card__desc byproj-empty">No API spend yet this month. Claude Code runs on your subscription and isn\'t counted here.</span>'}
      </div>
      <div class="set-row set-row--card"><div class="grow"><span class="set-row__label">Save the session for urgent work</span><span class="set-row__desc">Hold Low-priority tasks when the session is over 85%.</span></div>${toggle(!!(d.settings && d.settings.hold_low_when_busy), 'toggleHoldLow', 'aria-label="Save the session for urgent work"')}</div>
      <div class="set-row set-row--card"><div class="grow"><span class="set-row__label">Warn me about the weekly limit</span><span class="set-row__desc">Phone and dashboard alert when you're on pace to run out before the week resets.</span></div>${toggle(!(d.settings && d.settings.notify_weekly_pace === false), 'toggleWeeklyPace', 'aria-label="Warn me about the weekly limit"')}</div>
      <div class="set-row set-row--card"><div class="grow"><span class="set-row__label">Restart Octopus</span><span class="set-row__desc">${esc(restartText(c.ui.restart))}</span></div>
        <button class="btn btn--ghost btn--38" data-action="restartOctopus"${c.ui.restart ? ' disabled' : ''}>${c.ui.restart ? spinner() + 'Restarting' : 'Restart'}</button></div>
    </div>`;
  return wrap('usage', inner, 'Usage');
}

// ---------------------------------------------------------------- Add project
function addProject(c) {
  const ob = c.ui.onboard, busy = c.ui.busy.addProject;
  const inner = `<form class="sheet__body sheet__body--form" data-submit="addProject">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">Add a project</span><button class="icon-btn icon-btn--44" type="button" data-action="closeSheet" aria-label="Close">✕</button></div>
    <span class="card__desc card__desc--lg">Point Octopus at a folder on your Mac. Its tasks run one at a time from the shared queue.</span>
    <input id="addName" class="input" name="name" placeholder="Name, e.g. api-gateway" value="${esc(ob.name)}" data-input="onboard.name" autocomplete="off" aria-label="Name">
    <input class="input input--mono" name="path" placeholder="~/code/folder" value="${esc(ob.path)}" data-input="onboard.path" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Path">
    ${scopeSwitch(ob.category || 'work', 'setNewCategory', [['work', 'Work'], ['personal', 'Personal']])}
    ${ob.error ? `<span class="form-err">${esc(ob.error)}</span>` : ''}
    <button class="btn btn--primary btn--50" type="submit"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Adding…' : 'Add project'}</button>
  </form>`;
  return wrap('addProject', inner, 'Add a project');
}

// ---------------------------------------------------------------- Shortcuts
const KEYS = [['⌘K / N', 'New task'], ['/', 'Search everything'],['A', 'Approve the waiting request'], ['D', 'Deny it'], ['1 – 4', 'Home, Projects, Recipes, Activity'], ['6', 'Messages'], ['7', 'Calendar'], ['G', 'Good morning briefing'], ['[', 'Collapse or expand the sidebar'], ['Esc', 'Close the panel'], ['?', 'This list']];
function keys() {
  return wrap('keys', `<div class="sheet__body"><div class="sheet__head sheet__head--flush"><span class="sheet__title">Shortcuts</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    ${KEYS.map(([k, d]) => `<div class="key-row"><span>${d}</span><span class="kbd">${k}</span></div>`).join('')}</div>`, 'Shortcuts');
}

// ---------------------------------------------------------------- Summary
// c.ui.summary: { taskId, title, text, loading, error, cached, modelId }
function summary(c) {
  const s = c.ui.summary || {};
  let body;
  if (s.loading) body = `<div class="sum-wait">${spinner()}<span>Reading the whole chat…</span></div>`;
  else if (s.error) body = `<span class="form-err">${esc(s.error)}</span>`;
  else body = `<div class="sum-text">${esc(s.text || '')}</div>
    <span class="meta">${s.cached ? 'Saved summary · nothing new since' : 'Fresh summary' + (s.modelId ? ' · ' + esc(modelLabel(s.modelId)) : '')}</span>`;
  const inner = `<div class="sheet__body sheet__body--form">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">Summary</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <span class="sum-name">${esc(s.title || '')}</span>
    ${body}
    ${s.loading ? '' : `<div class="need__row"><button class="btn btn--ghost btn--38" data-action="summarise" data-id="${s.taskId}" data-refresh="1">${s.error ? 'Try again' : 'Refresh'}</button></div>`}
  </div>`;
  return wrap('summary', inner, 'Summary');
}

// ---------------------------------------------------------------- Edit a queued task
// c.ui.edit: { id, isReply, canMoveProject, title, prompt, priority, projectId, model, after, origAfter, saving, error }
// after / origAfter: 'first' or the id (as a string) of the queued item it sits behind.
function editTask(c) {
  const e = c.ui.edit || {};
  const t = c.d.tasks.find(x => x.id === e.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">${e.isReply ? 'Edit reply' : 'Edit task'}</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (!t || t.status !== 'queued') {
    return wrap('editTask', `<div class="sheet__body sheet__body--form">${head}
      <span class="card__desc card__desc--lg">${t ? 'It has already started, so it can\'t be changed any more.' : 'This task is gone.'}</span></div>`, 'Edit task');
  }
  const others = queuedTasks(c.d).filter(x => x.id !== e.id);
  const ms = c.d.models.filter(m => m.lane !== 'control').map(m => modelState(m, c.now));
  const label = s => `<span class="pal-label">${s}</span>`;
  const inner = `<div class="sheet__body sheet__body--form">
    ${head}
    ${e.isReply ? `<span class="card__desc">In <b>${esc(taskName(t))}</b> · a reply stays in its chat's project and model.</span>`
      : `${label('NAME')}<input class="input" value="${esc(e.title)}" data-input="edit.title" placeholder="Named automatically" aria-label="Name" autocomplete="off">`}
    ${label('PROMPT')}<textarea class="input edit-prompt" rows="5" data-input="edit.prompt" data-autogrow="260" aria-label="Prompt">${esc(e.prompt)}</textarea>
    ${e.canMoveProject ? `${label('PROJECT')}<div class="pal-chips pal-chips--flush" data-noswipe>${orderedProjects(c.d).map(x => `<button class="pal-chip${x.id === e.projectId ? ' is-on' : ''}" style="opacity:${x.active ? 1 : 0.6}" data-action="editProject" data-id="${x.id}"><span class="swatch-sq" style="background:${projectColor(x)}"></span>${esc(x.name)}</button>`).join('')}</div>` : ''}
    ${e.isReply ? '' : `${label('MODEL')}<div class="pal-chips pal-chips--flush" data-noswipe>${[{ id: 'auto', label: 'Auto', dot: 'var(--text)', ok: true }, ...ms.map(m => ({ id: m.id, label: m.label, dot: m.dot, ok: m.available || m.rate }))].map(m =>
      `<button class="pal-model${e.model === m.id ? ' is-on' : ''}" style="opacity:${m.ok ? 1 : 0.5}" data-action="editModel" data-id="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>`}
    ${t.lane === 'api' || t.lane === 'assistant' ? '' : `${label('MODE')}<div class="pal-chips pal-chips--flush" data-noswipe>${PERMISSION_MODES.map(m =>
      `<button class="pal-model${e.mode === m.id ? ' is-on' : ''}" title="${esc(m.sub)}" data-action="editMode" data-id="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>`}
    ${label('PRIORITY')}<div class="segmented">${[[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']].map(([v, l]) => `<button class="segmented__opt${e.priority === v ? (v === 1 ? ' is-urgent' : ' is-on') : ''}" data-action="editPriority" data-v="${v}">${l}</button>`).join('')}</div>
    ${label('WHEN IT RUNS')}<select class="input" data-input="edit.after" aria-label="Position in the queue">
      <option value="first"${e.after === 'first' ? ' selected' : ''}>First in line</option>
      ${others.map((o, i) => `<option value="${o.id}"${e.after === String(o.id) ? ' selected' : ''}>After #${i + 1} · ${esc(taskName(o).slice(0, 60))}</option>`).join('')}
    </select>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveEdit"${e.saving ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : 'Save'}</button>
      <button class="btn btn--ghost btn--44" data-action="runNow" data-id="${e.id}">▶ Proceed now</button>
      <button class="btn-dismiss" data-action="cancelQueued" data-id="${e.id}">Cancel task</button>
    </div>
  </div>`;
  return wrap('editTask', inner, e.isReply ? 'Edit reply' : 'Edit task');
}

// Edit a backlog item: the same text the item will run with, in a box big enough for real detail.
function editTodo(c) {
  const e = c.ui.todoEdit || {};
  const gone = !(c.d.todos[e.pid] || []).some(x => x.id === e.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Edit backlog item</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (gone) return wrap('editTodo', `<div class="sheet__body sheet__body--form">${head}<span class="card__desc card__desc--lg">This item is gone.</span></div>`, 'Edit backlog item');
  return wrap('editTodo', `<div class="sheet__body sheet__body--form">
    ${head}
    <span class="pal-label">DETAILS</span><textarea id="todoEditInput" class="input edit-prompt" rows="8" data-input="todoEdit.text" data-autogrow="360" aria-label="Backlog item">${esc(e.text)}</textarea>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveTodoEdit"${e.saving || !e.text.trim() ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : 'Save'}</button>
      <button class="btn-dismiss" data-action="deleteTodoEdit">Delete</button>
    </div>
  </div>`, 'Edit backlog item');
}

// Edit a project note: change what it says, what kind it is, or delete it.
function editNote(c) {
  const e = c.ui.noteEdit || {};
  const gone = !(c.d.notes[e.pid] || []).some(x => x.id === e.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Edit note</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (gone) return wrap('editNote', `<div class="sheet__body sheet__body--form">${head}<span class="card__desc card__desc--lg">This note is gone.</span></div>`, 'Edit note');
  return wrap('editNote', `<div class="sheet__body sheet__body--form">
    ${head}
    <div class="chip-row" role="radiogroup" aria-label="Kind of note">${NOTE_KINDS.map(([k, l]) =>
      `<button class="chip${e.kind === k ? ' is-on' : ''}" role="radio" aria-checked="${e.kind === k}" data-action="setNoteEditKind" data-kind="${k}">${l}</button>`).join('')}</div>
    <span class="pal-label">DETAILS</span><textarea id="noteEditInput" class="input edit-prompt" rows="8" data-input="noteEdit.text" data-autogrow="360" aria-label="Note">${esc(e.text)}</textarea>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveNoteEdit"${e.saving || !e.text.trim() ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : 'Save'}</button>
      <button class="btn-dismiss" data-action="deleteNoteEdit">Delete</button>
    </div>
  </div>`, 'Edit note');
}

export function sheet(c) {
  switch (c.ui.sheet) {
    case 'palette': return palette(c);
    case 'plan': return planSheet(c);
    case 'usage': return usage(c);
    case 'addProject': return addProject(c);
    case 'keys': return keys();
    case 'summary': return summary(c);
    case 'editTask': return editTask(c);
    case 'editTodo': return editTodo(c);
    case 'editNote': return editNote(c);
    case 'quick': return quickSheet(c, wrap);
    case 'event': return eventSheet(c, wrap);
    default: return '';
  }
}

export { plural, modelLabel };
