// CRM clients (clients.py): the Clients page (a searchable list) and one client's page - its details,
// projects (stage, due date, value), contacts, and total / active contract value.
// Data: c.d.clients (GET /clients, every poll) and c.d.clientPages[id] (GET /clients/{id} while it's open).
// UI state (clients-ui.js): clList { q, status }, clNew { name, status, saving, error } (the new-client form),
// clEdit { cid, k, v, saving, error } (the detail being edited), clDel { cid, busy } (delete asked for).

import { esc, icon, emptyState } from '../ui.js';
import { plural } from '../format.js';
import { clientFill, projectColor } from '../data.js';
import { stageInfo, money } from '../crm-table.js';
import { dueBadge, mdHTML } from './overview.js';
import { contactsCard } from './contacts.js';
import { activityCard } from './crm-activity.js';

// [key, label, colour] - same keys as clients.STATUSES on the server.
export const CLIENT_STATUSES = [['prospect', 'Prospect', 'var(--review)'], ['active', 'Active', 'var(--run)'], ['past', 'Past', 'var(--text-meta)']];
const statusInfo = k => CLIENT_STATUSES.find(s => s[0] === k) || CLIENT_STATUSES[0];
const TILE_COLORS = ['#e8825a', '#e8c24f', '#4fbf8a', '#4f9de8', '#b583e8', '#e86a8f', '#5fc4c9', '#9aa3b2'];
export const LIST_DEFAULT = { q: '', status: '' };

export const clientAvatar = (cl, cls = '') =>
  `<span class="avatar cl-av ${cls}" style="background:${clientFill(cl)}" aria-hidden="true">${cl.logo ? '' : esc((cl.name || '?').charAt(0).toUpperCase())}</span>`;
export const statusChip = k => { const [, l, col] = statusInfo(k); return `<span class="cl-status" style="--c:${col}">${l}</span>`; };

// "$7,000" or "$7,000 + ₹3,00,000" - one figure per currency.
export const valueText = (rows, key) => (rows || []).filter(r => r[key]).map(r => money(r[key], r.currency)).join(' + ');

// Words a client is found by: its own fields and its contacts' names.
export function clientMatches(cl, q, contacts = []) {
  const terms = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = [cl.name, cl.industry, cl.website, cl.address, cl.notes, ...contacts.filter(x => x.client_id === cl.id).map(x => `${x.name} ${x.role || ''} ${x.email || ''}`)]
    .filter(Boolean).join(' ').toLowerCase();
  return terms.every(t => hay.includes(t));
}

// ---------------------------------------------------------------- the list
function newClientForm(c) {
  const n = c.ui.clNew;
  if (!n) return '';
  return `<div class="card card--pad cl-new" data-key="cl-new"><span class="card__title">New client</span>
    <input id="cl-new-name" class="input" value="${esc(n.name)}" data-input="cl.new.name" data-enter="clNewSave" maxlength="120" placeholder="Company or person, e.g. Acme Ltd" autocomplete="off" aria-label="Client name">
    <div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${CLIENT_STATUSES.map(([v, l]) =>
      `<button class="segmented__opt${n.status === v ? ' is-on' : ''}" role="radio" aria-checked="${n.status === v}" data-action="clNewStatus" data-v="${v}">${l}</button>`).join('')}</div>
    <span class="card__desc">Add the rest - industry, website, address, contacts - on its page.</span>
    <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="clNewSave"${n.saving || !n.name.trim() ? ' disabled' : ''}>${n.saving ? 'Creating…' : 'Create client'}</button>
      <button class="btn btn--ghost btn--sm" data-action="clNewCancel">Cancel</button></div>
    ${n.error ? `<span class="form-err">${esc(n.error)}</span>` : ''}</div>`;
}

function clientRow(cl, i) {
  const active = valueText(cl.value, 'active'), total = valueText(cl.value, 'total');
  const counts = [plural(cl.project_count || 0, 'project'), cl.active_projects ? `${cl.active_projects} active` : '', plural(cl.contact_count || 0, 'contact')].filter(Boolean).join(' · ');
  return `<button class="cl-row" data-action="openClient" data-id="${cl.id}" data-key="cl${cl.id}" style="animation-delay:${Math.min(i, 8) * 40}ms">
    ${clientAvatar(cl)}
    <span class="cl-row__text"><span class="cl-row__name truncate">${esc(cl.name)}</span>
      <span class="cl-row__meta">${statusChip(cl.status)}${cl.industry ? `<span class="truncate">${esc(cl.industry)}</span>` : ''}<span class="meta">${counts}</span></span></span>
    <span class="cl-row__value">${active ? `<b>${esc(active)}</b><span class="meta">active</span>` : total ? `<b>${esc(total)}</b><span class="meta">total</span>` : ''}</span>
  </button>`;
}

