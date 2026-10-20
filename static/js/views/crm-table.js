// The Projects page's Table and Board views (state, filters and actions: ../crm-table.js). The page
// (projects.js) draws the view switch and calls viewSwitch / tableView / boardView.

import { esc, icon } from '../ui.js';
import { agoShort } from '../format.js';
import { projectFill, projectColor } from '../data.js';
import { dueBadge, fmtDate, PRIORITIES, HEALTHS } from './overview.js';
import { crmt, COLUMNS, STAGES, RANGES, NO_CLIENT, money, visibleRows, filterCount, allTags, allClients, stageTotals, stageInfo, serverHasStages } from '../crm-table.js';

const VIEWS = [['cards', 'Cards'], ['table', 'Table'], ['board', 'Board']];

export const viewSwitch = st => `<div class="chip-row crmt-views" role="radiogroup" aria-label="Projects view" data-noswipe>${VIEWS.map(([k, l]) =>
  `<button class="chip${st.view === k ? ' is-on' : ''}" role="radio" aria-checked="${st.view === k}" data-action="ctView" data-v="${k}" data-key="ctv-${k}">${l}</button>`).join('')}</div>`;

const opt = (v, label, cur) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(label)}</option>`;
const select = (k, label, options, cur) => `<label class="crmt-f"><span>${label}</span><select class="input" data-ct-f="${k}" aria-label="${label}">${options.map(([v, l]) => opt(v, l, cur)).join('')}</select></label>`;

function filterPanel(c, st) {
  const f = st.f, d = c.d;
  const clients = allClients(d), tags = allTags(d);
  return `<div class="card card--pad crmt-panel" data-key="ct-filters">
    <div class="crmt-grid">
      ${select('category', 'Category', [['', 'Work and personal'], ['work', 'Work'], ['personal', 'Personal']], f.category)}
      ${select('stage', 'Stage', [['', 'Any stage'], ...STAGES.map(s => [s[0], s[1]])], f.stage)}
      ${select('priority', 'Priority', [['', 'Any priority'], ...PRIORITIES.map(s => [s[0], s[1]])], f.priority)}
      ${select('health', 'Health', [['', 'Any health'], ...HEALTHS.map(s => [s[0], s[1]])], f.health)}
      ${select('client', 'Client', [['', 'Any client'], [NO_CLIENT, 'No client'], ...clients.map(x => [x, x])], f.client)}
      ${select('tag', 'Tag', [['', 'Any tag'], ...tags.map(x => [x, x])], f.tag)}
      ${select('range', 'Due', RANGES, f.range)}
      ${f.range === 'custom' ? `<label class="crmt-f"><span>Due from</span><input class="input" type="date" data-ct-f="from" value="${esc(f.from)}"></label>
        <label class="crmt-f"><span>Due to</span><input class="input" type="date" data-ct-f="to" value="${esc(f.to)}"></label>` : ''}
    </div>
    <div class="crmt-panel__foot">
      <button class="chip${f.overdue ? ' is-on' : ''}" role="switch" aria-checked="${!!f.overdue}" data-action="ctFilter" data-ct-f="overdue" data-toggle="1" data-key="ct-overdue">Overdue only</button>
      <button class="btn btn--sm" data-action="ctClear">Clear filters</button>
    </div>
  </div>`;
}

function columnPanel(st) {
  return `<div class="card card--pad crmt-panel" data-key="ct-cols">
    <span class="menu__label">SHOW COLUMNS</span>
    <div class="crmt-cols">${COLUMNS.map(col => {
      const on = st.cols.includes(col.k);
      return `<button class="chip${on ? ' is-on' : ''}" role="switch" aria-checked="${on}" ${col.fixed ? 'disabled' : `data-action="ctCol" data-k="${col.k}"`} data-key="ctc-${col.k}">${col.label}</button>`;
    }).join('')}</div>
    <div class="crmt-panel__foot"><button class="btn btn--sm" data-action="ctColsReset">Reset columns</button></div>
  </div>`;
}

function savedPanel(st) {
  return `<div class="card card--pad crmt-panel" data-key="ct-saved">
    <span class="menu__label">SAVED VIEWS · THIS DEVICE</span>
    ${st.saved.length ? `<div class="crmt-saved">${st.saved.map(v => `<div class="crmt-saved__row">
      <button class="crmt-saved__use${st.active === v.id ? ' is-on' : ''}" data-action="ctApplyView" data-id="${esc(v.id)}"><span class="truncate">${esc(v.name)}</span><span class="crmt-saved__kind">${v.view === 'board' ? 'Board' : 'Table'}</span></button>
      <button class="crmt-saved__x" data-action="ctDeleteView" data-id="${esc(v.id)}" aria-label="Delete view ${esc(v.name)}">✕</button></div>`).join('')}</div>`
      : '<span class="prow__empty">No saved views yet.</span>'}
    <div class="crmt-save">
      <input class="input" id="ctName" type="text" maxlength="60" placeholder="Name the current filters and columns" value="${esc(st.saveName)}" data-input="ct.name" data-enter="ctSaveView" aria-label="Saved view name">
      <button class="btn btn--sm" type="button" data-action="ctSaveView">Save view</button>
    </div>
  </div>`;
}

const stale = c => (serverHasStages(c.d) ? '' : '<div class="crmt-note">Restart the Den to move projects between stages. Until then stages are worked out from each project\'s status.</div>');

function toolbar(c, st, n, total) {
  const fn = filterCount(st.f);
  const btn = (panel, ic, label, extra = '') => `<button class="toolbar__btn${st.panel === panel ? ' is-on' : ''}" data-action="ctPanel" data-p="${panel}" aria-expanded="${st.panel === panel}">${icon(ic, 16)}${label}${extra}</button>`;
  return `<div class="crmt-bar" data-noswipe>
    <div class="search crmt-search">${icon('search', 16, { strokeWidth: 1.8 })}<input id="ctSearch" type="search" placeholder="Search name, client, tag, owner" value="${esc(st.q)}" data-input="ct.q" aria-label="Search projects"></div>
    <div class="crmt-tools">
      ${btn('filters', 'filter', 'Filters', fn ? `<span class="filter-badge">${fn}</span>` : '')}
      ${st.view === 'table' ? btn('cols', 'sort', 'Columns') : ''}
      ${btn('saved', 'filter', 'Views')}
      <button class="toolbar__btn" data-action="ctExport" title="Download the rows shown as a CSV file">${icon('download', 16)}CSV</button>
      <button class="toolbar__add" data-action="openSheet" data-sheet="addProject"><span>+</span>New project</button>
    </div>
    <span class="crmt-count">${n === total ? `${total} project${total === 1 ? '' : 's'}` : `${n} of ${total}`}</span>
  </div>
  ${stale(c)}
  ${st.panel === 'filters' ? filterPanel(c, st) : st.panel === 'cols' && st.view === 'table' ? columnPanel(st) : st.panel === 'saved' ? savedPanel(st) : ''}`;
}

const empty = (c, total) => (total
  ? `<div class="empty"><span>No projects match.</span><button class="btn btn--sm" data-action="ctClear">Clear filters</button></div>`
  : `<div class="empty"><span>No projects yet.</span><button class="btn btn--sm" data-action="openSheet" data-sheet="addProject">Create your first project</button></div>`);

const chipOf = (list, k) => { const x = list.find(y => y[0] === k); return x ? `<span class="crmt-pill" style="--c:${x[2]}">${x[1]}</span>` : ''; };
const avatar = p => `<span class="avatar crmt-av" style="background:${projectFill(p)};opacity:${p.active ? 1 : .55}">${p.image ? '' : esc(p.name.charAt(0).toUpperCase())}</span>`;

function cellHTML(col, r) {
  const p = r.p;
  switch (col.k) {
    case 'name': return `<button class="crmt-name" data-action="openProject" data-id="${p.id}">${avatar(p)}<span class="truncate">${esc(p.name)}</span></button>`;
    case 'client': return r.client ? esc(r.client) : '<span class="crmt-dim">—</span>';
    case 'stage': return `<span class="crmt-pill" style="--c:${stageInfo(r.stage)[2]}">${stageInfo(r.stage)[1]}</span>`;
    case 'priority': return chipOf(PRIORITIES, p.priority);
    case 'health': return chipOf(HEALTHS, p.health);
    case 'start': return p.start_date ? esc(fmtDate(p.start_date)) : '<span class="crmt-dim">—</span>';
    case 'due': return p.due_date ? `<span class="crmt-due">${esc(fmtDate(p.due_date))}${p.schedule && p.schedule.due_state === 'overdue' ? dueBadge(p, { compact: true }) : ''}</span>` : '<span class="crmt-dim">—</span>';
    case 'value': return p.contract_value == null ? '<span class="crmt-dim">—</span>' : esc(money(p.contract_value, p.currency));
    case 'progress': {
      const n = p.progress && p.progress.percent;
      return n == null ? '<span class="crmt-dim">—</span>' : `<span class="crmt-prog"><span class="crmt-prog__bar"><i style="width:${n}%"></i></span>${n}%</span>`;
    }
    case 'activity': return r.activity ? esc(agoShort(new Date(r.activity).toISOString())) : '<span class="crmt-dim">—</span>';
    default: return '';
  }
}

export function tableView(c) {
  const st = crmt(), total = c.d.projects.filter(p => !p.is_general).length;
  const rows = visibleRows(c.d, st);
  const cols = COLUMNS.filter(col => st.cols.includes(col.k));
  const head = cols.map(col => {
    const on = st.sort.k === col.k;
    return `<th class="${col.num ? 'is-num' : ''}${col.fixed ? ' is-sticky' : ''}" scope="col" aria-sort="${on ? (st.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}">
      <button class="crmt-th${on ? ' is-on' : ''}" data-action="ctSort" data-k="${col.k}">${col.label}<span class="crmt-th__arrow" aria-hidden="true">${on ? (st.sort.dir === 'asc' ? '↑' : '↓') : ''}</span></button></th>`;
  }).join('');
  const body = rows.map(r => `<tr data-key="ctr${r.p.id}">${cols.map(col => `<td class="${col.num ? 'is-num' : ''}${col.fixed ? ' is-sticky' : ''}">${cellHTML(col, r)}</td>`).join('')}</tr>`).join('');
  return `<div class="page page--projects page--crmt" data-key="page-projects">
    ${viewSwitch(st)}
    ${toolbar(c, st, rows.length, total)}
    ${rows.length ? `<div class="card crmt-scroll" data-noswipe tabindex="0" role="region" aria-label="Projects table, scrolls sideways"><table class="crmt-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>` : empty(c, total)}
  </div>`;
}

function cardHTML(r) {
  const p = r.p, due = dueBadge(p);
  return `<div class="crmt-card" draggable="true" data-drag-project="${p.id}" data-key="bc${p.id}">
    <button class="crmt-card__main" data-action="openProject" data-id="${p.id}">
      <span class="crmt-card__top">${avatar(p)}<span class="crmt-card__name truncate">${esc(p.name)}</span></span>
      ${r.client ? `<span class="crmt-card__client truncate">${esc(r.client)}</span>` : ''}
      <span class="crmt-card__meta">${chipOf(PRIORITIES, p.priority)}${chipOf(HEALTHS, p.health)}${p.contract_value != null ? `<span class="crmt-card__val">${esc(money(p.contract_value, p.currency))}</span>` : ''}</span>
      ${due ? `<span class="prow__due">${due}</span>` : ''}
    </button>
    <label class="crmt-move"><span class="sr-only">Move ${esc(p.name)} to</span><select data-ct-move data-id="${p.id}" aria-label="Move ${esc(p.name)} to a stage">${STAGES.map(s => opt(s[0], 'Move to: ' + s[1], r.stage)).join('')}</select></label>
  </div>`;
}

export function boardView(c) {
  const st = crmt(), total = c.d.projects.filter(p => !p.is_general).length;
  const rows = visibleRows(c.d, st);
  const cols = STAGES.map(([k, label, color]) => {
    const list = rows.filter(r => r.stage === k);
    const totals = stageTotals(list);
    return `<section class="crmt-col" data-drop-stage="${k}" data-key="bcol-${k}" aria-label="${label}">
      <header class="crmt-col__head"><span class="dot" style="--c:${color}"></span><span class="crmt-col__name">${label}</span><span class="crmt-col__n">${list.length}</span></header>
      <div class="crmt-col__sum">${totals.length ? totals.map(t => esc(money(t.total, t.currency))).join(' · ') : '<span class="crmt-dim">No value</span>'}</div>
      <div class="crmt-col__cards">${list.map(cardHTML).join('') || '<span class="crmt-col__empty">Drop a project here</span>'}</div>
    </section>`;
  }).join('');
  return `<div class="page page--projects page--crmt" data-key="page-projects">
    ${viewSwitch(st)}
    ${toolbar(c, st, rows.length, total)}
    ${total ? `<div class="crmt-board" data-noswipe tabindex="0" role="region" aria-label="Pipeline board, swipe sideways">${cols}</div>` : empty(c, 0)}
  </div>`;
}
