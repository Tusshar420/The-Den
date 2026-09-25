// Projects: quick filter chips, sort / filter / search toolbar, expandable rows
// listing each project's tasks as titles only. General is always pinned first.

import { QUICK_FILTERS, statusOf, taskName } from '../format.js';
import { projectColor, queuedTasks, SCOPES, projectStatus } from '../data.js';
import { esc, icon, scopeSwitch, bulkBar } from '../ui.js';
import { octopusSVG } from '../octopus.js';

const ACTIVE = ['approval', 'running', 'rate_limited', 'needs_review', 'error'];
const SORTS = [['activity', 'Most active'], ['recent', 'Recently used'], ['queued', 'Most queued'], ['name', 'Name A–Z'], ['newest', 'Newest on top']];
const chev = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5l4 4 4-4"/></svg>';

export function projectRows(c) {
  const { d, w, ui } = c;
  const pl = ui.projectsList;
  const qOrder = new Map(queuedTasks(d).map((t, i) => [t.id, i]));
  const cur = QUICK_FILTERS.find(f => f.key === pl.quick) || QUICK_FILTERS[0];
  const q = pl.q.trim().toLowerCase();
  const itemsFor = (pid, sts) => {
    const pt = d.tasks.filter(t => t.project_id === pid && !t.dismissed);
    const st = t => w.uiStatus(t);
    if (sts) return pt.filter(t => sts.includes(st(t))).sort((a, b) => (sts[0] === 'queued' ? qOrder.get(a.id) - qOrder.get(b.id) : b.id - a.id));
    return [...pt.filter(t => ACTIVE.includes(st(t))), ...pt.filter(t => st(t) === 'queued').sort((a, b) => qOrder.get(a.id) - qOrder.get(b.id))];
  };
  const rows = d.projects.map(p => {
    const pt = d.tasks.filter(t => t.project_id === p.id);
    const needN = pt.filter(t => !t.dismissed && ['approval', 'error', 'needs_review'].includes(w.uiStatus(t))).length;
    const qn = pt.filter(t => t.status === 'queued').length;
    const running = pt.some(t => t.status === 'running');
    let status = !p.active ? 'paused' : needN ? `${needN} need${needN === 1 ? 's' : ''} you` : running ? 'running now' : qn ? qn + ' queued' : 'quiet';
    if (p.is_general && status === 'quiet') status = 'answers only · pinned';
    const nameHit = !q || p.name.toLowerCase().includes(q) || (p.path || '').toLowerCase().includes(q);
    let items = itemsFor(p.id, cur.statuses);
    if (!nameHit) items = items.filter(t => (taskName(t) + ' ' + t.prompt).toLowerCase().includes(q));
    const visible = (nameHit || items.length > 0) && (pl.showPaused || p.active) && (!cur.statuses || items.length > 0)
      && (!pl.onlyActive || itemsFor(p.id, null).length > 0);
    const open = pl.open[p.id] ?? (pl.quick !== 'all' || (!!q && !nameHit));
    return {
      p, visible, open, items, status,
      score: !p.active ? 0 : needN ? 4 : running ? 3 : qn ? 2 : 1, qn,
      last: pt.reduce((a, t) => Math.max(a, t.id), 0),
      color: !p.active ? 'var(--text-meta)' : needN ? 'var(--wait)' : running ? 'var(--run)' : 'var(--queued)',
      emptyText: cur.statuses ? `No ${cur.label.toLowerCase()} tasks.` : 'Nothing in the queue.',
    };
  }).filter(r => r.visible);
  const sorters = { activity: (a, b) => b.score - a.score || b.qn - a.qn, name: (a, b) => a.p.name.localeCompare(b.p.name), queued: (a, b) => b.qn - a.qn || b.score - a.score, recent: (a, b) => b.last - a.last, newest: (a, b) => b.p.id - a.p.id };
  rows.sort(sorters[pl.sort] || sorters.activity);
  rows.sort((a, b) => (b.p.is_general ? 1 : 0) - (a.p.is_general ? 1 : 0));
  return rows;
}

function rowHTML(r, i, w) {
  const p = r.p;
      const avatar = p.is_general
        ? `<span class="avatar avatar--general">${octopusSVG('idle')}</span>`
        : `<span class="avatar" style="background:${projectColor(p)};opacity:${p.active ? 1 : 0.55}">${esc(p.name.charAt(0).toUpperCase())}</span>`;
      return `<div class="prow" data-key="p${p.id}" style="animation-delay:${Math.min(i, 8) * 45}ms">
        <div class="prow__head">
          <button class="prow__main" data-action="openProject" data-id="${p.id}">${avatar}
            <span class="prow__text"><span class="prow__name truncate" style="opacity:${p.active ? 1 : 0.55}">${esc(p.name)}</span><span class="prow__status" style="--c:${r.color}">${p.is_general ? '' : `<span class="pstat" style="--c:${projectStatus(p)[2]}">${projectStatus(p)[1]}</span> · `}${esc(r.status)}</span></span></button>
          <button class="prow__toggle" aria-expanded="${r.open}" aria-label="Show queue" data-action="plToggle" data-id="${p.id}" data-open="${r.open}">${r.items.length || ''}${chev}</button>
        </div>
        ${r.open ? `<div class="prow__items">${r.items.length ? r.items.map(t => {
          const st = w.uiStatus(t);
          const color = st === 'approval' ? 'var(--wait)' : statusOf(t).color;
          const act = st === 'approval' ? `data-action="openApprovalForTask" data-id="${t.id}"` : `data-action="openChat" data-id="${t.id}"`;
          return `<button class="prow__item" ${act} data-key="pi${t.id}"><span class="dot" style="--c:${color}"></span><span class="grow truncate">${esc(taskName(t))}</span></button>`;
        }).join('') : `<span class="prow__empty">${r.emptyText}</span>`}</div>` : ''}
      </div>`;
}

