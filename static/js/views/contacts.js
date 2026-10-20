// Contacts (clients.py): the contact list and forms shared by a client's page (views/clients.js) and a
// project's Overview tab (views/overview.js). Actions and inputs are in clients-ui.js.
// UI state: cnNew { at: 'client' | 'project', id, name, role, email, phone, preferred_method, project_role, saving, error }
// (the quick-add form), cnEdit { id, name, role, email, phone, notes, preferred_method, saving, error } (a contact being edited).

import { esc, emptyState } from '../ui.js';
import { clientById, clientFill, projectById, projectColor } from '../data.js';
import { activityCard } from './crm-activity.js';

// [key, label] - same keys as clients.CONTACT_METHODS on the server.
export const METHODS = [['email', 'Email'], ['phone', 'Call'], ['text', 'Text'], ['whatsapp', 'WhatsApp']];
const methodLabel = k => (METHODS.find(m => m[0] === k) || [])[1] || '';
export const initials = name => (name || '?').trim().split(/\s+/).slice(0, 2).map(w => w.charAt(0).toUpperCase()).join('') || '?';

const telHref = phone => `tel:${phone.replace(/[^0-9+]/g, '')}`;
const waHref = phone => `https://wa.me/${phone.replace(/[^0-9]/g, '')}`;

// How to reach them, their preferred way first.
function reach(ct) {
  const out = [];
  if (ct.email) out.push(['email', `<a href="mailto:${esc(ct.email)}" class="cn-link">${esc(ct.email)}</a>`]);
  if (ct.phone) out.push(['phone', `<a href="${esc(telHref(ct.phone))}" class="cn-link">${esc(ct.phone)}</a>`]);
  if (ct.phone && ct.preferred_method === 'whatsapp') out.push(['whatsapp', `<a href="${esc(waHref(ct.phone))}" target="_blank" rel="noopener noreferrer" class="cn-link">WhatsApp</a>`]);
  const pref = ct.preferred_method;
  out.sort((a, b) => (b[0] === pref) - (a[0] === pref));
  return out.map(x => x[1]).join('<span class="cn-sep" aria-hidden="true">·</span>');
}

function chips(group, k, options, cur) {
  return `<div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${options.map(([v, l]) =>
    `<button class="segmented__opt${cur === v ? ' is-on' : ''}" role="radio" aria-checked="${cur === v}" data-action="cnSet" data-f="${group}" data-k="${k}" data-v="${v}">${l}</button>`).join('')}</div>`;
}

// The add / edit form. group: 'new' (cnNew) or 'edit' (cnEdit).
export function contactForm(f, group, { project = false, title = '' } = {}) {
  const inp = (k, label, attrs = '') => `<label class="cn-f"><span class="cn-f__l">${label}</span><input${k === 'name' ? ` id="cn-${group}-name"` : ''} class="input" value="${esc(f[k] || '')}" data-input="cn.${group}.${k}" data-enter="${group === 'new' ? 'cnSave' : 'cnUpdate'}" autocomplete="off" ${attrs}></label>`;
  return `<div class="cn-form" data-key="cn-form-${group}${f.id ? '-' + f.id : ''}">
    ${title ? `<span class="cn-form__title">${title}</span>` : ''}
    <div class="cn-form__grid">
      ${inp('name', 'Name', 'maxlength="120" placeholder="Full name" aria-required="true"')}
      ${inp('role', 'Role', 'maxlength="120" placeholder="e.g. Head of marketing"')}
      ${inp('email', 'Email', 'type="email" inputmode="email" maxlength="254" autocapitalize="off" spellcheck="false" placeholder="name@company.com"')}
      ${inp('phone', 'Phone', 'type="tel" inputmode="tel" maxlength="40" placeholder="+44 20 7946 0000"')}
    </div>
    <div class="cn-f"><span class="cn-f__l">Prefers</span>${chips(group, 'preferred_method', [...METHODS, ['', 'No preference']], f.preferred_method || '')}</div>
    ${project ? `<div class="cn-f"><span class="cn-f__l">On this project</span>${chips(group, 'project_role', [['stakeholder', 'Stakeholder'], ['key', 'Key contact']], f.project_role || 'stakeholder')}</div>` : ''}
    <label class="cn-f"><span class="cn-f__l">Notes</span><textarea class="input cn-f__notes" rows="2" data-input="cn.${group}.notes" maxlength="20000" placeholder="Anything worth remembering">${esc(f.notes || '')}</textarea></label>
    <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="${group === 'new' ? 'cnSave' : 'cnUpdate'}"${f.saving ? ' disabled' : ''}>${f.saving ? 'Saving…' : group === 'new' ? 'Add contact' : 'Save'}</button>
      <button class="btn btn--ghost btn--sm" data-action="cnCancel" data-f="${group}">Cancel</button></div>
    ${f.error ? `<span class="form-err">${esc(f.error)}</span>` : ''}
  </div>`;
}

