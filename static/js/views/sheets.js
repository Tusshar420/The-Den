// Bottom sheets (centred modals on desktop): New task, Usage, Add project, Shortcuts.

import { money, modelState, plural, taskName, PERMISSION_MODES } from '../format.js';
import { orderedProjects, projectById, projectColor, threadSummaries, queuedTasks, NOTE_KINDS, bulkPlan, crmOnly } from '../data.js';
import { esc, octo, icon, spinner, toggle, clock, modelLabel, scopeSwitch, polishLabel } from '../ui.js';
import { recipeTarget } from './recipes.js';
import { spendBars } from './home.js';
import { sessionColor, restartText } from './shell.js';
import { micAvailable } from '../voice.js';
import { FEATURES } from '../features.js';
import { imageStrip } from '../attach.js';
import { accountById, shortName, restingUntil, accountField, accountSheet, accountEditSheet, accountSwitcher } from './accounts.js';
import { planSheet } from './plan-card.js';
import { quickSheet } from './quick.js';
import { taskDetail } from './task-detail.js';
import { latestInChat } from '../octopus-data.js';
import { runReviewSheet } from './run-review.js';
import { eventSheet } from './calendar.js';
import { phaseSheet, reportSheet, generateSheet } from './phases.js';
import { tourSheet, helpSheet } from './tour.js';
import { SHORTCUTS, bindingOf, label } from '../shortcuts.js';
import { previewSheet } from '../preview.js';
import { newProjectSheet } from './new-project.js';

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
  // Claude Code work runs one task at a time per Claude account: only tasks on this project's account are ahead of it.
  const sameAccount = t => { if (pal.account) return t.pinned_account === pal.account || (!t.pinned_account && (projectById(t.project_id) || {}).claude_account === pal.account); const tp = projectById(t.project_id); return !p || !p.claude_account || !tp || tp.claude_account === p.claude_account; };
  const pos = queuedTasks(c.d).filter(t => t.priority >= pal.priority && sameAccount(t)).length;
  const acct = pal.account ? accountById(c, pal.account) : p && accountById(c, p.claude_account);
  const busyOn = c.d.tasks.find(t => t.status === 'running' && t.lane === 'agent' && sameAccount(t));
  const pick = pal.model !== 'auto' ? c.d.models.find(m => m.id === pal.model) : null;
  if (pal.error) return [pal.error, 'var(--err-soft)'];
  if (pal.when !== 'now') return [`Runs ${WHENS.find(x => x.k === pal.when).l.toLowerCase()} in ${p ? p.name : 'this project'}`, 'var(--sleep)'];
  if (pick && pick.lane === 'api') return [`${pick.label} answers right away · no queue`, 'var(--ok-soft)'];
  if (p && !p.active) return [`${p.name} is paused. It queues but won't run until you resume.`, 'var(--wait)'];
  if (w.approval) return [`Waits behind ${short(taskName(w.approvalTask || w.approval))} (needs you)${pos ? ` + ${pos} more` : ''}`, 'var(--wait)'];
  if (acct && restingUntil(acct)) return [`Starts after ${shortName(acct)}'s limit resets at ${clock(acct.limited_until)}`, 'var(--sleep)'];
  if (!acct && w.rateLimited && w.rateLimited.resume_at) return [`Starts after the limit resets at ${clock(w.rateLimited.resume_at)}`, 'var(--sleep)'];
  if (busyOn) return [`Starts after ${short(taskName(busyOn))}${pos ? ` + ${pos} more` : ''}`, 'var(--text-meta)'];
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
    <div class="pal-top">${octo(mood, 34)}<textarea id="palInput" class="palette__input" rows="2" data-autogrow="520" data-fit placeholder="What should Octopus do?" aria-label="What should Octopus do?" data-input="palette.text" data-enter="submitPalette">${esc(pal.text)}</textarea></div>
    ${imageStrip(pal.images, 'palette')}
    <div class="pal-chips" data-noswipe>${orderedProjects(c.d).filter(x => !crmOnly(x) || x.id === pal.projectId).map(x => `<button class="pal-chip${x.id === pal.projectId ? ' is-on' : ''}" style="opacity:${x.active ? 1 : 0.6}" data-action="palProject" data-id="${x.id}"><span class="swatch-sq" style="background:${projectColor(x)}"></span>${esc(x.name)}</button>`).join('')}</div>
    <div class="pal-chips" data-noswipe>${[{ id: 'auto', label: 'Auto', dot: 'var(--text)', ok: true }, ...ms.map(m => ({ id: m.id, label: m.label, dot: m.dot, ok: m.available || m.rate }))].map(m =>
      `<button class="pal-model${pal.model === m.id ? ' is-on' : ''}" style="opacity:${m.ok ? 1 : 0.5}" data-action="palModel" data-id="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>
    ${scheduled ? '' : accountField(c, { model: pal.model, value: pal.account, action: 'palAccount', projectId: pal.projectId, inline: true })}
    ${scheduled || (pal.model !== 'auto' && (ms.find(m => m.id === pal.model) || {}).lane === 'api') ? '' : `<div class="pal-chips" data-noswipe><span class="pal-label">APPROVAL</span>${PERMISSION_MODES.map(m => `<button class="pal-model${(pal.mode || 'acceptEdits') === m.id ? ' is-on' : ''}" title="${esc(m.sub)}" data-action="palMode" data-id="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>`}
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
      <button class="tool-btn tool-btn--boxed pal-mic${c.ui.palListening ? ' is-on' : ''}${micAvailable() ? '' : ' is-unsupported'}" data-action="palDictate" title="Dictate" aria-label="${c.ui.palListening ? 'Stop dictating' : 'Dictate'}" aria-pressed="${!!c.ui.palListening}"><svg width="19" height="19" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7.2" y="2.5" width="5.6" height="10" rx="2.8"/><path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5"/></svg></button>
      <button class="tool-btn tool-btn--boxed" data-action="polish" data-target="palette" title="Polish text" aria-label="Polish text"${!pal.text.trim() || polishBusy ? ' disabled' : ''}>${polishBusy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 19, { strokeWidth: 1.6 })}</button>
      <span class="palette__eta" style="--c:${etaColor}">${esc(etaText)}</span>
      <button class="pal-keep" role="checkbox" aria-checked="${!!pal.keep}" data-action="palKeep" title="Stay here after queueing, so you can add another task"><span class="check check--sm" aria-hidden="true">${pal.keep ? '✓' : ''}</span>Keep</button>
      <button class="pal-cta" data-action="submitPalette"${!pal.text.trim() || pal.sending ? ' disabled' : ''}>${pal.sending ? spinner() + (scheduled ? 'Scheduling…' : 'Routing…') : `${scheduled ? 'Schedule' : 'Queue'}<span>↵</span>`}</button>
    </div>`;
  return wrap('palette', inner, 'New task');
}

// ---------------------------------------------------------------- Usage
// Everything past the 5h / weekly limits: API spend, spend by project and the usage switches.
// Shown in the Usage sheet and right inside a Claude account's sheet (no extra click to get to it).
function overallUsage(c) {
  const d = c.d, s = d.spend;
  const max = Math.max(0.0001, ...((s && s.by_project) || []).map(x => x.cost_usd));
  return `<div class="card card--pad"><div class="usage-row__head"><span class="card__title">API spend</span><span class="meta">${money(s ? s.month_usd : 0)} / ${money(s ? s.monthly_budget_usd : 100)} this month</span></div>
        <div class="bigbars">${spendBars(s, true)}</div></div>
      <button class="btn btn--ghost" data-action="go" data-view="usage">See token usage in detail →</button>
      <div class="card card--list"><span class="card__title card__title--list">By project · this month</span>
        ${s && s.by_project.length ? s.by_project.map(x => { const p = projectById(x.project_id); return `<div class="byproj"><span class="truncate">${esc(x.name)}</span><div class="bar6 bar6--soft"><div style="width:${Math.round(x.cost_usd / max * 100)}%;background:${projectColor(p)}"></div></div><span class="mono">${money(x.cost_usd)}</span></div>`; }).join('') : '<span class="card__desc byproj-empty">No API spend yet this month. Claude Code runs on your subscription and isn\'t counted here.</span>'}
      </div>
      <div class="set-row set-row--card"><div class="grow"><span class="set-row__label">Save the session for urgent work</span><span class="set-row__desc">Hold Low-priority tasks when the session is over 85%.</span></div>${toggle(!!(d.settings && d.settings.hold_low_when_busy), 'toggleHoldLow', 'aria-label="Save the session for urgent work"')}</div>
      <div class="set-row set-row--card"><div class="grow"><span class="set-row__label">Warn me about the weekly limit</span><span class="set-row__desc">Phone and dashboard alert when you're on pace to run out before the week resets.</span></div>${toggle(!(d.settings && d.settings.notify_weekly_pace === false), 'toggleWeeklyPace', 'aria-label="Warn me about the weekly limit"')}</div>
      <div class="set-row set-row--card"><div class="grow"><span class="set-row__label">Restart Octopus</span><span class="set-row__desc">${esc(restartText(c.ui.restart))}</span></div>
        <button class="btn btn--ghost btn--38" data-action="restartOctopus"${c.ui.restart ? ' disabled' : ''}>${c.ui.restart && !c.ui.restart.hard ? spinner() + 'Restarting' : 'Restart'}</button>
        <button class="btn btn--danger btn--38" data-action="hardRestartOctopus" title="Stop what's running and restart now"${c.ui.restart && c.ui.restart.hard ? ' disabled' : ''}>${c.ui.restart && c.ui.restart.hard ? spinner() + 'Restarting' : 'Hard restart'}</button></div>`;
}

function usage(c) {
  const { w } = c;
  const stat = (label, u, color, reset) => `<div class="stat-card"><span class="stat-card__l">${label}</span>
      <span class="stat-card__v" style="color:${color}">${u ? u.pct + '%' : '—'}</span>
      <div class="bar5"><div style="width:${u ? u.pct : 0}%;background:${color}"></div></div>
      <span class="meta">${u ? reset : 'after the next Claude Code run'}</span></div>`;
  const inner = `
    <div class="sheet__head"><span class="sheet__title">Usage</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <div class="sheet__body">
      ${c.desk ? '' : accountSwitcher(c).replace('class="acct-sw"', 'class="acct-sw acct-sw--sheet"')}
      <div class="stat-cards">
        ${stat('Claude session · 5h', w.session, w.session ? sessionColor(w.session.pct) : 'var(--text-meta)', w.session && w.session.resets ? 'resets ' + clock(w.session.resets.toISOString()) : 'window reset')}
        ${stat('Weekly limit', w.week, 'var(--text)', w.week && w.week.resets ? 'resets ' + w.week.resets.toLocaleDateString([], { weekday: 'short' }) + ' ' + clock(w.week.resets.toISOString()) : 'window reset')}
      </div>
      ${overallUsage(c)}
    </div>`;
  return wrap('usage', inner, 'Usage');
}

// ---------------------------------------------------------------- New project (views/new-project.js)
const addProject = c => newProjectSheet(c, wrap);

// ---------------------------------------------------------------- Shortcuts
// The changeable ones (Settings > Shortcuts) as currently set, then the built-in ones.
function keys() {
  const rows = [...SHORTCUTS.filter(([id]) => bindingOf(id)).map(([id, d]) => [label(bindingOf(id)), d]),
    [`${label('Mod+K')} / N`, 'New task'], ['[', 'Collapse or expand the sidebar'], ['1 – 4', 'Home, Projects, Recipes, Activity'], ['6', 'Messages'], ['7', 'Calendar'], ['Esc', 'Close the panel']];
  return wrap('keys', `<div class="sheet__body"><div class="sheet__head sheet__head--flush"><span class="sheet__title">Shortcuts</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    ${rows.map(([k, d]) => `<div class="key-row"><span>${esc(d)}</span><span class="kbd">${esc(k)}</span></div>`).join('')}
    <button class="btn btn--sm" data-action="go" data-view="settings">Change shortcuts</button></div>`, 'Shortcuts');
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
// c.ui.edit: { id, isReply, canMoveProject, title, prompt, priority, projectId, model, account, after, origAfter, saving, error }
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
  const inner = `${head.replace(' sheet__head--flush', '')}
  <div class="sheet__body sheet__body--form">
    ${e.isReply ? `<span class="card__desc">In <b>${esc(taskName(t))}</b> · a reply stays in its chat's project and model.</span>`
      : `${label('NAME')}<input class="input" value="${esc(e.title)}" data-input="edit.title" placeholder="Named automatically" aria-label="Name" autocomplete="off">`}
    ${polishLabel('PROMPT', 'editPromptInput', e.prompt)}<textarea id="editPromptInput" class="input edit-prompt" rows="5" data-input="edit.prompt" data-autogrow="260" aria-label="Prompt">${esc(e.prompt)}</textarea>
    ${e.canMoveProject ? `${label('PROJECT')}<div class="pal-chips pal-chips--flush" data-noswipe>${orderedProjects(c.d).map(x => `<button class="pal-chip${x.id === e.projectId ? ' is-on' : ''}" style="opacity:${x.active ? 1 : 0.6}" data-action="editProject" data-id="${x.id}"><span class="swatch-sq" style="background:${projectColor(x)}"></span>${esc(x.name)}</button>`).join('')}</div>` : ''}
    ${e.isReply ? '' : `${label('MODEL')}<div class="pal-chips pal-chips--flush" data-noswipe>${[{ id: 'auto', label: 'Auto', dot: 'var(--text)', ok: true }, ...ms.map(m => ({ id: m.id, label: m.label, dot: m.dot, ok: m.available || m.rate }))].map(m =>
      `<button class="pal-model${e.model === m.id ? ' is-on' : ''}" style="opacity:${m.ok ? 1 : 0.5}" data-action="editModel" data-id="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>`}
    ${accountField(c, { model: e.isReply ? t.model_id : e.model, value: e.account, action: 'editAccount', projectId: e.projectId })}
    ${t.lane === 'api' || t.lane === 'assistant' ? '' : `${label('MODE')}<div class="pal-chips pal-chips--flush" data-noswipe>${PERMISSION_MODES.map(m =>
      `<button class="pal-model${e.mode === m.id ? ' is-on' : ''}" title="${esc(m.sub)}" data-action="editMode" data-id="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>`}
    ${label('PRIORITY')}<div class="segmented">${[[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']].map(([v, l]) => `<button class="segmented__opt${e.priority === v ? (v === 1 ? ' is-urgent' : ' is-on') : ''}" data-action="editPriority" data-v="${v}">${l}</button>`).join('')}</div>
    ${label('WHEN IT RUNS')}<select class="input" data-input="edit.after" aria-label="Position in the queue">
      <option value="first"${e.after === 'first' ? ' selected' : ''}>First in line</option>
      ${others.map((o, i) => `<option value="${o.id}"${e.after === String(o.id) ? ' selected' : ''}>After #${i + 1} · ${esc(taskName(o).slice(0, 60))}</option>`).join('')}
    </select>
  </div>
  <div class="sheet__foot">
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveEdit"${e.saving ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : 'Save'}</button>
      <button class="btn btn--ghost btn--44" data-action="runNow" data-id="${e.id}">▶ Proceed now</button>
      <button class="btn-dismiss" data-action="cancelQueued" data-id="${e.id}">Cancel task</button>
      <button class="btn-dismiss btn-dismiss--del" data-action="deleteQueued" data-id="${e.id}">Delete</button>
    </div>
  </div>`;
  return wrap('editTask', inner, e.isReply ? 'Edit reply' : 'Edit task');
}

// ---------------------------------------------------------------- Edit and retry a failed / stopped task
// c.ui.retryEdit: { id, prompt, saving, error } - Retry saves the new prompt and queues it at the front in one step.
function retryEdit(c) {
  const e = c.ui.retryEdit || {};
  const t = c.d.tasks.find(x => x.id === e.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Edit and retry</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (!t || !['error', 'cancelled', 'needs_review', 'rate_limited'].includes(t.status)) {
    return wrap('retryEdit', `<div class="sheet__body sheet__body--form">${head}
      <span class="card__desc card__desc--lg">${t ? "This task isn't failed or stopped any more, so it can't be retried from here." : 'This task is gone.'}</span></div>`, 'Edit and retry');
  }
  const why = t.error_message ? `<span class="need__err">${esc(t.error_message.slice(0, 300))}</span>` : '';
  return wrap('retryEdit', `<div class="sheet__body sheet__body--form">
    ${head}
    ${why}
    ${polishLabel('PROMPT', 'retryPromptInput', e.prompt)}<textarea id="retryPromptInput" class="input edit-prompt" rows="6" data-input="retryEdit.prompt" data-autogrow="260" aria-label="Prompt">${esc(e.prompt)}</textarea>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="submitRetryEdit"${e.saving ? ' disabled' : ''}>${e.saving ? spinner() + 'Retrying…' : 'Retry with changes'}</button>
      <button class="btn btn--ghost btn--44" data-action="closeSheet">Cancel</button>
    </div>
  </div>`, 'Edit and retry');
}

// ---------------------------------------------------------------- Model / mode of a queued task
// c.ui.runOpts: { id, isReply, model, account, mode, saving, error } - every tap saves straight away.
function runOpts(c) {
  const o = c.ui.runOpts || {};
  const t = c.d.tasks.find(x => x.id === o.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Model / Account / Mode</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (!t || t.status !== 'queued') {
    return wrap('runOpts', `<div class="sheet__body sheet__body--form">${head}
      <span class="card__desc card__desc--lg">${t ? 'It has already started, so it can\'t be changed any more.' : 'This task is gone.'}</span></div>`, 'Model / Mode');
  }
  const ms = c.d.models.filter(m => m.lane !== 'control').map(m => modelState(m, c.now));
  const label = s => `<span class="pal-label">${s}</span>`;
  const dis = o.saving ? ' disabled' : '';
  const inner = `<div class="sheet__body sheet__body--form">
    ${head}
    ${o.isReply ? `<span class="card__desc">A reply stays on its chat's model.</span>`
      : `${label('MODEL')}<div class="pal-chips pal-chips--flush" data-noswipe>${[{ id: 'auto', label: 'Auto', dot: 'var(--text)', ok: true }, ...ms.map(m => ({ id: m.id, label: m.label, dot: m.dot, ok: m.available || m.rate }))].map(m =>
        `<button class="pal-model${o.model === m.id ? ' is-on' : ''}" style="opacity:${m.ok ? 1 : 0.5}" data-action="runOptsSet" data-field="model" data-id="${m.id}"${dis}><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>`}
    ${accountField(c, { model: o.isReply ? t.model_id : o.model, value: o.account, action: 'runOptsSet', projectId: t.project_id, disabled: o.saving, attrs: 'data-field="account"' })}
    ${t.lane === 'api' || t.lane === 'assistant' ? '' : `${label('MODE')}<div class="pal-chips pal-chips--flush" data-noswipe>${PERMISSION_MODES.map(m =>
      `<button class="pal-model${o.mode === m.id ? ' is-on' : ''}" title="${esc(m.sub)}" data-action="runOptsSet" data-field="mode" data-id="${m.id}"${dis}><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>`}
    ${o.error ? `<span class="form-err">${esc(o.error)}</span>` : ''}
  </div>`;
  return wrap('runOpts', inner, 'Model / Mode');
}

// ---------------------------------------------------------------- Move or delete a chat
// c.ui.chatAct: { id (the chat's first message), saving (project id being moved to), error }
// Anything not under way: finished one way or another, or still waiting in line.
const SETTLED = ['queued', 'cancelled', 'done', 'error', 'needs_review'];
function detailSheet(c) {
  const id = c.ui.detailTask;
  const root = c.d.tasks.find(t => t.id === id);
  return wrap('taskDetail', `<div class="sheet__body">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title truncate">${esc(root ? taskName(root) : 'Details')}</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <div class="td-modal">${taskDetail(c, latestInChat(id))}</div></div>`, 'Task details');
}

function chatActions(c) {
  const a = c.ui.chatAct || {};
  if (a.ids) {
    const label = `<span class="pal-label">MOVE ${a.ids.length} CHAT${a.ids.length === 1 ? '' : 'S'} TO PROJECT</span>`;
    return wrap('chatActions', `<div class="sheet__body sheet__body--form">
      <div class="sheet__head sheet__head--flush"><span class="sheet__title">Move chats</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
      ${label}<div class="pal-chips pal-chips--flush" data-noswipe>${orderedProjects(c.d).map(x =>
        `<button class="pal-chip" style="opacity:${x.active ? 1 : 0.6}" data-action="transferChat" data-id="${x.id}"${a.saving ? ' disabled' : ''}>${a.saving === x.id ? spinner() : `<span class="swatch-sq" style="background:${projectColor(x)}"></span>`}${esc(x.name)}</button>`).join('')}</div>
      <span class="card__desc">Each chat's messages go with it. Chats already in the chosen project stay put.</span>
      ${a.error ? `<span class="form-err">${esc(a.error)}</span>` : ''}
    </div>`, 'Move chats');
  }
  const fresh = m => ({ ...m, ...(c.d.tasks.find(x => x.id === m.id) || {}) });
  const th = c.d.threads[a.id];
  const msgs = (th ? th.tasks : c.d.tasks.filter(t => (t.thread_id || t.id) === a.id)).map(fresh);
  const root = msgs.find(m => m.id === a.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Move or delete</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (!root) return wrap('chatActions', `<div class="sheet__body sheet__body--form">${head}<span class="card__desc card__desc--lg">This chat is gone.</span></div>`, 'Move or delete');
  const here = projectById(root.project_id);
  const label = s => `<span class="pal-label">${s}</span>`;
  const busy = msgs.some(m => !SETTLED.includes(m.status));
  const n = msgs.length;
  const inner = `<div class="sheet__body sheet__body--form">
    ${head}
    <span class="card__desc"><b>${esc(taskName(root))}</b> · in ${esc(here ? here.name : '?')}${n > 1 ? ` · ${plural(n, 'message')}` : ''}</span>
    ${busy ? `<button class="btn btn--ghost btn--44" data-action="summarise" data-id="${root.id}">${icon('summary', 16)} Summarise this chat</button>
    <span class="card__desc card__desc--lg">It's still under way - stop it or wait for it to finish, then it can be moved or deleted.</span>` : `
    <button class="btn btn--ghost btn--44" data-action="summarise" data-id="${root.id}">${icon('summary', 16)} Summarise this chat</button>
    ${c.desk && c.ui.pane ? `<button class="btn btn--ghost btn--44" data-action="closeAllPanes">Close the side panel and all its tabs</button>` : ''}
    ${label('MOVE TO PROJECT')}<div class="pal-chips pal-chips--flush" data-noswipe>${orderedProjects(c.d).filter(x => x.id !== root.project_id).map(x =>
      `<button class="pal-chip" style="opacity:${x.active ? 1 : 0.6}" data-action="transferChat" data-id="${x.id}"${a.saving ? ' disabled' : ''}>${a.saving === x.id ? spinner() : `<span class="swatch-sq" style="background:${projectColor(x)}"></span>`}${esc(x.name)}</button>`).join('')}</div>
    <span class="card__desc">${n > 1 ? 'All its messages go with it. ' : ''}A reply after the move runs in the new project's folder, starting a fresh Claude Code session with this chat so far passed along.</span>
    ${a.error ? `<span class="form-err">${esc(a.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn-dismiss btn-dismiss--del" data-action="deleteChat" data-id="${root.id}"${a.saving ? ' disabled' : ''}>Delete${n > 1 ? ' whole chat' : ''}</button>
    </div>`}
  </div>`;
  return wrap('chatActions', inner, 'Move or delete');
}

// Bulk change on ticked tasks (queue sidebar, Home's Up next, a project's Activity tab): say in plain words what will
// happen and to how many, then do it on Confirm.
const PRIO = [[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']];
function bulkSheet(c) {
  const b = c.ui.bulk;
  if (!b) return '';
  const { ok, skipped } = bulkPlan(b.kind, b.ids);
  const n = ok.length, what = b.kind === 'move' ? 'chat' : 'task';
  const title = { cancel: 'Cancel tasks', move: 'Move to another project', priority: 'Change priority', rerun: 'Re-run tasks' }[b.kind];
  const dest = projectById(b.pid), level = (PRIO.find(([v]) => v === b.prio) || [])[1];
  let pick = '', say = '', ready = n > 0;
  if (b.kind === 'cancel') say = `Cancel ${plural(n, what)}? ${n === 1 ? 'It leaves' : 'They leave'} the line and won't run. You can undo right after.`;
  if (b.kind === 'rerun') say = `Re-run ${plural(n, what)}? ${n === 1 ? 'It goes' : 'They go'} to the back of the queue and ${n === 1 ? 'runs' : 'run'} again with the same prompt.`;
  if (b.kind === 'move') {
    const here = new Set(ok.map(t => t.project_id));
    pick = `<span class="pal-label">MOVE TO PROJECT</span><div class="pal-chips pal-chips--flush" data-noswipe>${orderedProjects(c.d).map(x =>
      `<button class="pal-chip${b.pid === x.id ? ' is-on' : ''}" style="opacity:${x.active ? 1 : 0.6}" data-action="bulkPick" data-k="pid" data-v="${x.id}"${b.busy ? ' disabled' : ''}><span class="swatch-sq" style="background:${projectColor(x)}"></span>${esc(x.name)}</button>`).join('')}</div>`;
    say = dest ? `Move ${plural(n, what)} to ${dest.name}? Each chat's messages go with it${here.size === 1 && here.has(b.pid) ? ', but these are already there' : ''}. A reply after the move runs in the new project's folder.` : `Pick the project to move ${plural(n, what)} to.`;
    ready = ready && !!dest;
  }
  if (b.kind === 'priority') {
    pick = `<span class="pal-label">NEW PRIORITY</span><div class="segmented segmented--start" data-noswipe>${PRIO.map(([v, l]) =>
      `<button class="segmented__opt${b.prio === v ? ' is-on' : ''}" data-action="bulkPick" data-k="prio" data-v="${v}"${b.busy ? ' disabled' : ''}>${l}</button>`).join('')}</div>`;
    say = level ? `Set ${plural(n, what)} to ${level} priority? ${n === 1 ? 'It keeps' : 'They keep'} the same place in line unless you reorder.` : `Pick a priority for ${plural(n, what)}.`;
    ready = ready && b.prio != null;
  }
  const skip = skipped ? `<span class="card__desc">${plural(skipped, 'ticked task')} can't be ${b.kind === 'cancel' ? 'cancelled' : b.kind === 'rerun' ? 're-run' : 'changed'} (${b.kind === 'rerun' ? 'only failed, stopped or cancelled ones can be re-run' : 'only tasks still waiting in line can be'}), so ${skipped === 1 ? 'it is' : 'they are'} left alone.</span>` : '';
  const none = n ? '' : `<span class="card__desc card__desc--lg">Nothing ticked can be changed this way.</span>`;
  const danger = b.kind === 'cancel';
  return wrap('bulk', `<div class="sheet__body sheet__body--form">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">${title}</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    ${pick}
    ${n ? `<span class="card__desc card__desc--lg">${esc(say)}</span>` : ''}${none}${skip}
    ${b.error ? `<span class="form-err">${esc(b.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--ghost btn--44" data-action="closeSheet">Not now</button>
      <button class="btn ${danger ? 'btn-dismiss btn-dismiss--del' : 'btn--primary'} btn--44" data-action="bulkConfirm"${ready && !b.busy ? '' : ' disabled'}>${b.busy ? spinner() : `${title.split(' ')[0] === 'Move' ? 'Move' : danger ? 'Cancel' : b.kind === 'rerun' ? 'Re-run' : 'Set'} ${plural(n, what)}`}</button>
    </div>
  </div>`, title);
}

// Edit a backlog item: the same text the item will run with, in a box big enough for real detail.
function editTodo(c) {
  const e = c.ui.todoEdit || {};
  const gone = !(c.d.todos[e.pid] || []).some(x => x.id === e.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Edit backlog item</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (gone) return wrap('editTodo', `<div class="sheet__body sheet__body--form">${head}<span class="card__desc card__desc--lg">This item is gone.</span></div>`, 'Edit backlog item');
  return wrap('editTodo', `<div class="sheet__body sheet__body--form">
    ${head}
    ${polishLabel('DETAILS', 'todoEditInput', e.text)}<textarea id="todoEditInput" class="input edit-prompt" rows="8" data-input="todoEdit.text" data-autogrow="360" aria-label="Backlog item">${esc(e.text)}</textarea>
    ${FEATURES.images ? `<div class="need__row"><span class="pal-label grow">PICTURES</span>${FEATURES.images ? `<label class="tool-btn polish-btn" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="todoEdit">${icon('image', 18)}</label>` : ''}</div>
    ${imageStrip(e.images, 'todoEdit')}` : ''}
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveTodoEdit"${e.saving || !e.text.trim() ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : 'Save'}</button>
      <button class="btn-dismiss" data-action="deleteTodoEdit">Delete</button>
    </div>
  </div>`, 'Edit backlog item');
}

// Edit a project note: change what it says, its pictures, what kind it is, or delete it.
function editNote(c) {
  const e = c.ui.noteEdit || {};
  const gone = !(c.d.notes[e.pid] || []).some(x => x.id === e.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Edit note</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (gone) return wrap('editNote', `<div class="sheet__body sheet__body--form">${head}<span class="card__desc card__desc--lg">This note is gone.</span></div>`, 'Edit note');
  return wrap('editNote', `<div class="sheet__body sheet__body--form">
    ${head}
    <div class="chip-row" role="radiogroup" aria-label="Kind of note">${NOTE_KINDS.map(([k, l]) =>
      `<button class="chip${e.kind === k ? ' is-on' : ''}" role="radio" aria-checked="${e.kind === k}" data-action="setNoteEditKind" data-kind="${k}">${l}</button>`).join('')}</div>
    ${polishLabel('DETAILS', 'noteEditInput', e.text)}<textarea id="noteEditInput" class="input edit-prompt" rows="8" data-input="noteEdit.text" data-autogrow="360" aria-label="Note">${esc(e.text)}</textarea>
    ${FEATURES.images ? `<div class="need__row"><span class="pal-label grow">PICTURES</span><label class="tool-btn polish-btn" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="noteEdit">${icon('image', 18)}</label></div>
    ${imageStrip(e.images, 'noteEdit')}` : ''}
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveNoteEdit"${e.saving || (!e.text.trim() && !(e.images || []).length) ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : 'Save'}</button>
      <button class="btn-dismiss" data-action="deleteNoteEdit">Delete</button>
    </div>
  </div>`, 'Edit note');
}

// Edit a schedule: what it runs, where, how often and when, on or off - or delete it.
const SCHED_CADENCES = [['once', 'Once'], ['daily', 'Every day'], ['weekdays', 'Weekdays'], ['weekly', 'Weekly']];
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function editSchedule(c) {
  const e = c.ui.schedEdit || {};
  const gone = !(c.d.schedules || []).some(x => x.id === e.id);
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">Edit schedule</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (gone) return wrap('editSchedule', `<div class="sheet__body sheet__body--form">${head}<span class="card__desc card__desc--lg">This schedule is gone.</span></div>`, 'Edit schedule');
  const label = s => `<span class="pal-label">${s}</span>`;
  const ms = c.d.models.filter(m => m.lane !== 'control');
  const ready = e.prompt.trim() && e.time && (e.cadence !== 'once' || e.date) && (e.cadence !== 'weekly' || e.weekday != null);
  const projects = orderedProjects(c.d).filter(x => !crmOnly(x) || x.id === e.pid);
  return wrap('editSchedule', `<div class="sheet__body sheet__body--form">
    ${head}
    ${label('WHAT IT RUNS')}<textarea id="schedPrompt" class="input edit-prompt" rows="4" data-input="sched.prompt" data-autogrow="300" aria-label="Prompt">${esc(e.prompt)}</textarea>
    ${label('NAME (OPTIONAL)')}<input class="input" maxlength="80" data-input="sched.label" aria-label="Name" placeholder="Same as the prompt" value="${esc(e.label)}">
    ${label('PROJECT')}<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Project">
      <button class="chip${e.pid == null ? ' is-on' : ''}" role="radio" aria-checked="${e.pid == null}" data-action="schedProject" data-id="">All projects</button>
      ${projects.map(p => `<button class="chip${e.pid === p.id ? ' is-on' : ''}" role="radio" aria-checked="${e.pid === p.id}" data-action="schedProject" data-id="${p.id}"><span class="swatch-sq" style="background:${projectColor(p)}"></span>${esc(p.name)}</button>`).join('')}</div>
    ${label('REPEAT')}<div class="segmented segmented--scroll sched-seg" role="radiogroup" aria-label="Repeat" data-noswipe>${SCHED_CADENCES.map(([k, l]) =>
      `<button class="segmented__opt${e.cadence === k ? ' is-scheduled' : ''}" role="radio" aria-checked="${e.cadence === k}" data-action="schedCadence" data-k="${k}">${l}</button>`).join('')}</div>
    ${e.cadence === 'weekly' ? `<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Day of the week">${WEEKDAYS.map((d, i) =>
      `<button class="chip chip--sm${e.weekday === i ? ' is-on' : ''}" role="radio" aria-checked="${e.weekday === i}" data-action="schedWeekday" data-d="${i}">${d}</button>`).join('')}</div>` : ''}
    ${label('TIME')}<div class="sched-when${e.cadence === 'once' ? '' : ' sched-when--time'}">
      ${e.cadence === 'once' ? `<input class="input" type="date" data-input="sched.date" aria-label="Date" value="${esc(e.date)}">` : ''}
      <input class="input" type="time" data-input="sched.time" aria-label="Time" value="${esc(e.time)}">
    </div>
    ${label('MODEL')}<div class="pal-chips" data-noswipe>${[{ id: 'auto', label: 'Auto' }, ...ms].map(m =>
      `<button class="pal-model${e.model === m.id ? ' is-on' : ''}" data-action="schedModel" data-id="${esc(m.id)}">${esc(m.label)}</button>`).join('')}</div>
    ${label('PRIORITY')}<div class="segmented sched-seg">${[[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']].map(([v, l]) =>
      `<button class="segmented__opt${e.priority === v ? (v === 1 ? ' is-urgent' : ' is-on') : ''}" data-action="schedPriority" data-v="${v}">${l}</button>`).join('')}</div>
    <div class="need__row sched-on"><span class="grow">${e.enabled ? 'On - runs at the time above' : 'Off - won\'t run until you turn it on'}</span>${toggle(!!e.enabled, 'schedEnabled', 'aria-label="Schedule on"', true)}</div>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="saveSchedule"${e.saving || !ready ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : 'Save'}</button>
      <button class="btn-dismiss" data-action="deleteSchedule">Delete</button>
    </div>
  </div>`, 'Edit schedule');
}

export function sheet(c) {
  switch (c.ui.sheet) {
    case 'editSchedule': return editSchedule(c);
    case 'palette': return palette(c);
    case 'plan': return planSheet(c);
    case 'usage': return usage(c);
    case 'account': return accountSheet(c, wrap, overallUsage(c));
    case 'accountEdit': return accountEditSheet(c, wrap);
    case 'addProject': return addProject(c);
    case 'keys': return keys();
    case 'summary': return summary(c);
    case 'editTask': return editTask(c);
    case 'retryEdit': return retryEdit(c);
    case 'runOpts': return runOpts(c);
    case 'chatActions': return chatActions(c);
    case 'bulk': return bulkSheet(c);
    case 'taskDetail': return detailSheet(c);
    case 'editTodo': return editTodo(c);
    case 'editNote': return editNote(c);
    case 'quick': return quickSheet(c, wrap);
    case 'runReview': return runReviewSheet(c, wrap);
    case 'event': return eventSheet(c, wrap);
    case 'phase': return phaseSheet(c, wrap);
    case 'report': return reportSheet(c, wrap);
    case 'genTasks': return generateSheet(c, wrap);
    case 'tour': return tourSheet(c);
    case 'help': return helpSheet();
    case 'preview': return previewSheet(c, wrap);
    default: return '';
  }
}

export { plural, modelLabel };