// General first, then Work and Personal under their own headings (one plain list when a side is picked).
function sections(rows, scope) {
  if (scope !== 'all') return [['', rows]];
  const part = f => rows.filter(f);
  return [['', part(r => r.p.is_general)], ['WORK', part(r => !r.p.is_general && r.p.category === 'work')],
    ['PERSONAL', part(r => !r.p.is_general && r.p.category !== 'work')]].filter(([, l]) => l.length);
}

export function projects(c) {
  const { d, w, ui } = c;
  const pl = ui.projectsList;
  const chips = QUICK_FILTERS.map(f => {
    const n = f.statuses ? d.tasks.filter(t => !t.dismissed && f.statuses.includes(w.uiStatus(t))).length : '';
    return `<button class="chip${pl.quick === f.key ? ' is-on' : ''}" data-action="plQuick" data-key="qf-${f.key}" data-q="${f.key}">${f.dot ? `<span class="dot" style="--c:${f.dot};width:7px;height:7px"></span>` : ''}${f.label}<span class="chip__count">${n}</span></button>`;
  }).join('');
  const filterN = (pl.showPaused ? 0 : 1) + (pl.onlyActive ? 1 : 0);
  const sortLabel = pl.sort === 'activity' ? 'Sort' : SORTS.find(s => s[0] === pl.sort)[1];
  let menu = '';
  if (pl.menu === 'sort') {
    menu = `<div class="menu-scrim" data-action="closeMenus"></div><div class="menu menu--tool" role="menu"><span class="menu__label">SORT BY</span>
      ${SORTS.map(([k, l]) => `<button class="menu__opt" role="menuitemradio" aria-checked="${pl.sort === k}" data-action="plSort" data-sort="${k}"><span class="grow">${l}</span><span class="menu__check">${pl.sort === k ? '✓' : ''}</span></button>`).join('')}</div>`;
  } else if (pl.menu === 'filter') {
    menu = `<div class="menu-scrim" data-action="closeMenus"></div><div class="menu menu--tool menu--260" role="menu"><span class="menu__label">SHOW</span>
      ${[['showPaused', 'Show paused projects'], ['onlyActive', 'Only projects with tasks']].map(([k, l]) =>
        `<button class="menu__opt" role="menuitemcheckbox" aria-checked="${!!pl[k]}" data-action="plFilter" data-f="${k}"><span class="check check--sm" aria-checked="${!!pl[k]}">${pl[k] ? '✓' : ''}</span>${l}</button>`).join('')}</div>`;
  }
  const rows = projectRows(c);
  const hasProj = d.projects.length > 0;
  return `<div class="page page--narrow page--projects" data-key="page-projects">
    ${c.desk ? '' : scopeSwitch(ui.scope, 'setScope', SCOPES)}
    <div class="chip-row" data-noswipe>${chips}</div>
    <div class="toolbar">
      <button class="toolbar__btn${pl.menu === 'sort' || pl.sort !== 'activity' ? ' is-active' : ''}" data-action="plMenu" data-menu="sort">${icon('sort', 16)}${sortLabel}</button>
      <button class="toolbar__btn${pl.menu === 'filter' || filterN ? ' is-active' : ''}" data-action="plMenu" data-menu="filter">${icon('filter', 16)}Filter${filterN ? `<span class="filter-badge">${filterN}</span>` : ''}</button>
      <button class="toolbar__btn toolbar__btn--icon${pl.searchOpen ? ' is-on' : ''}" data-action="plSearch" aria-label="Search">${icon('search', 16, { strokeWidth: 1.8 })}</button>
      <button class="toolbar__add" data-action="openSheet" data-sheet="addProject"><span>+</span>Add</button>
      ${menu}
    </div>
    ${pl.searchOpen ? `<div class="search">${icon('search', 16, { strokeWidth: 1.8 })}<input id="plSearch" type="search" placeholder="Search projects and tasks" value="${esc(pl.q)}" data-input="pl.q" aria-label="Search projects and tasks"><button class="search__x" data-action="plSearchClose" aria-label="Close search">✕</button></div>` : ''}
    ${pl.quick === 'queue' ? bulkBar(rows.flatMap(r => r.items), 'bulk-projects') : ''}
    ${rows.length ? sections(rows, ui.scope).map(([label, list]) => `${label ? `<div class="plist-head">${label}</div>` : ''}<div class="list">${list.map((r, i) => rowHTML(r, i, w)).join('')}</div>`).join('')
    : `<div class="empty"><span>${hasProj ? 'No projects match.' : 'No projects yet.'}</span><button class="btn btn--sm" data-action="${hasProj ? 'plClear' : 'openSheet'}" data-sheet="addProject">${hasProj ? 'Clear filters' : 'Add project'}</button></div>`}
  </div>`;
}
