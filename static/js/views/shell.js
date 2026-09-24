// App chrome: topbar, desktop sidebar, phone tab bar + FAB, toast, skeletons.

import { money, plural } from '../format.js';
import { orderedProjects, projectColor, queuedTasks } from '../data.js';
import { esc, icon, octo, sk } from '../ui.js';

const TITLES = { home: 'Home', projects: 'Projects', recipes: 'Recipes & schedules', activity: 'Activity' };

export function viewTitle(c) {
  const { ui, desk } = c;
  if (!desk && ui.view === 'home') return 'Workspace';
  if (!desk && ui.view === 'project') return 'Projects';
  if (ui.view === 'project') return (c.project && c.project.name) || 'Project';
  return TITLES[ui.view] || 'Workspace';
}

const densityIcon = compact => compact
  ? '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 5.5h12M3 9h12M3 12.5h12"/></svg>'
  : '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 3.5h12M3 9h12M3 14.5h12"/></svg>';

const sessionColor = pct => (pct >= 95 ? 'var(--sleep)' : pct >= 80 ? 'var(--wait)' : 'var(--run)');
export { sessionColor };

function usagePill(c) {
  const { w, d } = c;
  const bars = w.session
    ? `<div class="usage-pill__bars">
        <div class="bar4"><div style="width:${w.session.pct}%;background:${sessionColor(w.session.pct)}"></div></div>
        <div class="bar4"><div style="width:${w.week ? w.week.pct : 0}%;background:var(--text-meta)"></div></div>
      </div>` : '';
  const today = d.spend ? d.spend.today_usd : 0;
  return `<button class="usage-pill" data-action="openSheet" data-sheet="usage" aria-label="Usage">${bars}<span class="usage-pill__val">${money(today)}</span></button>`;
}

export function topbar(c) {
  const { ui, desk, d } = c;
  const onProject = ui.view === 'project';
  const back = !desk && onProject ? `<button class="icon-btn icon-btn--44 back-btn" data-action="goBack" aria-label="Back">←</button>` : '';
  const eyebrow = !desk ? `<span class="topbar__eyebrow">${ui.view === 'home' ? "TUSHAR'S" : 'WORKSPACE'}</span>` : '';
  const live = !(onProject && !desk)
    ? (d.error ? `<span class="live live--off">offline</span>` : `<span class="live">live</span>`) : '';
  return `<header class="topbar">
    ${back}
    <div class="topbar__titles">${eyebrow}<span class="topbar__title">${esc(viewTitle(c))}</span></div>
    <span class="topbar__spacer"></span>
    ${live}
    ${usagePill(c)}
    <button class="icon-btn${c.compact ? ' is-on' : ''}" data-action="toggleDensity" aria-label="Compact layout" title="Compact layout">${densityIcon(c.compact)}</button>
    ${desk ? `<button class="topbar__new" data-action="openPalette">+ New task<span>⌘K</span></button>` : ''}
  </header>`;
}

function projectMeta(p, c) {
  const { d, w } = c;
  if (!p.active) return ['paused', 'var(--text-meta)'];
  const live = d.tasks.find(t => t.project_id === p.id && !t.dismissed && (w.apTask.has(t.id) || ['running', 'rate_limited', 'error'].includes(t.status)));
  if (live) {
    const s = w.uiStatus(live);
    return s === 'approval' ? ['needs you', 'var(--wait)'] : s === 'running' ? ['running', 'var(--run)'] : s === 'rate_limited' ? ['asleep', 'var(--sleep)'] : ['failed', 'var(--err)'];
  }
  const qn = queuedTasks(d).filter(t => t.project_id === p.id).length;
  return qn ? [qn + ' queued', 'var(--text-meta)'] : ['', 'var(--text-meta)'];
}

