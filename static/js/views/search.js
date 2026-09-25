// Header search: one box that finds pages, projects, chats (and what was said in them), recipes and schedules.
// Everything except message contents is matched here against the snapshot the dashboard already holds;
// message contents come from GET /messages?q= (see loadSearchSoon in data.js).

import { statusOf, taskName } from '../format.js';
import { projectColor, threadSummaries, orderedProjects } from '../data.js';
import { esc, icon } from '../ui.js';
import { highlight } from './messages.js';

// [kind, id, label, extra words it should also be found by]. 'view' and 'personal' navigate; 'action' opens a sheet.
const PAGES = [
  ['view', 'home', 'Command Center', 'home queue approvals'],
  ['view', 'projects', 'Projects', ''],
  ['view', 'recipes', 'Recipes & schedules', 'recurring'],
  ['view', 'activity', 'Activity', 'history'],
  ['view', 'messages', 'Messages', 'chats conversations'],
  ['view', 'providers', 'AI Providers', 'models'],
  ['view', 'routing', 'Routing', 'models'],
  ['view', 'settings', 'Settings', 'preferences'],
  ['personal', 'email', 'Personal assistant · Email', ''],
  ['personal', 'calendar', 'Personal assistant · Calendar', ''],
  ['personal', 'reminders', 'Personal assistant · Reminders', ''],
  ['personal', 'connections', 'Personal assistant · Connections', 'google'],
  ['action', 'palette', 'New task', 'create add queue'],
  ['action', 'addProject', 'Add project', 'register'],
  ['action', 'keys', 'Keyboard shortcuts', 'help hotkeys'],
];
const LIMITS = { pages: 6, projects: 5, chats: 8, recipes: 4, schedules: 4 };
const TAG = { view: 'PAGE', personal: 'PAGE', action: 'ACTION', project: 'PROJECT', chat: 'CHAT', recipe: 'RECIPE', schedule: 'SCHEDULE' };

const words = q => q.trim().toLowerCase().split(/\s+/).filter(Boolean);

// Sections of { kind, id, title, sub, color? }, in the order they are shown. With nothing typed: the pages and actions.
export function searchSections(c) {
  const { d, ui } = c;
  const terms = words(ui.search.q);
  const hit = (...fields) => {
    const h = fields.filter(Boolean).join(' ').toLowerCase();
    return terms.every(t => h.includes(t));
  };
  const out = [];
  const add = (label, items, limit) => { if (items.length) out.push({ label, items: items.slice(0, limit) }); };

  add('PAGES & ACTIONS', PAGES.filter(([, , label, extra]) => hit(label, extra)).map(([kind, id, title]) => ({ kind, id, title, sub: '' })), terms.length ? LIMITS.pages : PAGES.length);
  if (!terms.length) return out;

  add('PROJECTS', orderedProjects(d).filter(p => hit(p.name, p.path, p.category))
    .map(p => ({ kind: 'project', id: p.id, title: p.name, sub: p.path || '', color: projectColor(p) })), LIMITS.projects);

  // Chats: the server's answer (it looks inside every message, however old) first, then whatever the snapshot
  // already matches while that is on its way.
  const seen = new Set(), chats = [];
  const take = (t, status, sub) => { if (!seen.has(t.id)) { seen.add(t.id); chats.push({ kind: 'chat', id: t.id, title: taskName(t), sub, status, project: t.project_name }); } };
  const s = ui.search;
  if (s.chatsFor === s.q.trim()) {
    for (const ch of s.chats) {
      const shown = ch.snippet || ch.preview;
      take(ch, statusOf(ch), shown ? `${shown.role === 'you' ? 'You' : 'Octopus'}: ${shown.text}` : '');
    }
  }
  for (const t of threadSummaries(d)) {
    if (hit(t.title, t.prompt, t.project_name)) take(t, statusOf({ ...t, status: c.w.uiStatus(t) }), t.title ? t.prompt : '');
  }
  add('CHATS', chats, LIMITS.chats);

  add('RECIPES', (d.recipes || []).filter(r => hit(r.name, r.prompt)).map(r => ({ kind: 'recipe', id: r.id, title: r.name, sub: r.prompt })), LIMITS.recipes);
  add('SCHEDULES', (d.schedules || []).filter(x => hit(x.label, x.prompt)).map(x => ({ kind: 'schedule', id: x.id, title: x.label || x.prompt, sub: `${x.cadence} · ${x.time_of_day}${x.enabled ? '' : ' · paused'}` })), LIMITS.schedules);
  return out;
}

export const searchItems = c => searchSections(c).flatMap(s => s.items);

function row(it, i, sel, terms) {
  const lead = it.kind === 'project' ? `<span class="swatch-sq" style="background:${it.color}"></span>`
    : it.kind === 'chat' ? `<span class="dot" style="--c:${it.status.color}"></span>` : '';
  const meta = it.kind === 'chat'
    ? `<span class="nowrap">${highlight(it.project || '', terms)}</span><span style="color:${it.status.color}">${esc(it.status.label)}</span>`
    : '';
  return `<button class="srow${i === sel ? ' is-sel' : ''}" data-action="searchPick" data-kind="${it.kind}" data-id="${esc(String(it.id))}" data-key="sr-${it.kind}-${esc(String(it.id))}">
    ${lead}
    <span class="srow__body">
      <span class="srow__title truncate">${highlight(it.title, terms)}</span>
      ${it.sub || meta ? `<span class="srow__sub truncate">${it.sub ? highlight(it.sub, terms) : ''}${meta ? `<span class="srow__meta">${meta}</span>` : ''}</span>` : ''}
    </span>
    <span class="srow__tag">${TAG[it.kind]}</span>
  </button>`;
}

function panel(c) {
  const s = c.ui.search, terms = words(s.q);
  const sections = searchSections(c);
  let i = 0;
  const body = sections.length
    ? sections.map(sec => `<div class="spanel__group" data-key="sg-${sec.label}"><span class="spanel__label">${sec.label}</span>${sec.items.map(it => row(it, i++, s.sel, terms)).join('')}</div>`).join('')
    : `<div class="spanel__empty">No match for “${esc(s.q.trim())}”.<span>Searches pages, projects, chats and what was said in them, recipes and schedules.</span></div>`;
  return `<div class="spanel" data-key="spanel" role="listbox">${body}</div>`;
}

// Desktop: the box always sits in the header. Phone: an icon that turns the whole header into the box (see .topbar--searching).
export function searchBox(c) {
  const s = c.ui.search;
  if (!c.desk && !s.open) {
    return `<button class="icon-btn" data-action="searchOpen" data-key="tsearch" aria-label="Search" title="Search">${icon('search', 18)}</button>`;
  }
  const clear = c.desk
    ? (s.q ? `<button class="tsearch__x" data-action="searchClear" aria-label="Clear search">✕</button>` : '<span class="tsearch__kbd">/</span>')
    : `<button class="tsearch__cancel" data-action="searchClose">Cancel</button>`;
  return `<div class="tsearch${s.open ? ' is-open' : ''}" data-key="tsearch" role="search">
    <div class="tsearch__box">${icon('search', 16, { strokeWidth: 1.8 })}<input id="topSearch" type="search" placeholder="Search pages, projects, chats…" value="${esc(s.q)}" data-input="search" aria-label="Search everything" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="go">${clear}</div>
    ${s.open ? panel(c) : ''}
  </div>`;
}
