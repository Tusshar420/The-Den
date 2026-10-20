// Project Overview: the CRM record - description, priority, health, dates, contract, owner, tags and
// links - each editable in place, plus progress (from phases and to-dos) and days left / overdue.
// The record comes from GET /projects/{id} (c.d.records); edits go through crm-ui.js.

import { esc, icon } from '../ui.js';
import { ago, plural } from '../format.js';
import { PROJECT_STATUSES, crmOnly, NO_FOLDER_HINT, projectColor, projectFill, clientById, clientFill } from '../data.js';
import { contactsCard } from './contacts.js';
import { activityCard } from './crm-activity.js';

// Preset colours for a project's tile (any other colour: the custom picker).
const TILE_COLORS = ['#e8825a', '#e8c24f', '#4fbf8a', '#4f9de8', '#b583e8', '#e86a8f', '#5fc4c9', '#9aa3b2'];

// [key, label, colour] - same keys as crm.PRIORITIES / crm.HEALTHS on the server.
export const PRIORITIES = [['low', 'Low', 'var(--text-meta)'], ['medium', 'Medium', 'var(--queued)'], ['high', 'High', 'var(--wait)'], ['critical', 'Critical', 'var(--err)']];
export const HEALTHS = [['on_track', 'On track', 'var(--ok, #5fd39a)'], ['at_risk', 'At risk', 'var(--wait)'], ['off_track', 'Off track', 'var(--err)']];
export const BILLING = [['fixed', 'Fixed price'], ['hourly', 'Hourly'], ['retainer', 'Retainer']];
export const LINK_KINDS = ['Repo', 'Live site', 'Staging', 'Docs', 'Design'];
const pick = (list, k) => list.find(x => x[0] === k);

// '2026-10-12' → '12 Oct' (this year) or '12 Oct 2027'. Read as a local date, never shifted by time zone.
const localDate = iso => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
export function fmtDate(iso, long = false) {
  if (!iso) return '';
  const dt = localDate(iso), sameYear = dt.getFullYear() === new Date().getFullYear();
  return dt.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', ...(long || !sameYear ? { year: 'numeric' } : {}), ...(long ? { weekday: 'short' } : {}) });
}

export function fmtMoney(v, cur) {
  if (v == null) return '';
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD', maximumFractionDigits: v % 1 ? 2 : 0 }).format(v); }
  catch { return `${cur || ''} ${v}`.trim(); }
}

// The due date and its badge for the projects list ("Due 30 Sep  Overdue · 3 days"), or one short chip
// for the sidebar (compact: "3d late", "Due in 2d", "12 Oct"). Empty with no due date or once done.
// p.schedule comes from the server (crm.schedule) - an older server without it just shows nothing.
export function dueBadge(p, { compact = false } = {}) {
  const s = p && p.schedule;
  if (!s || !s.due_state || s.due_state === 'done') return '';
  const n = s.days_left, when = esc(fmtDate(p.due_date)), title = esc(`Due ${fmtDate(p.due_date, true)}`);
  const chip = (cls, text) => `<span class="crm-due crm-due--${cls}" title="${title}">${text}</span>`;
  const soon = n === 0 ? 'Due today' : n === 1 ? 'Due tomorrow' : compact ? `Due in ${n}d` : `Due in ${n} days`;
  if (compact) return s.due_state === 'overdue' ? chip('over', `${-n}d late`) : s.due_state === 'due_soon' ? chip('soon', soon) : chip('later', when);
  const date = chip('later', `Due ${when}`);
  return s.due_state === 'overdue' ? `${date}${chip('over', `Overdue · ${plural(-n, 'day')}`)}`
    : s.due_state === 'due_soon' ? `${date}${chip('soon', soon)}` : date;
}