export function clientsPage(c) {
  const { d, ui } = c;
  const st = { ...LIST_DEFAULT, ...(ui.clList || {}) };
  const all = d.clients || [];
  // The page can be newer than the server it talks to (the service picks up new code on restart).
  if (d.clientsMissing) {
    return `<div class="page page--narrow" data-key="clients"><div class="card card--pad" data-key="cl-restart"><span class="card__title">Clients are almost ready</span>
      <span class="card__desc">Restart the Den from the dashboard to start keeping clients and contacts, and linking them to projects.</span></div></div>`;
  }
  if (!all.length && !ui.clNew) {
    return `<div class="page page--narrow" data-key="clients">${emptyState({ title: 'No clients yet', body: 'Keep the companies and people you work for here: their details, projects, contacts and what the work is worth.', label: '+ New client', attrs: 'data-action="clNewOpen"', key: 'cl-empty' })}</div>`;
  }
  const counts = Object.fromEntries(CLIENT_STATUSES.map(([k]) => [k, all.filter(x => x.status === k).length]));
  const list = all.filter(cl => (!st.status || cl.status === st.status) && clientMatches(cl, st.q, d.contacts || []));
  const chip = (k, l, n) => `<button class="chip${st.status === k ? ' is-on' : ''}" data-action="clFilter" data-v="${k}">${l}${n != null ? ` <span class="chip__count">${n}</span>` : ''}</button>`;
  return `<div class="page page--narrow cl-page" data-key="clients">
    <div class="cl-tools">
      <div class="search cl-search">${icon('search', 16, { strokeWidth: 1.8 })}<input id="clSearch" type="search" placeholder="Search clients, industries, contacts" value="${esc(st.q)}" data-input="cl.q" aria-label="Search clients" autocomplete="off"></div>
      ${ui.clNew ? '' : `<button class="btn btn--primary btn--sm" data-action="clNewOpen">+ New client</button>`}
    </div>
    <div class="chip-row" data-noswipe>${chip('', 'All', all.length)}${CLIENT_STATUSES.map(([k, l]) => chip(k, l, counts[k])).join('')}</div>
    ${newClientForm(c)}
    <div class="cl-list">${list.length ? list.map(clientRow).join('') : `<span class="card__desc cl-none">No client matches${st.q.trim() ? ` “${esc(st.q.trim())}”` : ''}.</span>`}</div>
  </div>`;
}

// ---------------------------------------------------------------- one client
const pencil = '<span class="crm-f__pen" aria-hidden="true">✎</span>';
const placeholder = s => `<span class="crm-f__ph">${s}</span>`;
const editing = (c, cl, k) => { const e = c.ui.clEdit; return e && e.cid === cl.id && e.k === k ? e : null; };

function saveRow(e, clear = true) {
  return `<div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="clSave"${e.saving ? ' disabled' : ''}>${e.saving ? 'Saving…' : 'Save'}</button>
    <button class="btn btn--ghost btn--sm" data-action="clCancel">Cancel</button>${clear ? `<button class="btn btn--ghost btn--sm" data-action="clClear">Clear</button>` : ''}</div>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}`;
}

function field(c, cl, k, label, shown, editor) {
  const e = editing(c, cl, k);
  return `<div class="crm-f${e ? ' is-editing' : ''}" data-key="clf-${k}"><span class="crm-f__l">${label}</span>
    ${e ? `<div class="crm-ed">${editor(e)}</div>` : `<button class="crm-f__v" data-action="clEdit" data-k="${k}" aria-label="Edit ${esc(label.toLowerCase())}">${shown}${pencil}</button>`}</div>`;
}
const input = (k, attrs = '') => e => `<input id="cl-${k}" class="input" value="${esc(e.v)}" data-input="cl.v" data-enter="clSave" autocomplete="off" ${attrs}>${saveRow(e, k !== 'name')}`;