// One contact. at 'project': its role there (Key contact / stakeholder), make-key and remove-from-project;
// at 'client': delete for good. clientName: shown on a project page when they belong to another client.
export function contactRow(c, ct, { at, clientName = '' }) {
  const e = c.ui.cnEdit;
  if (e && e.id === ct.id) return `<div class="cn-row is-editing" data-key="cn${ct.id}">${contactForm(e, 'edit')}</div>`;
  const key = at === 'project' && ct.project_role === 'key';
  const where = [ct.role, clientName].filter(Boolean).map(esc).join(' · ');
  const pref = ct.preferred_method ? `<span class="cn-pref" title="Preferred way to reach them">prefers ${esc(methodLabel(ct.preferred_method))}</span>` : '';
  const reachHTML = reach(ct);
  return `<div class="cn-row${key ? ' is-key' : ''}" data-key="cn${ct.id}">
    <span class="cn-av" aria-hidden="true">${esc(initials(ct.name))}</span>
    <div class="cn-body">
      <span class="cn-name"><button class="cn-name__open" data-action="openContact" data-id="${ct.id}" title="Open ${esc(ct.name)}'s page"><b>${esc(ct.name)}</b></button>${key ? '<span class="cn-key">Key contact</span>' : ''}</span>
      ${where ? `<span class="cn-role">${where}</span>` : ''}
      ${reachHTML || pref ? `<span class="cn-meta">${reachHTML}${pref}</span>` : ''}
      ${ct.notes ? `<span class="cn-notes">${esc(ct.notes)}</span>` : ''}
    </div>
    <div class="cn-acts">
      ${at === 'project' ? `<button class="btn btn--ghost btn--sm" data-action="cnKey" data-id="${ct.id}" data-v="${key ? 'stakeholder' : 'key'}" title="${key ? 'Make them a stakeholder' : 'Make them the key contact'}">${key ? 'Unmark key' : 'Make key'}</button>` : ''}
      <button class="icon-btn icon-btn--ghost" data-action="cnEdit" data-id="${ct.id}" aria-label="Edit ${esc(ct.name)}" title="Edit">✎</button>
      ${at === 'project'
        ? `<button class="icon-btn icon-btn--ghost" data-action="cnUnlink" data-id="${ct.id}" aria-label="Take ${esc(ct.name)} off this project" title="Take off this project">✕</button>`
        : `<button class="icon-btn icon-btn--ghost" data-action="cnDelete" data-id="${ct.id}" aria-label="Delete ${esc(ct.name)}" title="Delete contact">✕</button>`}
    </div>
  </div>`;
}

