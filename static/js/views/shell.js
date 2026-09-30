// App chrome: topbar (with the notification bell), desktop sidebar, phone tab bar + FAB, skeletons.

import { plural } from '../format.js';
import { orderedProjects, projectColor, queuedTasks, pendingApprovals, SCOPES } from '../data.js';
import { isAssistant } from './assistant-card.js';
import { searchBox } from './search.js';
import { esc, icon, octo, sk, scopeSwitch, clock, dayWord, spinner } from '../ui.js';

const TITLES = { home: 'Command Center', projects: 'Projects', recipes: 'Recipes & schedules', activity: 'Activity', personal: 'Personal assistant', messages: 'Messages',
  routing: 'Routing', providers: 'AI Providers', settings: 'Settings', voice: 'Voice', usage: 'Token usage', calendar: 'Calendar', briefing: 'Good morning' };
const OCTOPUS_NAV = [['usage', 'Token usage'], ['providers', 'AI Providers'], ['routing', 'Routing'], ['voice', 'Voice'], ['settings', 'Settings']];
const PERSONAL_NAV = [['email', 'Email'], ['calendar', 'Calendar'], ['reminders', 'Reminders'], ['connections', 'Connections']];

export function viewTitle(c) {
  const { ui, desk } = c;
  if (!desk && ui.view === 'home') return 'Workspace';
  if (!desk && ui.view === 'project') return 'Projects';
  if (ui.view === 'project') return (c.project && c.project.name) || 'Project';
  return TITLES[ui.view] || 'Workspace';
}

const sessionColor = pct => (pct >= 95 ? 'var(--sleep)' : pct >= 80 ? 'var(--wait)' : 'var(--run)');
export { sessionColor };

function usageRow(label, u, color, resetText) {
  const pct = u ? u.pct : 0;
  return `<div class="usage-pill__row"><span class="usage-pill__lbl">${label}</span>
    <div class="bar4"><div style="width:${pct}%;background:${color}"></div></div>
    <span class="usage-pill__pct">${u ? pct + '%' : '–'}</span>
    <span class="usage-pill__reset">${u && u.resets ? resetText(u.resets) : ''}</span></div>`;
}

// Phone project page: the two-row pill squeezes the title, so show just the 5h session as a ring
// (the weekly figure is still in the Usage sheet this opens).
function usageRing(w) {
  const u = w.session;
  const pct = u ? Math.max(0, Math.min(100, u.pct)) : 0;
  const r = 15, circ = 2 * Math.PI * r;
  return `<button class="usage-ring" data-action="openSheet" data-sheet="usage" aria-label="Usage: ${u ? pct + '% of the 5 hour session used' : 'unknown'}">
    <svg viewBox="0 0 36 36" aria-hidden="true"><circle class="usage-ring__track" cx="18" cy="18" r="${r}"/>
      <circle class="usage-ring__arc" cx="18" cy="18" r="${r}" stroke="${u ? sessionColor(pct) : 'var(--text-meta)'}" stroke-dasharray="${(circ * pct / 100).toFixed(1)} ${circ.toFixed(1)}"/></svg>
    <span class="usage-ring__pct">${u ? pct : '–'}</span></button>`;
}

function usagePill(c) {
  const { w } = c;
  if (!c.desk && c.ui.view === 'project') return usageRing(w);
  const hm = r => clock(r.toISOString());
  const rows = w.session || w.week
    ? usageRow('5h', w.session, w.session ? sessionColor(w.session.pct) : 'var(--text-meta)', r => '<i>resets </i>' + hm(r))
      + usageRow('wk', w.week, 'var(--text-meta)', r => '<i>resets </i>' + r.toLocaleDateString([], { weekday: 'short' }) + ' ' + hm(r))
    : '<span class="usage-pill__reset">usage</span>';
  return `<button class="usage-pill" data-action="openSheet" data-sheet="usage" aria-label="Usage">${rows}</button>`;
}

// Restart Octopus: reload the service's code, then this page. On phone it lives in the Usage sheet.
export function restartText(r) {
  if (!r) return 'Loads the latest code, then reloads this page. If something is running, it finishes first.';
  return r.waitingOn.length
    ? `Waiting for ${r.waitingOn.map(id => '#' + id).join(', ')} to finish, then restarting. Nothing new starts meanwhile.`
    : "Restarting… this page reloads when it's back.";
}
function restartButton(c) {
  const r = c.ui.restart;
  return `<button class="icon-btn${r ? ' is-on' : ''}" data-action="restartOctopus" aria-label="Restart Octopus" title="${r ? esc(restartText(r)) : 'Restart Octopus · load the latest code'}"${r ? ' disabled' : ''}>${r ? spinner() : icon('restart', 18)}</button>`;
}

