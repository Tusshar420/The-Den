// Messages: every chat (a task and its replies), the one with the newest message first.
// Search, status chips and a project / model filter all run on the server (GET /messages),
// so they reach chats older than the dashboard's snapshot.

import { ago, statusOf, taskName } from '../format.js';
import { projectColor, projectById, orderedProjects, SCOPES } from '../data.js';
import { esc, icon, chipFor, scopeSwitch } from '../ui.js';

export const MESSAGE_STATUSES = [['all', 'All'], ['needs', 'Needs you'], ['active', 'Active'], ['done', 'Done'], ['error', 'Failed']];
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

export function messages(c) {
  const { d, ui } = c;
  const m = ui.messages, data = d.messages;
  const terms = m.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const filterN = (m.project !== 'all' ? 1 : 0) + (m.lane !== 'all' ? 1 : 0);
  const filtered = filterN > 0 || m.status !== 'all' || terms.length > 0;

  const chips = MESSAGE_STATUSES.map(([k, l]) =>
    `<button class="chip${m.status === k ? ' is-on' : ''}" data-action="msgStatus" data-key="ms-${k}" data-status="${k}">${l}</button>`).join('');

  const opt = (action, attr, value, label, on, dot = '') =>
    `<button class="menu__opt" role="menuitemradio" aria-checked="${on}" data-action="${action}" data-${attr}="${value}">${dot}<span class="grow truncate">${esc(label)}</span><span class="menu__check">${on ? '✓' : ''}</span></button>`;
  const menu = m.menu ? `<div class="menu-scrim" data-action="closeMenus"></div><div class="menu menu--tool menu--msg" role="menu">
    <span class="menu__label">PROJECT</span>
    ${opt('msgProject', 'id', 'all', 'All projects', m.project === 'all')}
    ${orderedProjects(d).map(p => opt('msgProject', 'id', p.id, p.name, m.project === p.id, `<span class="swatch-sq" style="background:${projectColor(p)}"></span>`)).join('')}
    <span class="menu__label">MODEL</span>
    ${LANES.map(([k, l]) => opt('msgLane', 'lane', k, l, m.lane === k)).join('')}
  </div>` : '';

  let body;
  if (!data.loaded) {
    body = `<div class="empty">Loading chats…</div>`;
  } else if (!data.chats.length) {
    body = `<div class="empty"><span>${filtered ? 'No chats match.' : 'No chats yet.'}</span>${filtered ? `<button class="btn btn--sm" data-action="msgClear">Clear filters</button>` : `<button class="btn btn--sm" data-action="openPalette">New task</button>`}</div>`;
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

  return `<div class="page page--narrow page--messages" data-key="page-messages">
    ${c.desk ? '' : scopeSwitch(ui.scope, 'setScope', SCOPES)}
    <div class="search">${icon('search', 16, { strokeWidth: 1.8 })}<input id="msgSearch" type="search" placeholder="Search chats and messages" value="${esc(m.q)}" data-input="msg.q" aria-label="Search chats and messages" autocomplete="off">${m.q ? `<button class="search__x" data-action="msgSearchClear" aria-label="Clear search">✕</button>` : ''}</div>
    <div class="chip-row" data-noswipe>${chips}</div>
    <div class="toolbar">
      <button class="toolbar__btn${m.menu || filterN ? ' is-active' : ''}" data-action="msgMenu">${icon('filter', 16)}Filter${filterN ? `<span class="filter-badge">${filterN}</span>` : ''}</button>
      ${filtered ? `<button class="toolbar__btn" data-action="msgClear">Clear</button>` : ''}
      <span class="msg-total">${data.loaded ? `${data.total} ${data.total === 1 ? 'chat' : 'chats'}` : ''}</span>
      ${menu}
    </div>
    ${body}
  </div>`;
}