// The Contacts card. at 'client' (id = client id) lists the client's contacts; at 'project' (id = project id)
// the project's key contact and stakeholders, plus a picker to link a contact that's already saved.
export function contactsCard(c, { at, id, contacts, clientId = null, clients = [] }) {
  const n = c.ui.cnNew && c.ui.cnNew.at === at && c.ui.cnNew.id === id ? c.ui.cnNew : null;
  const clientName = cid => (cid && cid !== clientId ? ((clients.find(x => x.id === cid) || {}).name || '') : '');
  const rows = contacts.map(ct => contactRow(c, ct, { at, clientName: at === 'project' ? clientName(ct.client_id) : '' })).join('');
  let picker = '';
  if (at === 'project') {
    const linked = new Set(contacts.map(x => x.id));
    const free = (c.d.contacts || []).filter(x => !linked.has(x.id));
    const mine = free.filter(x => clientId && x.client_id === clientId), others = free.filter(x => !(clientId && x.client_id === clientId));
    const opt = x => `<option value="${x.id}">${esc(x.name)}${x.role ? ' · ' + esc(x.role) : ''}${!mine.includes(x) && x.client_id ? ' (' + esc(clientName(x.client_id) || 'other client') + ')' : ''}</option>`;
    const own = clients.find(x => x.id === clientId);
    picker = free.length ? `<select class="input cn-pick" data-cl-change="cnLinkPick" aria-label="Add a saved contact to this project">
        <option value="">Add someone already saved…</option>
        ${mine.length ? `<optgroup label="${esc(own ? own.name : 'This client')}">${mine.map(opt).join('')}</optgroup>` : ''}
        ${others.length ? `<optgroup label="${mine.length ? 'Other contacts' : 'Contacts'}">${others.map(opt).join('')}</optgroup>` : ''}
      </select>` : '';
  }
  const empty = at === 'project'
    ? 'Who to talk to on this project: the key contact and anyone else with a stake in it.'
    : 'The people you deal with here: names, roles and how to reach them.';
  return `<section class="card card--pad crm-card" data-key="cn-card-${at}"><div class="crm-card__head"><span class="card__title">Contacts${contacts.length ? ` <span class="meta">${contacts.length}</span>` : ''}</span>
      ${n ? '' : `<button class="btn--text" data-action="cnNew" data-at="${at}" data-id="${id}">+ Add contact</button>`}</div>
    ${rows ? `<div class="cn-list">${rows}</div>` : n ? '' : `<span class="card__desc">${empty}</span>`}
    ${n ? contactForm(n, 'new', { project: at === 'project', title: at === 'project' && clientId ? 'New contact (saved under this project\'s client)' : 'New contact' }) : ''}
    ${picker}
  </section>`;
}

// One contact's page (#/contact/<id>): who they are, how to reach them, their client and projects, and their
// activity timeline - what they were part of, and the follow-ups logged on them.
export function contactPage(c) {
  const id = c.ui.contactId, ct = (c.d.contacts || []).find(x => x.id === id);
  if (!ct) {
    if (!c.d.clients.length && !c.d.contacts.length && !c.d.clientsMissing) return `<div class="page page--narrow" data-key="contact-loading"><span class="meta">Loading…</span></div>`;
    return `<div class="page page--narrow" data-key="contact-gone">${emptyState({ title: 'This contact is gone', body: 'They were deleted, maybe from another device.', label: 'All clients', attrs: 'data-action="go" data-view="clients"', key: 'cn-gone' })}</div>`;
  }
  const cl = ct.client_id ? clientById(ct.client_id, c.d) : null;
  const projects = (ct.project_ids || []).map(pid => projectById(pid, c.d)).filter(Boolean);
  const where = cl ? `<button class="ov-client__chip" data-action="openClient" data-id="${cl.id}" title="Open ${esc(cl.name)}"><span class="avatar cl-av cl-av--sm" style="background:${clientFill(cl)}" aria-hidden="true">${cl.logo ? '' : esc(cl.name.charAt(0).toUpperCase())}</span><b class="truncate">${esc(cl.name)}</b></button>` : '';
  const projs = projects.length ? `<section class="card card--pad crm-card" data-key="cn-projects"><div class="crm-card__head"><span class="card__title">Projects <span class="meta">${projects.length}</span></span></div>
    <div class="cl-projs">${projects.map(p => `<button class="cl-proj" data-action="clOpenProject" data-id="${p.id}" data-key="cnp${p.id}"><span class="nav-proj__swatch" style="background:${projectColor(p)}"></span><span class="cl-proj__name truncate">${esc(p.name)}</span></button>`).join('')}</div></section>` : '';
  return `<div class="page page--wide cl-page" data-key="contact-${ct.id}">
    <div class="cl-head" data-key="cn-head"><span class="cn-av cn-av--lg" aria-hidden="true">${esc(initials(ct.name))}</span>
      <div class="cl-head__text"><span class="cl-head__name">${esc(ct.name)}</span>
        <span class="cl-row__meta">${ct.role ? `<span>${esc(ct.role)}</span>` : ''}${where}</span></div>
      <button class="btn btn--ghost btn--sm cl-head__back" data-action="${cl ? 'openClient' : 'go'}" ${cl ? `data-id="${cl.id}"` : 'data-view="clients"'}>${cl ? esc(cl.name) : 'All clients'}</button></div>
    <div class="crm-grid">
      <div class="crm-col">${activityCard(c, { at: 'contact', id: ct.id })}</div>
      <div class="crm-col"><section class="card card--pad crm-card" data-key="cn-details"><div class="crm-card__head"><span class="card__title">Details</span></div>
        <div class="cn-list">${contactRow(c, ct, { at: 'contact' })}</div></section>${projs}</div>
    </div>
  </div>`;
}
