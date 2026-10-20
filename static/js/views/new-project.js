// The New project sheet (sheet 'addProject'): a CRM record with everything the Overview tab keeps -
// name, category, stage, priority, health, dates, value, owner, description, tags, links - and an
// optional folder. With a folder Octopus can work on it; without one it is a CRM-only project.
// State: ui.onboard (see NEW_PROJECT in crm-ui.js); actions np… live in crm-ui.js.

import { esc, scopeSwitch, spinner } from '../ui.js';
import { PROJECT_STATUSES } from '../data.js';
import { PRIORITIES, HEALTHS, BILLING, LINK_KINDS } from './overview.js';

const seg = (k, options, cur, clearable = false) =>
  `<div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${options.map(([v, l, col]) =>
    `<button type="button" class="segmented__opt${cur === v ? ' is-on' : ''}" role="radio" aria-checked="${cur === v}" data-action="npSet" data-k="${k}" data-v="${v}"${col ? ` style="--c:${col}"` : ''}>${col ? `<span class="dot" style="--c:${col};width:7px;height:7px"></span>` : ''}${l}</button>`).join('')}${clearable && cur ? `<button type="button" class="segmented__opt" data-action="npSet" data-k="${k}" data-v="" title="Clear">—</button>` : ''}</div>`;

const row = (label, body, cls = '') => `<label class="np-f${cls}"><span class="np-f__l">${label}</span>${body}</label>`;
const grp = (label, body) => `<div class="np-f"><span class="np-f__l">${label}</span>${body}</div>`;

export function newProjectSheet(c, wrap) {
  const ob = c.ui.onboard, busy = c.ui.busy.addProject;
  const v = k => esc(ob[k] == null ? '' : ob[k]);
  const links = ob.links || [];
  const inner = `<form class="sheet__body sheet__body--form np" data-submit="addProject">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">New project</span><button class="icon-btn icon-btn--44" type="button" data-action="closeSheet" aria-label="Close">✕</button></div>
    <span class="card__desc card__desc--lg">Keep a project's record here: who it's for, its stage, dates and value. Add a folder on this Mac if Octopus should work on it too.</span>
    <input id="addName" class="input" name="name" placeholder="Project name" value="${v('name')}" data-input="onboard.name" autocomplete="off" aria-label="Name" maxlength="120">
    ${scopeSwitch(ob.category || 'work', 'setNewCategory', [['work', 'Work'], ['personal', 'Personal']])}
    ${row('Folder <span class="meta">optional</span>', `<input class="input input--mono" name="path" placeholder="~/code/folder" value="${v('path')}" data-input="onboard.path" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Folder">
      <span class="np-f__hint">${(ob.path || '').trim() ? 'Octopus will be able to run tasks in this folder.' : 'Leave empty for a CRM-only project. You can link a folder later from its Overview.'}</span>`)}
    ${row('Stage', `<select class="input" data-input="onboard.status" aria-label="Stage">${PROJECT_STATUSES.map(([k, l]) => `<option value="${k}"${(ob.status || 'not_started') === k ? ' selected' : ''}>${l}</option>`).join('')}</select>`)}
    ${grp('Priority', seg('priority', PRIORITIES, ob.priority || 'medium'))}
    ${grp('Health', seg('health', HEALTHS, ob.health || 'on_track'))}
    <div class="np-two">
      ${row('Start', `<input class="input" type="date" value="${v('start_date')}" data-input="onboard.start_date" aria-label="Start date">`)}
      ${row('Due', `<input class="input" type="date" value="${v('due_date')}" data-input="onboard.due_date" aria-label="Due date">`)}
    </div>
    ${row('Contract value', `<div class="crm-ed__money"><input class="input" type="number" min="0" step="0.01" inputmode="decimal" placeholder="0" value="${v('contract_value')}" data-input="onboard.contract_value" aria-label="Contract value">
      <input class="input crm-ed__cur" maxlength="3" placeholder="USD" value="${v('currency')}" data-input="onboard.currency" aria-label="Currency" autocapitalize="characters" autocomplete="off"></div>`)}
    ${grp('Billing', seg('billing_type', BILLING, ob.billing_type || '', true))}
    ${row('Owner', `<input class="input" maxlength="120" placeholder="Who is responsible" value="${v('owner')}" data-input="onboard.owner" autocomplete="off" aria-label="Owner">`)}
    ${row('Description', `<textarea class="input" rows="4" data-autogrow="320" placeholder="What it is, the scope, anything worth knowing. Markdown works." data-input="onboard.description" aria-label="Description">${v('description')}</textarea>`)}
    ${row('Tags', `<input class="input" maxlength="400" placeholder="Separated by commas, e.g. retainer, shopify" value="${v('tags')}" data-input="onboard.tags" autocomplete="off" aria-label="Tags">`)}
    ${grp('Links', `${links.length ? `<div class="crm-links">${links.map((x, i) => `<div class="crm-link"><span class="crm-link__a"><b>${esc(x.label || 'Link')}</b><span class="meta truncate">${esc(x.url)}</span></span>
        <button type="button" class="icon-btn icon-btn--ghost" data-action="npLinkDel" data-i="${i}" aria-label="Remove link">✕</button></div>`).join('')}</div>` : ''}
      <div class="chip-row crm-link__kinds" data-noswipe>${LINK_KINDS.map(k => `<button type="button" class="chip chip--sm${(ob.linkLabel ?? 'Repo') === k ? ' is-on' : ''}" data-action="npLinkKind" data-v="${esc(k)}">${k}</button>`).join('')}</div>
      <div class="crm-add crm-add--link"><input class="input crm-add__label" maxlength="60" placeholder="Label" value="${esc(ob.linkLabel ?? 'Repo')}" data-input="onboard.linkLabel" autocomplete="off" aria-label="Link label">
        <input id="np-link-url" class="input" type="url" inputmode="url" placeholder="https://…" value="${v('linkUrl')}" data-input="onboard.linkUrl" data-enter="npLinkAdd" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Link URL">
        <button type="button" class="btn btn--ghost btn--38" data-action="npLinkAdd"${(ob.linkUrl || '').trim() ? '' : ' disabled'}>Add</button></div>`)}
    ${ob.error ? `<span class="form-err">${esc(ob.error)}</span>` : ''}
    <button class="btn btn--primary btn--50" type="submit" data-action="npSubmit"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Creating…' : (ob.path || '').trim() ? 'Create project' : 'Create CRM project'}</button>
  </form>`;
  return wrap('addProject', inner, 'New project');
}