function look(cl) {
  const cur = cl.color ? cl.color.toLowerCase() : null, custom = cur && !TILE_COLORS.includes(cur);
  const sw = TILE_COLORS.map(col => `<button class="acct-swatch${cur === col ? ' is-on' : ''}" style="--c:${col}" data-action="clColor" data-v="${col}" aria-label="Use ${col}" aria-pressed="${cur === col}"></button>`).join('');
  return `<div class="crm-look">${clientAvatar(cl, 'crm-look__tile')}<div class="crm-look__ctl">
    <div class="crm-look__row">${sw}<label class="acct-swatch acct-swatch--custom${custom ? ' is-on' : ''}"${custom ? ` style="--c:${esc(cur)};background:var(--c)"` : ''} title="Pick any colour"><input type="color" value="${esc(cur || '#9aa3b2')}" data-cl-change="clColor" aria-label="Custom colour"></label>
      ${cur ? `<button class="btn btn--ghost btn--sm" data-action="clColor" data-v="">Automatic</button>` : ''}</div>
    <div class="crm-look__row"><button class="btn btn--sm" data-action="clLogoPick">${cl.logo ? 'Change picture' : 'Add picture'}</button>
      ${cl.logo ? `<button class="btn btn--ghost btn--sm" data-action="clLogoDel">Remove picture</button>` : ''}
      <input type="file" accept="image/*" hidden data-cl-logo data-cl-change="clLogoPicked"></div></div></div>`;
}

