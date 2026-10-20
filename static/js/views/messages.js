// Messages: every chat (a task and its replies), the one with the newest message first.
// Search, status chips and a project / model filter all run on the server (GET /messages),
// so they reach chats older than the dashboard's snapshot.
import { tabInfo } from './chat.js';

import { ago, statusOf, taskName } from '../format.js';
import { projectColor, projectById, orderedProjects, SCOPES } from '../data.js';
import { esc, icon, chipFor, scopeSwitch, emptyState } from '../ui.js';

export const MESSAGE_STATUSES = [['all', 'All'], ['needs', 'Needs you'], ['active', 'Active'], ['done', 'Done'], ['error', 'Failed']];
// Tasks run to completion; messages are questions whose answers you read.
const KINDS = [['all', 'Tasks and messages'], ['task', 'Tasks'], ['message', 'Messages']];
const LANES = [['all', 'Any model'], ['agent', 'Claude Code'], ['api', 'Answer-only models'], ['assistant', 'Personal assistant']];

// The search words wrapped in <mark>; everything else escaped.
export function highlight(text, terms) {
  if (!terms.length) return esc(text);
  const re = new RegExp('(' + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
  return text.split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join('');
}

function dayLabel(iso) {
  const at = new Date(iso), now = new Date();
  const days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate()) - new Date(at.getFullYear(), at.getMonth(), at.getDate())) / 86400000);
  return days <= 0 ? 'TODAY' : days === 1 ? 'YESTERDAY' : days < 7 ? 'THIS WEEK' : 'EARLIER';
}

function chatRow(ch, c, terms) {
  const p = projectById(ch.project_id);
  const st = ch.needs_you && !['error', 'needs_review'].includes(ch.status) ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(ch);
  const live = ['running', 'rate_limited'].includes(ch.status) || (ch.needs_you && st.label === 'Waiting on you');
  const shown = ch.snippet || ch.preview;
  const who = shown.role === 'you' ? 'You' : 'Octopus';
  const title = taskName(ch);
  const meta = [`${ch.turns} ${ch.turns === 1 ? 'message' : 'messages'}`, `<span style="color:${st.color}">${st.label}</span>`].join(' · ');
  return `<button class="mrow" data-action="openChat" data-id="${ch.id}" data-key="m${ch.id}">
    <span class="dot${live ? ' dot--pulse' : ''}" style="--c:${st.color}"></span>
    <span class="mrow__body">
      <span class="mrow__top"><span class="mrow__title truncate">${highlight(title, terms)}</span><span class="mrow__when">${esc(ago(ch.last_at, c.now))}</span></span>
      <span class="mrow__msg${ch.snippet ? ' is-match' : ''}"><b>${who}:</b> ${highlight(shown.text, terms)}</span>
      <span class="meta-row"><span class="swatch-sq" style="background:${projectColor(p)}"></span><span class="nowrap">${highlight(ch.project_name, terms)}</span>${chipFor(ch)}<span class="truncate">· ${meta}</span></span>
    </span>
  </button>`;
}

// Chats pinned from their tab (c.ui.pinned): kept at the top of the list, one compact row each.
function pinnedSection(c) {
  const ids = c.desk ? (c.ui.pinned || []) : [];
  if (!ids.length) return '';
  const rows = ids.map(id => {
    const t = tabInfo(c, id);
    return `<button class="mrow mrow--pinned" data-action="openChat" data-id="${id}" data-key="pin${id}">
      <span class="dot${t.live ? ' dot--pulse' : ''}" style="--c:${t.color}"></span>
      <span class="mrow__body"><span class="mrow__top"><span class="mrow__title truncate">${esc(t.title)}</span></span>
      <span class="meta-row"><span class="task-num">#${id}</span>${t.project ? `<span class="truncate">· ${esc(t.project)}</span>` : ''}</span></span>
    </button>`;
  }).join('');
  return `<div class="section msg-group" data-key="mg-pinned"><span class="act-group__label">PINNED</span><div class="list">${rows}</div></div>`;
}

const searchTerms = m => m.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
const filterCount = m => (m.project !== 'all' ? 1 : 0) + (m.lane !== 'all' ? 1 : 0) + (m.kind !== 'all' ? 1 : 0);
const isFiltered = m => filterCount(m) > 0 || m.status !== 'all' || searchTerms(m).length > 0;