export function sidebar(c) {
  const { ui, d, w } = c;
  const nav = [['home', 'Home', '1'], ['projects', 'Projects', '2'], ['recipes', 'Recipes & schedules', '3'], ['activity', 'Activity', '4']];
  const needs = w.needs.length;
  return `<nav class="sidebar" aria-label="Workspace">
    <button class="sidebar__brand" data-action="go" data-view="home">${octo(w.mood, 42)}<span class="sidebar__brand-text"><span class="sidebar__brand-top">TUSHAR'S</span><span class="sidebar__brand-name">Workspace</span></span></button>
    <button class="sidebar__status" data-action="go" data-view="home"><span class="dot dot--pulse" style="--c:${w.hero.color}"></span><span class="grow">${esc(w.hero.short)}</span></button>
    <div class="sidebar__group">
      ${nav.map(([v, l, k]) => `<button class="nav-item${ui.view === v ? ' is-active' : ''}" data-action="go" data-view="${v}"><span class="grow">${l}</span>${v === 'home' && needs ? `<span class="count-badge count-badge--sm">${needs}</span>` : ''}<span class="nav-item__key">${k}</span></button>`).join('')}
    </div>
    <div class="sidebar__group">
      <span class="sidebar__label">PROJECTS</span>
      ${orderedProjects(d).map(p => {
        const [meta, mc] = projectMeta(p, c);
        const on = ui.view === 'project' && ui.projectId === p.id;
        return `<button class="nav-item nav-proj${on ? ' is-active' : ''}" data-action="openProject" data-id="${p.id}" data-key="np${p.id}">
          ${p.is_general ? octo('idle', 18, ';margin:0 -5px') : `<span class="nav-proj__swatch" style="background:${projectColor(p)}"></span>`}
          <span class="grow truncate" style="opacity:${p.active ? 1 : 0.55}">${esc(p.name)}</span>
          <span class="nav-proj__meta" style="color:${mc}">${meta}</span></button>`;
      }).join('')}
      <button class="nav-item nav-add" data-action="openSheet" data-sheet="addProject">+ Add project</button>
    </div>
    <div class="sidebar__foot">
      <button class="sidebar__new" data-action="openPalette">New task<span>⌘K</span></button>
      <button class="sidebar__keys" data-action="openSheet" data-sheet="keys">Keyboard shortcuts · ?</button>
    </div>
  </nav>`;
}

export function tabbar(c) {
  const { ui, w } = c;
  const tab = (v, l, ic) => {
    const on = ui.view === v || (v === 'projects' && ui.view === 'project');
    const badge = v === 'home' && w.needs.length ? `<span class="tab__badge">${w.needs.length}</span>` : '';
    return `<button class="tab${on ? ' is-active' : ''}" data-action="go" data-view="${v}">${icon(ic, 22)}${l}${badge}</button>`;
  };
  return `<nav class="tabbar" aria-label="Views">
    ${tab('home', 'Home', 'home')}${tab('projects', 'Projects', 'projects')}
    <button class="fab" data-action="openPalette" aria-label="New task">+</button>
    ${tab('recipes', 'Recipes', 'recipes')}${tab('activity', 'Activity', 'activity')}
  </nav>`;
}

export function toast(c) {
  const t = c.ui.toast;
  if (!t) return '';
  return `<div class="toast" role="status" data-key="toast${t.id}"><span class="truncate">${esc(t.text)}</span>${t.action ? `<button class="toast__action" data-action="toastAction">${esc(t.action)}</button>` : ''}</div>`;
}

// ---------------------------------------------------------------- skeletons (≈520ms after every view change)
export function homeSkeleton(c) {
  const hc = c.desk ? (c.compact ? 64 : 88) : c.compact ? 52 : 76;
  const row = () => `<div class="sk-row">${sk('8px', 8, 3)}<div class="sk-col">${sk('75%', 11, 5)}${sk('45%', 9, 5)}</div></div>`;
  return `<div class="page page--sk" data-key="sk-home">
    <div class="hero" style="grid-template-columns:${hc}px minmax(0,1fr)">${sk(hc + 'px', hc, hc / 2)}<div class="sk-col" style="gap:9px">${sk('40%', 10, 5)}${sk('92%', 20, 7)}${sk('68%', 20, 7)}${sk('84%', 11, 5)}</div></div>
    ${sk('100%', 62, 16)}${sk('100%', 150, 20)}
    <div class="sk-col" style="gap:8px">${sk('30%', 12, 6)}<div class="sk-list">${[0, 1, 2, 3, 4].map(row).join('')}</div></div>
  </div>`;
}

export function listSkeleton() {
  const row = () => `<div class="sk-row sk-row--64">${sk('38px', 38, 12)}<div class="sk-col" style="gap:7px">${sk('55%', 12, 6)}${sk('32%', 9, 5)}</div></div>`;
  return `<div class="page page--narrow page--sk" data-key="sk-list">
    <div class="sk-chips">${[44, 78, 76, 72, 64].map(wd => sk(wd + 'px', 40, 999)).join('')}</div>
    <div class="sk-tools">${sk('76px', 44, 12)}${sk('84px', 44, 12)}${sk('44px', 44, 12)}${sk('78px', 44, 12, ';margin-left:auto')}</div>
    <div class="sk-list">${[0, 1, 2, 3, 4].map(row).join('')}</div>
  </div>`;
}

export { plural };