// ---------------------------------------------------------------- markdown
// The description's markdown as HTML: headings, lists, quotes, code, **bold**, *italic*, [links](https://…).
// Everything is escaped first, so only these tags can come out of it.
function inline(s) {
  return esc(s)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(\S(?:.*?\S)?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*(\S(?:.*?\S)?)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/(^|\W)_(\S(?:.*?\S)?)_(?!\w)/g, '$1<em>$2</em>');
}
export function mdHTML(md) {
  let html = '', list = null, para = [], code = null;
  const flushPara = () => { if (para.length) html += `<p>${para.map(inline).join('<br>')}</p>`; para = []; };
  const closeList = () => { if (list) html += `</${list}>`; list = null; };
  for (const line of (md || '').split('\n')) {
    if (code !== null) {
      if (/^```/.test(line)) { html += `<pre><code>${esc(code.join('\n'))}</code></pre>`; code = null; } else code.push(line);
      continue;
    }
    if (/^```/.test(line)) { flushPara(); closeList(); code = []; continue; }
    const h = line.match(/^(#{1,3})\s+(.*)$/), ul = line.match(/^\s*[-*]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/), q = line.match(/^>\s?(.*)$/);
    if (h) { flushPara(); closeList(); html += `<h${h[1].length + 2}>${inline(h[2])}</h${h[1].length + 2}>`; }
    else if (ul || ol) {
      flushPara();
      const want = ul ? 'ul' : 'ol';
      if (list !== want) { closeList(); html += `<${want}>`; list = want; }
      html += `<li>${inline((ul || ol)[1])}</li>`;
    } else if (q) { flushPara(); closeList(); html += `<blockquote>${inline(q[1])}</blockquote>`; }
    else if (!line.trim()) { flushPara(); closeList(); }
    else { closeList(); para.push(line); }
  }
  if (code !== null) html += `<pre><code>${esc(code.join('\n'))}</code></pre>`;
  flushPara(); closeList();
  return html;
}

// ---------------------------------------------------------------- pieces
const pencil = `<span class="crm-f__pen" aria-hidden="true">${icon('edit', 13)}</span>`;
const placeholder = s => `<span class="crm-f__ph">${s}</span>`;
const editing = (c, p, k) => { const e = c.ui.ovEdit; return e && e.pid === p.id && e.k === k ? e : null; };

function saveRow(e, extra = '') {
  return `<div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="ovSave"${e.saving ? ' disabled' : ''}>${e.saving ? 'Saving…' : 'Save'}</button>
    <button class="btn btn--ghost btn--sm" data-action="ovCancel">Cancel</button>${extra}</div>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}`;
}

// One label / value row that turns into an editor when tapped.
function field(c, p, k, label, shown, editor) {
  const e = editing(c, p, k);
  return `<div class="crm-f${e ? ' is-editing' : ''}" data-key="ovf-${k}"><span class="crm-f__l">${label}</span>
    ${e ? `<div class="crm-ed">${editor(e)}</div>` : `<button class="crm-f__v" data-action="ovEdit" data-k="${k}" aria-label="Edit ${esc(label.toLowerCase())}">${shown}${pencil}</button>`}</div>`;
}

const textEditor = (k, type = 'text', attrs = '') => e =>
  `<input id="ov-${k}" class="input" type="${type}" value="${esc(e.v)}" data-input="ov.v" data-enter="ovSave" autocomplete="off" ${attrs}>${saveRow(e, `<button class="btn btn--ghost btn--sm" data-action="ovClear">Clear</button>`)}`;

// Tap to set: priority, health, billing type.
function chips(p, k, options, cur, clearable = false) {
  return `<div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${options.map(([v, l, col]) =>
    `<button class="segmented__opt${cur === v ? ' is-on' : ''}" role="radio" aria-checked="${cur === v}" data-action="ovSet" data-k="${k}" data-v="${v}"${col ? ` style="--c:${col}"` : ''}>${col ? `<span class="dot" style="--c:${col};width:7px;height:7px"></span>` : ''}${l}</button>`).join('')}${clearable && cur ? `<button class="segmented__opt" data-action="ovSet" data-k="${k}" data-v="" title="Clear">—</button>` : ''}</div>`;
}

const tildify = s => (s || '').replace(/^\/Users\/[^/]+/, '~');

// Name, category, stage and the folder - the folder can only be linked here, never changed or removed.
// Colour and picture for the project's tile: shown in the sidebar, lists, chips and the header.
function look(p, r) {
  const cur = r.color ? r.color.toLowerCase() : null;
  const tile = `<span class="avatar crm-look__tile" style="background:${projectFill({ ...p, ...r })}">${r.image ? '' : esc((r.name || '?').charAt(0).toUpperCase())}</span>`;
  const sw = TILE_COLORS.map(col => `<button class="acct-swatch${cur === col ? ' is-on' : ''}" style="--c:${col}" data-action="ovColor" data-v="${col}" title="${col}" aria-label="Use ${col}" aria-pressed="${cur === col}"></button>`).join('');
  const custom = cur && !TILE_COLORS.includes(cur);
  const bg = r.logo_bg ? r.logo_bg.toLowerCase() : null;
  const bgCustom = bg && !TILE_COLORS.includes(bg);
  return `<div class="crm-look">${tile}<div class="crm-look__ctl">
    <div class="crm-look__row">${sw}<label class="acct-swatch acct-swatch--custom${custom ? ' is-on' : ''}"${custom ? ` style="--c:${esc(cur)};background:var(--c)"` : ''} title="Pick any colour"><input type="color" value="${esc(cur || '#9aa3b2')}" data-ov-color aria-label="Custom colour"></label>
      ${cur ? `<button class="btn btn--ghost btn--sm" data-action="ovColor" data-v="">Automatic</button>` : ''}</div>
    <div class="crm-look__row"><span class="meta">Logo background</span>${TILE_COLORS.map(col => `<button class="acct-swatch${bg === col ? ' is-on' : ''}" style="--c:${col}" data-action="ovLogoBg" data-v="${col}" title="${col}" aria-label="Logo background ${col}" aria-pressed="${bg === col}"></button>`).join('')}<label class="acct-swatch acct-swatch--custom${bgCustom ? ' is-on' : ''}"${bgCustom ? ` style="--c:${esc(bg)};background:var(--c)"` : ''} title="Pick any colour"><input type="color" value="${esc(bg || cur || '#9aa3b2')}" data-ov-logobg aria-label="Custom logo background"></label>
      ${bg ? `<button class="btn btn--ghost btn--sm" data-action="ovLogoBg" data-v="">Same as project</button>` : ''}</div>
    <div class="crm-look__row"><button class="btn btn--sm" data-action="ovImagePick">${r.image ? 'Change picture' : 'Add picture'}</button>
      ${r.image ? `<button class="btn btn--ghost btn--sm" data-action="ovImageDel">Remove picture</button>` : ''}
      <input type="file" accept="image/*" hidden data-ov-image></div></div></div>`;
}

function basics(c, p, r) {
  const g = (k, label, shown, ed) => field(c, p, k, label, shown, ed);
  const fe = editing(c, p, 'folder');
  const folder = !crmOnly(r)
    && !fe ? `<span class="crm-f__v crm-f__v--static mono" title="${esc(r.path)}">${esc(tildify(r.path))}</span>${r.is_general ? '' : '<button class="btn btn--sm" data-action="ovFolderChange">Change</button>'}`
    : fe ? `<div class="crm-ed"><input id="ov-folder" class="input input--mono" value="${esc(fe.v)}" data-input="ov.v" data-enter="ovSave" placeholder="~/code/folder" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Folder">
        <span class="np-f__hint">A folder on this Mac. Octopus runs this project's tasks in it.</span>
        <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="ovSave"${fe.saving ? ' disabled' : ''}>${fe.saving ? 'Saving…' : crmOnly(r) ? 'Link folder' : 'Save folder'}</button><button class="btn btn--ghost btn--sm" data-action="ovCancel">Cancel</button></div>
        ${fe.error ? `<span class="form-err">${esc(fe.error)}</span>` : ''}</div>`
    : `<span class="crm-f__v crm-f__v--static"><span class="crm-badge">CRM only</span><button class="btn btn--sm" data-action="ovLinkStart">Link a folder</button></span>`;
  return `<section class="card card--pad crm-card" data-key="ov-basics"><div class="crm-card__head"><span class="card__title">Project</span></div>
    <div class="crm-fields">
      ${g('name', 'Name', esc(r.name), e => `<input id="ov-name" class="input" value="${esc(e.v)}" data-input="ov.v" data-enter="ovSave" maxlength="120" autocomplete="off" aria-label="Name">${saveRow(e)}`)}
      ${r.contacts !== undefined ? `<div class="crm-f${c.ui.ovClient && c.ui.ovClient.pid === p.id ? ' is-editing' : ''}" data-key="ovf-client"><span class="crm-f__l">Client</span>${clientField(c, p, r)}</div>` : ''}
      <div class="crm-f" data-key="ovf-look"><span class="crm-f__l">Look</span>${look(p, r)}</div>
      <div class="crm-f" data-key="ovf-category"><span class="crm-f__l">Category</span>${chips(p, 'category', [['work', 'Work'], ['personal', 'Personal']], r.category)}</div>
      <div class="crm-f" data-key="ovf-status"><span class="crm-f__l">Stage</span>${chips(p, 'status', PROJECT_STATUSES, r.status)}</div>
      <div class="crm-f" data-key="ovf-folder"><span class="crm-f__l">Folder</span>${folder}</div>
    </div>
    ${crmOnly(r) && !fe ? `<span class="meta">${NO_FOLDER_HINT}.</span>` : ''}</section>`;
}

// The client the project is for (clients.py): a chip that opens the client's page, or the picker -
// any saved client, none, or a new one made right here (ui.ovClient, clients-ui.js).
function clientField(c, p, r) {
  const o = c.ui.ovClient && c.ui.ovClient.pid === p.id ? c.ui.ovClient : null;
  if (o) {
    const list = c.d.clients || [];
    return `<div class="crm-ed"><select id="ov-client" class="input" data-input="cl.ovClient" aria-label="Client">
        <option value=""${o.v === '' ? ' selected' : ''}>No client</option>
        ${list.map(x => `<option value="${x.id}"${o.v === String(x.id) ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}
        <option value="new"${o.v === 'new' ? ' selected' : ''}>+ New client…</option></select>
      ${o.v === 'new' ? `<input id="ov-client-name" class="input" value="${esc(o.name)}" data-input="cl.ovClientName" data-enter="ovClientSave" maxlength="120" placeholder="The new client's name" autocomplete="off" aria-label="New client name">` : ''}
      <div class="crm-ed__acts"><button class="btn btn--primary btn--sm" data-action="ovClientSave"${o.saving ? ' disabled' : ''}>${o.saving ? 'Saving…' : o.v === 'new' ? 'Create and pick' : 'Save'}</button>
        <button class="btn btn--ghost btn--sm" data-action="ovClientCancel">Cancel</button></div>
      ${o.error ? `<span class="form-err">${esc(o.error)}</span>` : ''}</div>`;
  }
  const cl = r.client || (r.client_id ? clientById(r.client_id, c.d) : null);
  if (!cl) return `<button class="crm-f__v" data-action="ovClientEdit" aria-label="Pick a client">${placeholder('Pick or add a client')}${pencil}</button>`;
  return `<span class="crm-f__v crm-f__v--static ov-client"><button class="ov-client__chip" data-action="openClient" data-id="${cl.id}" title="Open ${esc(cl.name)}">
      <span class="avatar cl-av cl-av--sm" style="background:${clientFill(cl)}" aria-hidden="true">${cl.logo ? '' : esc(cl.name.charAt(0).toUpperCase())}</span><b class="truncate">${esc(cl.name)}</b></button>
    <button class="btn btn--ghost btn--sm" data-action="ovClientEdit">Change</button></span>`;
}

// Archive and hide keep everything; delete is for good and needs the name typed.
function manage(c, p, r) {
  const d = c.ui.ovDel && c.ui.ovDel.pid === p.id ? c.ui.ovDel : null;
  const archived = r.status === 'archived';
  return `<section class="card card--pad crm-card crm-manage" data-key="ov-manage"><div class="crm-card__head"><span class="card__title">Keep or remove</span></div>
    <span class="card__desc">Archive or hide a project you're done with: nothing is lost and you can bring it back.</span>
    <div class="crm-ed__acts"><button class="btn btn--sm" data-action="ovArchive">${archived ? 'Unarchive' : 'Archive'}</button>
      <button class="btn btn--sm" data-action="hideProject">Hide</button>
      ${d ? '' : `<button class="btn btn--ghost btn--sm crm-manage__del" data-action="ovDelOpen">Delete…</button>`}</div>
    ${d ? `<div class="crm-del" data-key="ov-del"><span class="card__desc">This deletes <b>${esc(r.name)}</b> for good: its record, to-dos, phases, notes, documents and calendar. ${crmOnly(r) ? '' : 'Its folder on this Mac is not touched. '}A project Octopus has already worked on can't be deleted - archive it instead.</span>
      <input id="ov-del" class="input" value="${esc(d.text)}" data-input="ov.del" data-enter="ovDelete" placeholder="Type ${esc(r.name)} to confirm" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Type the project name to confirm">
      <div class="crm-ed__acts"><button class="btn btn--danger btn--sm" data-action="ovDelete"${d.text !== r.name || d.busy ? ' disabled' : ''}>${d.busy ? 'Deleting…' : 'Delete for good'}</button><button class="btn btn--ghost btn--sm" data-action="ovDelCancel">Cancel</button></div>
      ${d.error ? `<span class="form-err">${esc(d.error)}</span>` : ''}</div>` : ''}
  </section>`;
}

function highlights(r) {
  const pr = pick(PRIORITIES, r.priority) || PRIORITIES[1], he = pick(HEALTHS, r.health) || HEALTHS[0];
  const s = r.schedule || {}, g = r.progress || {};
  const due = !r.due_date ? ['Not set', '', ''] : s.due_state === 'done' ? [fmtDate(r.due_date), 'done', 'var(--ok, #5fd39a)']
    : s.due_state === 'overdue' ? [`${plural(-s.days_left, 'day')} overdue`, `was due ${fmtDate(r.due_date)}`, 'var(--err)']
    : s.days_left === 0 ? ['Today', fmtDate(r.due_date), 'var(--wait)']
    : [`${plural(s.days_left, 'day')} left`, fmtDate(r.due_date), s.due_state === 'due_soon' ? 'var(--wait)' : ''];
  const tile = (label, v, note, col) => `<div class="crm-hl__t"><span class="crm-hl__l">${label}</span><span class="crm-hl__v"${col ? ` style="color:${col}"` : ''}>${v}</span>${note ? `<span class="crm-hl__n">${esc(note)}</span>` : ''}</div>`;
  const billing = (pick(BILLING, r.billing_type) || [])[1];
  return `<div class="crm-hl" data-key="ov-hl">
    ${tile('PRIORITY', `<span class="dot" style="--c:${pr[2]};width:8px;height:8px"></span>${pr[1]}`, '')}
    ${tile('HEALTH', `<span class="dot" style="--c:${he[2]};width:8px;height:8px"></span>${he[1]}`, '')}
    ${tile('DUE', esc(due[0]), due[1], due[2])}
    ${tile('PROGRESS', g.percent == null ? '–' : g.percent + '%', g.percent == null ? 'nothing to measure yet' : `${g.todos_done}/${g.todos_total} to-dos`)}
    ${tile('VALUE', r.contract_value == null ? '–' : esc(fmtMoney(r.contract_value, r.currency)), billing || '')}
  </div>`;
}

function description(c, p, r) {
  const e = editing(c, p, 'description');
  const body = e
    ? `<textarea id="ov-description" class="input crm-desc__ta" rows="8" data-input="ov.v" data-autogrow="520" placeholder="What this project is, scope, notes for whoever picks it up… Markdown works: # headings, - lists, **bold**, [links](https://…)">${esc(e.v)}</textarea>${saveRow(e, `<button class="btn btn--ghost btn--sm" data-action="ovGenDesc"${e.generating ? ' disabled' : ''}>${e.generating ? 'Writing…' : '✨ Generate from project'}</button>`)}`
    : r.description ? `<div class="crm-md">${mdHTML(r.description)}</div>`
    : `<button class="crm-empty" data-action="ovEdit" data-k="description">Add a description: what it is, the scope, anything worth knowing.</button>
       <button class="btn btn--ghost btn--sm" data-action="ovGenDesc">✨ Generate from project</button>`;
  return `<section class="card card--pad crm-card" data-key="ov-desc"><div class="crm-card__head"><span class="card__title">Description</span>
    ${e || !r.description ? '' : `<button class="btn--text" data-action="ovEdit" data-k="description">Edit</button>`}</div>${body}</section>`;
}

function details(c, p, r) {
  const g = (k, label, shown, ed) => field(c, p, k, label, shown, ed);
  const billing = (pick(BILLING, r.billing_type) || [])[1];
  return `<section class="card card--pad crm-card" data-key="ov-details"><div class="crm-card__head"><span class="card__title">Details</span></div>
    <div class="crm-fields">
      <div class="crm-f" data-key="ovf-priority"><span class="crm-f__l">Priority</span>${chips(p, 'priority', PRIORITIES, r.priority)}</div>
      <div class="crm-f" data-key="ovf-health"><span class="crm-f__l">Health</span>${chips(p, 'health', HEALTHS, r.health)}</div>
      ${g('owner', 'Owner', r.owner ? esc(r.owner) : placeholder('Add owner'), textEditor('owner', 'text', 'maxlength="120" placeholder="Who is responsible"'))}
      <div class="crm-f" data-key="ovf-billing"><span class="crm-f__l">Billing</span>${chips(p, 'billing_type', BILLING, r.billing_type, true)}</div>
      ${g('contract', 'Contract value', r.contract_value == null ? placeholder('Add value') : `${esc(fmtMoney(r.contract_value, r.currency))}${billing ? ` <span class="meta">· ${esc(billing)}</span>` : ''}`,
        e => `<div class="crm-ed__money"><input id="ov-contract" class="input" type="number" min="0" step="0.01" inputmode="decimal" value="${esc(e.v)}" data-input="ov.v" data-enter="ovSave" placeholder="0" aria-label="Contract value">
          <input class="input crm-ed__cur" maxlength="3" value="${esc(e.v2)}" data-input="ov.v2" data-enter="ovSave" aria-label="Currency" placeholder="USD" autocapitalize="characters" autocomplete="off"></div>${saveRow(e, `<button class="btn btn--ghost btn--sm" data-action="ovClear">Clear</button>`)}`)}
    </div></section>`;
}

function schedule(c, p, r) {
  const s = r.schedule || {};
  const date = (k, label) => field(c, p, k, label, r[k] ? esc(fmtDate(r[k], true)) : placeholder('Set date'), textEditor(k, 'date'));
  let note = '', bar = '';
  if (s.due_state === 'overdue') note = `<span class="crm-sched__note crm-sched__note--over">${plural(-s.days_left, 'day')} overdue</span>`;
  else if (s.due_state === 'done') note = `<span class="crm-sched__note crm-sched__note--done">Done${r.completed_date ? ' · ' + esc(fmtDate(r.completed_date)) : ''}</span>`;
  else if (s.due_state) note = `<span class="crm-sched__note${s.due_state === 'due_soon' ? ' crm-sched__note--soon' : ''}">${s.days_left === 0 ? 'Due today' : plural(s.days_left, 'day') + ' left'}</span>`;
  if (r.start_date && r.due_date && s.due_state !== 'done') {
    const day = 86400000, a = localDate(r.start_date).getTime(), b = localDate(r.due_date).getTime(), now = new Date().setHours(0, 0, 0, 0);
    const pct = b > a ? Math.max(0, Math.min(100, Math.round((now - a) / (b - a) * 100))) : 100;
    bar = `<div class="crm-sched__bar"><div class="bar6"><div style="width:${pct}%;background:${s.due_state === 'overdue' ? 'var(--err)' : s.due_state === 'due_soon' ? 'var(--wait)' : 'var(--run)'}"></div></div>
      <span class="meta">${pct}% of the time used · ${plural(Math.round((b - a) / day), 'day')} planned</span></div>`;
  }
  return `<section class="card card--pad crm-card" data-key="ov-sched"><div class="crm-card__head"><span class="card__title">Schedule</span>${note}</div>
    ${bar}<div class="crm-fields">${date('start_date', 'Start')}${date('due_date', 'Due')}${date('completed_date', 'Completed')}</div></section>`;
}

function progressCard(r) {
  const g = r.progress;
  if (!g) return `<section class="card card--pad crm-card" data-key="ov-prog"><span class="card__title">Progress</span><span class="meta">Loading…</span></section>`;
  const pct = g.percent;
  return `<section class="card card--pad crm-card" data-key="ov-prog"><div class="crm-card__head"><span class="card__title">Progress</span><button class="btn--text" data-action="projectTab" data-tab="phases">Phases →</button></div>
    ${pct == null ? `<span class="card__desc">Nothing to measure yet. Add phases or backlog items and this fills in as they get done.</span>`
      : `<div class="crm-prog"><span class="crm-prog__pct">${pct}%</span><div class="bar6 crm-prog__bar"><div style="width:${pct}%;background:${pct === 100 ? 'var(--ok, #5fd39a)' : 'var(--run)'}"></div></div></div>
      <div class="crm-prog__rows">
        ${g.phases_total ? `<span><b>${g.phases_done}</b> of ${plural(g.phases_total, 'phase')} done</span>` : ''}
        <span><b>${g.todos_done}</b> of ${plural(g.todos_total, 'to-do')} done</span></div>`}
  </section>`;
}

function tags(c, p, r) {
  const list = r.tags || [];
  return `<section class="card card--pad crm-card" data-key="ov-tags"><div class="crm-card__head"><span class="card__title">Tags</span></div>
    ${list.length ? `<div class="allow-chips">${list.map((t, i) => `<span class="allow-chip">${esc(t)}<button data-action="ovTagDel" data-i="${i}" aria-label="Remove tag ${esc(t)}">✕</button></span>`).join('')}</div>` : ''}
    <div class="crm-add"><input id="ov-tag" class="input" maxlength="200" placeholder="Add tags, separated by commas" value="${esc(c.ui.ovTag || '')}" data-input="ov.tag" data-enter="ovTagAdd" autocomplete="off" aria-label="Add tag">
      <button class="btn btn--ghost btn--38" data-action="ovTagAdd"${(c.ui.ovTag || '').trim() ? '' : ' disabled'}>Add</button></div>
  </section>`;
}

function links(c, p, r) {
  const list = r.links || [];
  const nl = c.ui.ovLink || { label: 'Repo', url: '' };
  const short = u => u.replace(/^https?:\/\//, '').replace(/\/$/, '');
  return `<section class="card card--pad crm-card" data-key="ov-links"><div class="crm-card__head"><span class="card__title">Links</span></div>
    ${list.length ? `<div class="crm-links">${list.map((x, i) => `<div class="crm-link" data-key="ovl${i}">
        <a class="crm-link__a" href="${esc(x.url)}" target="_blank" rel="noopener noreferrer"><b>${esc(x.label)}</b><span class="meta truncate">${esc(short(x.url))}</span></a>
        <button class="icon-btn icon-btn--ghost" data-action="ovLinkDel" data-i="${i}" aria-label="Remove link ${esc(x.label)}">✕</button></div>`).join('')}</div>`
      : `<span class="card__desc">Repo, live site, staging, docs, designs: keep them one tap away.</span>`}
    <div class="chip-row crm-link__kinds" data-noswipe>${LINK_KINDS.map(k => `<button class="chip chip--sm${nl.label === k ? ' is-on' : ''}" data-action="ovLinkKind" data-v="${esc(k)}">${k}</button>`).join('')}</div>
    <div class="crm-add crm-add--link"><input class="input crm-add__label" maxlength="60" placeholder="Label" value="${esc(nl.label)}" data-input="ovLink.label" data-enter="ovLinkAdd" autocomplete="off" aria-label="Link label">
      <input id="ov-link-url" class="input" type="url" inputmode="url" placeholder="https://…" value="${esc(nl.url)}" data-input="ovLink.url" data-enter="ovLinkAdd" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Link URL">
      <button class="btn btn--ghost btn--38" data-action="ovLinkAdd"${nl.url.trim() ? '' : ' disabled'}>Add</button></div>
    ${nl.error ? `<span class="form-err">${esc(nl.error)}</span>` : ''}
  </section>`;
}

export function overviewTab(c, p) {
  const r = { ...p, ...(c.d.records[p.id] || {}) };
  // The page can be newer than the server it talks to (the service picks up new code on restart).
  if (r.priority === undefined) {
    return `<div class="section" data-key="tab-overview"><div class="card card--pad"><span class="card__title">Project records are almost ready</span>
      <span class="card__desc">Restart the Den from the dashboard to start keeping a description, dates, contract value, owner, tags and links for each project.</span></div></div>`;
  }
  return `<div class="crm" data-key="tab-overview">
    ${highlights(r)}
    <div class="crm-grid">
      <div class="crm-col">${basics(c, p, r)}${description(c, p, r)}${details(c, p, r)}${links(c, p, r)}</div>
      <div class="crm-col">${progressCard(r)}${schedule(c, p, r)}
        ${r.contacts !== undefined ? contactsCard(c, { at: 'project', id: p.id, contacts: r.contacts, clientId: r.client_id || null, clients: c.d.clients || [] }) : ''}
        ${tags(c, p, r)}${manage(c, p, r)}
        ${r.updated_at ? `<span class="meta crm-stamp">Record updated ${esc(ago(r.updated_at, c.now))}</span>` : ''}</div>
    </div>
    ${r.contacts !== undefined ? activityCard(c, { at: 'project', id: p.id }) : ''}
  </div>`;
}