export function topbar(c) {
  const { ui, desk, d } = c;
  const onProject = ui.view === 'project';
  const back = !desk && onProject ? `<button class="icon-btn icon-btn--44 back-btn" data-action="goBack" aria-label="Back">←</button>` : '';
  const eyebrow = !desk ? `<span class="topbar__eyebrow">${ui.view === 'home' ? "TUSHAR'S" : 'WORKSPACE'}</span>` : '';
  const live = !(onProject && !desk)
    ? (d.error ? `<span class="live live--off">offline</span>` : `<span class="live">live</span>`) : '';
  // Phone: the sidebar is a drawer, opened from here (a project page has the back button instead).
  const menu = (desk ? !c.navOpen : !onProject)
    ? `<button class="icon-btn topbar__menu" data-action="toggleSidebar" aria-label="${desk ? 'Expand sidebar' : 'Open menu'}" title="Expand sidebar · [" aria-expanded="false" aria-controls="sidebar">${icon('sidebar', 20)}</button>` : '';
  // #bell stays empty here: NotificationCenter owns its contents and classes, and morph
  // leaves anything it didn't create alone. The data-key keeps it the same element.
  // On a phone the open search takes over the whole header (CSS hides the rest, the bell included -
  // it has to stay in the DOM, see above).
  const searching = !desk && ui.search.open;
  return `<header class="topbar${searching ? ' topbar--searching' : ''}" data-key="topbar">
    ${menu}
    ${back}
    <div class="topbar__titles">${eyebrow}<span class="topbar__title">${esc(viewTitle(c))}</span></div>
    ${desk ? searchBox(c) : ''}
    <span class="topbar__spacer"></span>
    ${live}
    ${usagePill(c)}
    ${desk ? restartButton(c) : searchBox(c)}
    <button id="bell" data-key="bell"></button>
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

// Quick glance at the bottom of the sidebar: live counts, 5h usage, next schedule, and the
// compact-layout switch.
function sidebarDetails(c) {
  const { w } = c;
  const stat = (label, n, color) => `<div class="sidebar__stat"><span class="sidebar__stat-n" style="color:${n ? color : 'var(--text-meta)'}">${n}</span><span class="sidebar__stat-l">${label}</span></div>`;
  const s = w.session;
  const next = w.nextSched;
  return `<div class="sidebar__details">
    <div class="sidebar__stats">${stat('running', w.running.length, 'var(--run)')}${stat('queued', w.nq, 'var(--text)')}${stat('need you', w.needs.length, 'var(--wait)')}</div>
    ${s ? `<div class="sidebar__row"><span class="usage-pill__lbl">5h</span><div class="bar4"><div style="width:${s.pct}%;background:${sessionColor(s.pct)}"></div></div><span class="usage-pill__pct">${s.pct}%</span></div>` : ''}
    ${next ? `<div class="sidebar__row sidebar__row--meta"><span>Next schedule</span><span>${esc(dayWord(next.next_run_at))} ${clock(next.next_run_at)}</span></div>` : ''}
    <div class="sidebar__row sidebar__row--meta"><span>Compact layout</span><button class="toggle" role="switch" aria-checked="${c.compact}" data-action="toggleDensity" aria-label="Compact layout"></button></div>
  </div>`;
}

export function sidebar(c) {
  const { ui, d, w } = c;
  const nav = [['home', 'Command Center', '1'], ['projects', 'Projects', '2'], ['calendar', 'Calendar', '7'], ['recipes', 'Recipes & schedules', '3'], ['activity', 'Activity', '4'], ['messages', 'Messages', '6']];
  const needs = w.needs.length;
  return `<div class="sidebar-scrim${c.navOpen && c.navOverlay ? ' is-on' : ''}" data-action="toggleSidebar" data-key="sidebar-scrim" aria-hidden="true"></div>
  <nav class="sidebar${c.navOpen ? '' : ' is-collapsed'}${c.navOverlay ? ' sidebar--overlay' : ''}" id="sidebar" data-key="sidebar" aria-label="Workspace"${c.navOpen ? '' : ' inert'}>
    <div class="sidebar__head">
      <button class="sidebar__brand" data-action="go" data-view="home">${octo(w.mood, 42)}<span class="sidebar__brand-text"><span class="sidebar__brand-top">TUSHAR'S</span><span class="sidebar__brand-name">Workspace</span></span></button>
      <button class="icon-btn icon-btn--ghost sidebar__toggle" data-action="toggleSidebar" aria-label="Collapse sidebar" title="Collapse sidebar · [" aria-expanded="${c.navOpen}" aria-controls="sidebar">${icon('sidebar', 20)}</button>
    </div>
    <button class="sidebar__status" data-action="go" data-view="home"><span class="dot dot--pulse" style="--c:${w.hero.color}"></span><span class="grow">${esc(w.hero.short)}</span></button>
    ${scopeSwitch(ui.scope, 'setScope', SCOPES)}
    <div class="sidebar__group">
      ${nav.map(([v, l, k]) => `<button class="nav-item${ui.view === v ? ' is-active' : ''}" data-action="go" data-view="${v}"><span class="grow">${l}</span>${v === 'home' && needs ? `<span class="count-badge count-badge--sm">${needs}</span>` : ''}<span class="nav-item__key">${k}</span></button>`).join('')}
    </div>
    <div class="sidebar__group">
      <span class="sidebar__label">OCTOPUS</span>
      ${OCTOPUS_NAV.map(([v, l]) => `<button class="nav-item${ui.view === v ? ' is-active' : ''}" data-action="go" data-view="${v}"><span class="grow">${l}</span></button>`).join('')}
    </div>
    <div class="sidebar__group">
      <span class="sidebar__label">PERSONAL ASSISTANT</span>
      ${PERSONAL_NAV.map(([k, l]) => {
        const n = pendingApprovals(d).filter(a => isAssistant(a) && a.tool_name.startsWith('assistant:' + k + '.')).length;
        return `<button class="nav-item${ui.view === 'personal' && ui.personalTab === k ? ' is-active' : ''}" data-action="goPersonal" data-tab="${k}"><span class="grow">${l}</span>${n ? `<span class="count-badge count-badge--sm">${n}</span>` : ''}</button>`;
      }).join('')}
    </div>
    <div class="sidebar__group">
      ${(() => {
        const navProj = p => {
        const [meta, mc] = projectMeta(p, c);
        const on = ui.view === 'project' && ui.projectId === p.id;
        return `<button class="nav-item nav-proj${on ? ' is-active' : ''}" data-action="openProject" data-id="${p.id}" data-key="np${p.id}">
          ${p.is_general ? octo('idle', 18, ';margin:0 -5px') : `<span class="nav-proj__swatch" style="background:${projectColor(p)}"></span>`}
          <span class="grow truncate" style="opacity:${p.active ? 1 : 0.55}">${esc(p.name)}</span>
          <span class="nav-proj__meta" style="color:${mc}">${meta}</span></button>`;
        };
        const all = orderedProjects(d);
        if (ui.scope !== 'all') return `<span class="sidebar__label">${ui.scope === 'work' ? 'WORK' : 'PERSONAL'}</span>${all.map(navProj).join('')}`;
        const sec = (label, list) => list.length ? `<span class="sidebar__label">${label}</span>${list.map(navProj).join('')}` : '';
        return sec('GENERAL', all.filter(p => p.is_general)) + sec('WORK', all.filter(p => !p.is_general && p.category === 'work'))
          + sec('PERSONAL', all.filter(p => !p.is_general && p.category !== 'work'));
      })()}
      <button class="nav-item nav-add" data-action="openSheet" data-sheet="addProject">+ Add project</button>
    </div>
    <div class="sidebar__foot">
      ${sidebarDetails(c)}
      <button class="sidebar__new" data-action="openPalette">New task<span>⌘K</span></button>
      <button class="sidebar__keys" data-action="openSheet" data-sheet="keys">Keyboard shortcuts · ?</button>
    </div>
  </nav>`;
}

export function tabbar(c) {
  const { ui, w } = c;
  const tab = (v, l, ic) => {
    const on = ui.view === v || (v === 'projects' && ui.view === 'project');
    const paPending = pendingApprovals(c.d).filter(isAssistant).length;
    const badge = v === 'home' && w.needs.length ? `<span class="tab__badge">${w.needs.length}</span>`
      : v === 'personal' && paPending ? `<span class="tab__badge">${paPending}</span>` : '';
    return `<button class="tab${on ? ' is-active' : ''}" data-action="go" data-view="${v}">${icon(ic, 22)}${l}${badge}</button>`;
  };
  return `<nav class="tabbar" aria-label="Views">
    ${tab('home', 'Home', 'home')}${tab('projects', 'Projects', 'projects')}${tab('recipes', 'Recipes', 'recipes')}
    <button class="fab" data-action="openPalette" aria-label="New task">+</button>
    ${tab('activity', 'Activity', 'activity')}${tab('personal', 'Personal', 'personal')}${tab('messages', 'Messages', 'messages')}
  </nav>`;
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
