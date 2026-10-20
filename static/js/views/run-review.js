// "Queue backlog items" sheet: every backlog item in queue order, to tick, reword and set up before it joins the queue.
// Opened by Run selected… / Queue… on a project's backlog; actions are the rq* ones in app.js.
// Each change saves as you make it (like the backlog itself), so closing the sheet loses nothing.
// Bulk update applies a model / account / mode / priority to every ticked item in one tap.

import { plural, modelState, PERMISSION_MODES } from '../format.js';
import { queuedTasks } from '../data.js';
import { esc, icon, spinner } from '../ui.js';
import { accountList, autoAccount, shortName, usesClaudeAccount } from './accounts.js';

const PRIORITIES = [[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']];
const DEFAULTS = { model_id: 'claude-code', permission_mode: 'acceptEdits', priority: 0 };
const valueOf = (t, k) => t[k] || DEFAULTS[k];

// Models a backlog item can run on: Auto (the router picks) plus every model you can pick by hand.
export function todoModels(c) {
  return [{ id: 'auto', label: 'Auto', dot: 'var(--text)', ok: true },
    ...c.d.models.filter(m => m.lane !== 'control').map(m => modelState(m, c.now)).map(m => ({ id: m.id, label: m.label, dot: m.dot, ok: m.available || m.rate }))];
}

export const pendingTodos = (c, pid) => (c.d.todos[pid] || []).filter(t => t.status === 'pending');

function modelSelect(models, value, path, label) {
  return `<select class="input input--sm rq-model" data-input="${path}" aria-label="${label}">
    ${value == null ? '<option value="" selected>Model…</option>' : ''}
    ${models.map(m => `<option value="${esc(m.id)}"${m.id === value ? ' selected' : ''}>${esc(m.label)}${m.ok ? '' : ' (busy)'}</option>`).join('')}</select>`;
}

// Which Claude account an item runs on ('auto' = its project's own / the default). Only for
// Claude Code models - an API model has no account to pick, so its row says which key it uses.
// `value` null = the ticked items differ.
function acctSelect(c, pid, value, path, label) {
  const list = accountList(c);
  if (!list.length) return '';
  const auto = autoAccount(c, pid);
  return `<select class="input input--sm rq-model rq-acct" data-input="${path}" aria-label="${label}" title="Claude account">
    ${value == null ? '<option value="" selected>Account…</option>' : ''}
    <option value="auto"${value === '' ? ' selected' : ''}>Auto${auto ? ' · ' + esc(shortName(auto)) : ''}</option>
    ${list.map(a => `<option value="${esc(a.id)}"${a.id === value ? ' selected' : ''}>${esc(shortName(a))}${a.email ? ' · ' + esc(a.email) : ''}</option>`).join('')}</select>`;
}
const KEY_NAMES = { openai: 'OpenAI key', gemini: 'Gemini key', tools: 'Connected accounts' };
function acctCell(c, pid, t) {
  const model = valueOf(t, 'model_id');
  if (usesClaudeAccount(c, model)) return acctSelect(c, pid, t.only_account || '', `rq.acct.${t.id}`, 'Account');
  const m = c.d.models.find(x => x.id === model);
  return m && KEY_NAMES[m.provider] ? `<span class="meta rq-key" title="This model doesn't use a Claude account">${KEY_NAMES[m.provider]}</span>` : '';
}

// One tap sets it. `value` null = the ticked items differ, so nothing shows as on.
function modeSeg(value, attrs) {
  return `<div class="segmented rq-seg" role="group" aria-label="Mode">${PERMISSION_MODES.map(m =>
    `<button class="segmented__opt${value === m.id ? ' is-on' : ''}" title="${esc(m.label)}: ${esc(m.sub)}" ${attrs} data-k="permission_mode" data-v="${m.id}">${esc(m.short)}</button>`).join('')}</div>`;
}
function prioritySeg(value, attrs) {
  return `<div class="segmented rq-seg" role="group" aria-label="Priority">${PRIORITIES.map(([v, l]) =>
    `<button class="segmented__opt${value === v ? (v === 1 ? ' is-urgent' : ' is-on') : ''}" ${attrs} data-k="priority" data-v="${v}">${l}</button>`).join('')}</div>`;
}

function row(c, r, t, models) {
  const attrs = `data-action="rqSet" data-id="${t.id}"`;
  const pics = t.images && t.images.length;
  // Always a text box (looks like plain text until you tap it): no extra tap to start editing.
  const text = `<textarea id="rqText${t.id}" class="rq-row__text" rows="1" data-input="rq.text.${t.id}" data-enter="rqSaveText" data-autogrow="200" data-fit aria-label="What this item asks" title="Tap to edit · Enter saves · Shift+Enter for a new line">${esc(r.text[t.id] != null ? r.text[t.id] : t.text)}</textarea>`;
  return `<div class="rq-row${t.checked ? '' : ' is-off'}" data-key="rq${t.id}">
    <button class="check" role="checkbox" aria-checked="${t.checked ? 'true' : 'false'}" data-action="rqCheck" data-id="${t.id}" aria-label="${t.checked ? 'Leave out' : 'Include'}">${t.checked ? '✓' : ''}</button>
    <div class="rq-row__main">
      <div class="rq-row__top">${text}${pics ? `<span class="meta rq-row__pics" title="${plural(pics, 'picture')} attached">${icon('image', 14)}${pics > 1 ? pics : ''}</span>` : ''}</div>
      <div class="rq-row__opts">
        ${modelSelect(models, valueOf(t, 'model_id'), `rq.model.${t.id}`, 'Model')}
        ${acctCell(c, r.pid, t)}
        ${modeSeg(valueOf(t, 'permission_mode'), attrs)}
        ${prioritySeg(valueOf(t, 'priority'), attrs)}
      </div>
    </div>
  </div>`;
}

// The value every ticked item shares, or null when they differ.
const shared = (items, k) => (items.length && items.every(t => valueOf(t, k) === valueOf(items[0], k)) ? valueOf(items[0], k) : null);

function bulkPanel(c, pid, sel, models) {
  const attrs = 'data-action="rqBulk"';
  if (!sel.length) return `<div class="rq-bulk"><span class="meta">Tick some items to update them together.</span></div>`;
  return `<div class="rq-bulk">
    <span class="rq-bulk__note">Applies to the ${plural(sel.length, 'ticked item')}</span>
    <div class="rq-bulk__grid">
      <span class="pal-label">MODEL</span>${modelSelect(models, shared(sel, 'model_id'), 'rq.bulk.model', 'Model for all ticked items')}
      ${accountList(c).length ? `<span class="pal-label">ACCOUNT</span>${acctSelect(c, pid, sel.every(t => (t.only_account || '') === (sel[0].only_account || '')) ? sel[0].only_account || '' : null, 'rq.bulk.only_account', 'Claude account for all ticked items')}` : ''}
      <span class="pal-label">MODE</span>${modeSeg(shared(sel, 'permission_mode'), attrs)}
      <span class="pal-label">PRIORITY</span>${prioritySeg(shared(sel, 'priority'), attrs)}
    </div>
  </div>`;
}

export function runReviewSheet(c, wrap) {
  const r = c.ui.runReview || { pid: null, bulk: false, text: {} };
  const todos = pendingTodos(c, r.pid);
  const sel = todos.filter(t => t.checked);
  const all = todos.length && sel.length === todos.length;
  const models = todoModels(c);
  const ahead = queuedTasks(c.d).length;
  const busy = !!c.ui.busy.backlogRun;
  const head = `<div class="sheet__head"><span class="sheet__title">Queue backlog items</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  if (!todos.length) return wrap('runReview', `${head}<div class="sheet__body"><span class="card__desc card__desc--lg">The backlog is empty.</span></div>`, 'Queue backlog items');
  return wrap('runReview', `${head}
    <div class="rq-tools">
      <button class="btn--text" data-action="rqAll" data-all="${all ? 1 : 0}">${all ? 'Clear all' : 'Select all'}</button>
      <span class="meta grow">${sel.length} of ${todos.length} ticked</span>
      <button class="btn btn--sm${r.bulk ? ' is-on' : ''}" data-action="rqBulkToggle" aria-expanded="${r.bulk}">Bulk update</button>
    </div>
    <div class="sheet__body rq-list">${r.bulk ? bulkPanel(c, r.pid, sel, models) : ''}${todos.map(t => row(c, r, t, models)).join('')}</div>
    <div class="rq-foot">
      <span class="meta grow">${sel.length ? `Runs in this order, after ${plural(ahead, 'task')} already queued` : 'Nothing ticked'}</span>
      <button class="btn btn--primary btn--44" data-action="rqQueue"${busy || !sel.length ? ' disabled' : ''}>${busy ? spinner() + 'Queueing…' : `Queue ${plural(sel.length, 'item')}`}</button>
    </div>`, 'Queue backlog items');
}