function details(c, cl) {
  const g = (k, label, shown, ed) => field(c, cl, k, label, shown, ed);
  const site = cl.website ? esc(cl.website.replace(/^https?:\/\//, '').replace(/\/$/, '')) : placeholder('Add website');
  const open = cl.website && !editing(c, cl, 'website') ? `<a class="btn btn--ghost btn--sm cl-open" href="${esc(cl.website)}" target="_blank" rel="noopener noreferrer" aria-label="Open website">Open ↗</a>` : '';
  return `<section class="card card--pad crm-card" data-key="cl-details"><div class="crm-card__head"><span class="card__title">Details</span></div>
    <div class="crm-fields">
      ${g('name', 'Name', esc(cl.name), input('name', 'maxlength="120" aria-label="Name"'))}
      <div class="crm-f" data-key="clf-status"><span class="crm-f__l">Status</span><div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${CLIENT_STATUSES.map(([v, l, col]) =>
        `<button class="segmented__opt${cl.status === v ? ' is-on' : ''}" role="radio" aria-checked="${cl.status === v}" data-action="clSet" data-k="status" data-v="${v}"><span class="dot" style="--c:${col};width:7px;height:7px"></span>${l}</button>`).join('')}</div></div>
      ${g('industry', 'Industry', cl.industry ? esc(cl.industry) : placeholder('Add industry'), input('industry', 'maxlength="120" placeholder="e.g. Retail, Fintech" aria-label="Industry"'))}
      <div class="cl-site">${g('website', 'Website', site, input('website', 'type="url" inputmode="url" autocapitalize="off" spellcheck="false" placeholder="acme.com" aria-label="Website"'))}${open}</div>
      ${g('address', 'Address', cl.address ? `<span class="cl-addr">${esc(cl.address)}</span>` : placeholder('Add address'),
        e => `<textarea id="cl-address" class="input" rows="3" data-input="cl.v" maxlength="500" placeholder="Street, city, country" aria-label="Address">${esc(e.v)}</textarea>${saveRow(e)}`)}
      <div class="crm-f" data-key="clf-look"><span class="crm-f__l">Look</span>${look(cl)}</div>
    </div></section>`;
}

function notesCard(c, cl) {
  const e = editing(c, cl, 'notes');
  const body = e
    ? `<textarea id="cl-notes" class="input crm-desc__ta" rows="6" data-input="cl.v" data-autogrow="420" placeholder="How you met, how they like to work, billing quirks… Markdown works.">${esc(e.v)}</textarea>${saveRow(e, false)}`
    : cl.notes ? `<div class="crm-md">${mdHTML(cl.notes)}</div>`
    : `<button class="crm-empty" data-action="clEdit" data-k="notes">Add notes: how you met, how they like to work, anything worth knowing.</button>`;
  return `<section class="card card--pad crm-card" data-key="cl-notes"><div class="crm-card__head"><span class="card__title">Notes</span>
    ${e || !cl.notes ? '' : `<button class="btn--text" data-action="clEdit" data-k="notes">Edit</button>`}</div>${body}</section>`;
}

function projectsCard(c, cl) {
  const rows = (cl.projects || []).map(p => {
    const [, sl, sc] = stageInfo(p.stage);
    const due = dueBadge(p) || (p.due_date && p.schedule && p.schedule.due_state === 'done' ? '<span class="crm-due crm-due--later">Done</span>' : '');
    return `<button class="cl-proj" data-action="clOpenProject" data-id="${p.id}" data-key="clp${p.id}">
      <span class="nav-proj__swatch" style="background:${projectColor(p)}"></span>
      <span class="cl-proj__name truncate">${esc(p.name)}</span>
      <span class="cl-stage" style="--c:${sc}">${sl}</span>
      <span class="cl-proj__due">${due || '<span class="meta">No due date</span>'}</span>
      <span class="cl-proj__value">${p.contract_value != null ? esc(money(p.contract_value, p.currency)) : '<span class="meta">–</span>'}</span>
    </button>`;
  }).join('');
  const linked = new Set((cl.projects || []).map(p => p.id));
  const free = (c.d.projects || []).filter(p => !p.is_general && !linked.has(p.id));
  const picker = free.length ? `<select class="input cn-pick" data-cl-change="clLinkProject" aria-label="Add a project to this client">
      <option value="">Add a project to ${esc(cl.name)}…</option>
      ${free.map(p => `<option value="${p.id}">${esc(p.name)}${p.client_name ? ` (now ${esc(p.client_name)})` : ''}</option>`).join('')}</select>` : '';
  return `<section class="card card--pad crm-card" data-key="cl-projects"><div class="crm-card__head"><span class="card__title">Projects${cl.projects.length ? ` <span class="meta">${cl.projects.length}</span>` : ''}</span></div>
    ${rows ? `<div class="cl-projs">${rows}</div>` : `<span class="card__desc">No projects for ${esc(cl.name)} yet. Pick this client on a project's Overview tab, or add one here.</span>`}
    ${picker}</section>`;
}

function highlights(cl) {
  const tile = (label, v, note) => `<div class="crm-hl__t"><span class="crm-hl__l">${label}</span><span class="crm-hl__v">${v}</span>${note ? `<span class="crm-hl__n">${note}</span>` : ''}</div>`;
  const total = valueText(cl.value, 'total'), active = valueText(cl.value, 'active');
  return `<div class="crm-hl cl-hl" data-key="cl-hl">
    ${tile('TOTAL VALUE', total ? esc(total) : '–', 'all projects but lost ones')}
    ${tile('ACTIVE VALUE', active ? esc(active) : '–', 'work under way or on hold')}
    ${tile('PROJECTS', String(cl.project_count), cl.active_projects ? `${cl.active_projects} active` : '')}
    ${tile('CONTACTS', String(cl.contact_count), '')}
  </div>`;
}

function removeCard(c, cl) {
  const d = c.ui.clDel && c.ui.clDel.cid === cl.id ? c.ui.clDel : null;
  const n = cl.projects.length;
  return `<section class="card card--pad crm-card" data-key="cl-remove"><div class="crm-card__head"><span class="card__title">Keep or remove</span></div>
    <span class="card__desc">${n ? `Done working with ${esc(cl.name)}? Mark them <b>Past</b> - everything is kept. A client with projects can't be deleted.`
      : `Deleting ${esc(cl.name)} keeps its contacts (they just won't belong to a client).`}</span>
    <div class="crm-ed__acts">${cl.status !== 'past' ? `<button class="btn btn--sm" data-action="clSet" data-k="status" data-v="past">Mark as past</button>` : ''}
      ${n ? '' : d ? `<button class="btn btn--danger btn--sm" data-action="clDelete"${d.busy ? ' disabled' : ''}>${d.busy ? 'Deleting…' : 'Delete for good'}</button><button class="btn btn--ghost btn--sm" data-action="clDelCancel">Cancel</button>`
        : `<button class="btn btn--ghost btn--sm crm-manage__del" data-action="clDelOpen">Delete…</button>`}</div>
  </section>`;
}

export function clientPage(c) {
  const id = c.ui.clientId, cl = c.d.clientPages[id];
  if (cl === null) {
    return `<div class="page page--narrow" data-key="client-gone">${emptyState({ title: 'This client is gone', body: 'It was deleted, maybe from another device.', label: 'All clients', attrs: 'data-action="go" data-view="clients"', key: 'cl-gone' })}</div>`;
  }
  if (!cl) return `<div class="page page--narrow" data-key="client-loading"><span class="meta">Loading…</span></div>`;
  return `<div class="page page--wide cl-page" data-key="client-${cl.id}">
    <div class="cl-head" data-key="cl-head">${clientAvatar(cl, 'cl-av--lg')}
      <div class="cl-head__text"><span class="cl-head__name">${esc(cl.name)}</span>
        <span class="cl-row__meta">${statusChip(cl.status)}${cl.industry ? `<span>${esc(cl.industry)}</span>` : ''}</span></div>
      <button class="btn btn--ghost btn--sm cl-head__back" data-action="go" data-view="clients">All clients</button></div>
    ${highlights(cl)}
    <div class="crm-grid">
      <div class="crm-col">${projectsCard(c, cl)}${activityCard(c, { at: 'client', id: cl.id })}${contactsCard(c, { at: 'client', id: cl.id, contacts: cl.contacts, clientId: cl.id, clients: c.d.clients || [] })}${notesCard(c, cl)}</div>
      <div class="crm-col">${details(c, cl)}${removeCard(c, cl)}</div>
    </div>
  </div>`;
}