// Search box, status chips and the project / model filter. Shared by the Messages page and the
// desktop Messages sidebar (queue-panel.js); `where` keeps their search box ids and filter menus apart.
export function messageControls(c, where = 'page') {
  const inputId = where === 'page' ? 'msgSearch' : 'msgSearch-' + where;
  const { d, ui } = c;
  const m = ui.messages, data = d.messages;
  const filterN = filterCount(m), filtered = isFiltered(m);

  const chips = MESSAGE_STATUSES.map(([k, l]) =>
    `<button class="chip${m.status === k ? ' is-on' : ''}" data-action="msgStatus" data-key="ms-${k}" data-status="${k}">${l}</button>`).join('');

  const opt = (action, attr, value, label, on, dot = '') =>
    `<button class="menu__opt" role="menuitemradio" aria-checked="${on}" data-action="${action}" data-${attr}="${value}">${dot}<span class="grow truncate">${esc(label)}</span><span class="menu__check">${on ? '✓' : ''}</span></button>`;
  const menu = m.menu === where ? `<div class="menu-scrim" data-action="closeMenus"></div><div class="menu menu--tool menu--msg" role="menu">
    <span class="menu__label">PROJECT</span>
    ${opt('msgProject', 'id', 'all', 'All projects', m.project === 'all')}
    ${orderedProjects(d).map(p => opt('msgProject', 'id', p.id, p.name, m.project === p.id, `<span class="swatch-sq" style="background:${projectColor(p)}"></span>`)).join('')}
    <span class="menu__label">TYPE</span>
    ${KINDS.map(([k, l]) => opt('msgKind', 'kind', k, l, m.kind === k)).join('')}
    <span class="menu__label">MODEL</span>
    ${LANES.map(([k, l]) => opt('msgLane', 'lane', k, l, m.lane === k)).join('')}
  </div>` : '';

  return `<div class="search">${icon('search', 16, { strokeWidth: 1.8 })}<input id="${inputId}" type="search" placeholder="Search chats and messages" value="${esc(m.q)}" data-input="msg.q" aria-label="Search chats and messages" autocomplete="off">${m.q ? `<button class="search__x" data-action="msgSearchClear" data-focus="${inputId}" aria-label="Clear search">✕</button>` : ''}</div>
    <div class="chip-row" data-noswipe>${chips}</div>
    <div class="toolbar">
      <button class="toolbar__btn${m.menu === where || filterN ? ' is-active' : ''}" data-action="msgMenu" data-where="${where}">${icon('filter', 16)}Filter${filterN ? `<span class="filter-badge">${filterN}</span>` : ''}</button>
      ${filtered ? `<button class="toolbar__btn" data-action="msgClear">Clear</button>` : ''}
      <span class="msg-total">${data.loaded ? `${data.total} ${data.total === 1 ? 'chat' : 'chats'}` : ''}</span>
      ${menu}
    </div>`;
}

// The chats, grouped by day, with "Show more" at the end.
export function messageList(c) {
  const m = c.ui.messages, data = c.d.messages;
  const terms = searchTerms(m), filtered = isFiltered(m);
  let body;
  if (!data.loaded) {
    body = `<div class="empty">Loading chats…</div>`;
  } else if (!data.chats.length) {
    body = (filtered ? `<div class="empty"><span>No chats match.</span><button class="btn btn--sm" data-action="msgClear">Clear filters</button></div>`
      : emptyState({ title: 'No chats yet', body: 'Every task is a chat with Octopus. Start one and the conversation shows up here.', label: 'Add your first task', attrs: 'data-action="openPalette"' }));
  } else {
    const groups = [];
    for (const ch of data.chats) {
      const label = dayLabel(ch.last_at);
      if (!groups.length || groups[groups.length - 1].label !== label) groups.push({ label, rows: [] });
      groups[groups.length - 1].rows.push(ch);
    }
    body = groups.map(g => `<div class="section msg-group" data-key="mg-${g.label}">
      <span class="act-group__label">${g.label}</span>
      <div class="list">${g.rows.map(ch => chatRow(ch, c, terms)).join('')}</div>
    </div>`).join('')
      + (data.chats.length < data.total ? `<button class="btn btn--ghost msg-more" data-action="msgMore">Show more · ${data.chats.length} of ${data.total}</button>` : '');
  }
  return (filtered ? '' : pinnedSection(c)) + body;
}

export function messages(c) {
  const full = !!c.ui.messages.full;
  return `<div class="page page--narrow page--messages${full ? ' is-full' : ''}" data-key="page-messages">
    <button class="btn btn--sm msg-full" data-action="msgFull" aria-pressed="${full}" title="${full ? 'Exit full screen' : 'Full screen'}">${icon(full ? 'collapse' : 'expand', 16)}${full ? 'Exit full screen' : 'Full screen'}</button>
    ${c.desk ? '' : scopeSwitch(c.ui.scope, 'setScope', SCOPES)}
    ${messageControls(c)}
    ${messageList(c)}
  </div>`;
}
