// Desktop right sidebars, docked from the right-edge rail (queueRail): Queue (what's running, Up next, Completed)
// Messages (the message centre beside whatever page is open) and Calendar. Several can be open side by side (ui.sides); drag a header or rail tab to reorder.
// Resizable (see app.js RESIZERS, shared width); closes from its ✕ or by clicking its rail tab again.

import { priorityLabel, taskName } from '../format.js';
import { projectById, projectColor, threadSummaries, finishedAt } from '../data.js';
import { esc, icon, acctDots, chipFor, restartTag, progressBar, lazy, histRow, bulkBar, clock, spinner, emptyState } from '../ui.js';
import { queueSelBar } from './home.js';
import { accountById, shortName } from './accounts.js';
import { messageControls, messageList } from './messages.js';
import { paneTabs } from './chat.js';
import { calendarSide } from './calendar.js';
import { checklistSide, openCount } from './checklist.js';

// Right-edge rail: always visible on desktop. Each tab is one entry here, a rotated text tab that opens its sidebar.
// Add more by pushing { key, label, badge } - key is the ui.side value it opens.
export function queueRail(c) {
  const tabs = [
    { key: 'queue', label: 'Queue', icon: 'railQueue', badge: c.w.queue.length + c.w.running.length },
    { key: 'messages', label: 'Messages', icon: 'railChat', badge: c.w.needs.length, tone: 'wait' },   // badge: chats waiting on you
    { key: 'calendar', label: 'Calendar', icon: 'calendar', badge: 0 },
    { key: 'todos', label: 'To-dos', icon: 'todos', badge: openCount(c.d) },
  ];
  return `<nav class="rail" data-key="rail" aria-label="Side tabs">${tabs.map(t => {
    const on = c.ui.sides.includes(t.key);
    return `<button class="rail__tab${on ? ' is-on' : ''}" draggable="true" data-drag-side="${t.key}" data-key="rail-${t.key}" data-action="toggleSide" data-side="${t.key}" aria-label="${t.label}" aria-pressed="${on}" title="${t.label}${t.badge && t.tone === 'wait' ? ` · ${t.badge} waiting on you` : ''}">${icon(t.icon, 20)}${t.badge ? `<span class="rail__n${t.tone ? ' rail__n--' + t.tone : ''}">${t.badge > 99 ? '99+' : t.badge}</span>` : ''}</button>`;
  }).join('')}<button class="rail__quiet" data-action="go" data-view="hidden" data-key="rail-quiet" aria-label="Hidden" tabindex="-1"></button></nav>`;
}

// Phone: the same sidebars slid in from the right (ui.rdrawer), one at a time with a switch on top.
// Opened from a thin grip on the right edge (tap or swipe it left); closes from ✕, the scrim or a swipe right (app.js).
export function rightDrawer(c) {
  const open = c.ui.rdrawer, w = c.w;
  const busy = w.queue.length + w.running.length;
  const grip = `<button class="rgrip${busy ? ' has-work' : ''}" data-action="rdrawerOpen" data-key="rgrip" aria-label="Open the side panel (queue, messages, calendar, to-dos)"><i></i></button>`;
  if (!open) return grip;
  const tabs = [['queue', 'Queue', busy], ['messages', 'Messages', w.needs.length], ['calendar', 'Calendar', 0], ['todos', 'To-dos', openCount(c.d)]];
  return `${grip}<div class="rdrawer" data-key="rdrawer">
    <div class="rdrawer__scrim" data-action="rdrawerClose"></div>
    <div class="rdrawer__panel" data-noswipe>
      <div class="rdrawer__tabs"><div class="segmented" role="tablist">${tabs.map(([k, l, n]) => `<button class="segmented__opt${open === k ? ' is-on' : ''}" role="tab" aria-selected="${open === k}" data-action="rdrawerOpen" data-side="${k}">${l}${n ? `<span class="tabs__n">${n}</span>` : ''}</button>`).join('')}</div></div>
      ${sidePanel({ ...c, ui: { ...c.ui, pane: null } }, open, '')}
    </div>
  </div>`;
}

// Every open sidebar, left to right (ui.sides). `chat` is the open chat's pane markup when it is docked (see
// context().docked in app.js); it goes in the ui.side panel only, so the others show their own list.
export function sidePanels(c, chat = '') {
  const others = { ...c, ui: { ...c.ui, pane: null } };
  const one = k => sidePanel(k === c.ui.side ? c : others, k, k === c.ui.side ? chat : '');
  // A column holding several sidebars stacks them top to bottom, sharing its width and height.
  return c.ui.cols.map(col => col.length > 1 ? `<div class="queue-stack" data-key="stack-${col.join('-')}">${col.map(one).join('')}</div>` : one(col[0])).join('');
}

function sidePanel(c, key, chat) {
  if (key === 'queue') return queuePanel(c, chat);
  if (key === 'messages') return messagesPanel(c, chat);
  if (key === 'calendar') return shell('calendar', 'Calendar', '', `<button class="icon-btn" data-action="openEvent" aria-label="Add an item" title="Add an item">+</button>`, calendarSide(c));
  if (key === 'todos') return shell('todos', 'To-dos', openCount(c.d) || '', '', checklistSide(c));
  return '';
}

// The sidebar frame both panels share: resizer, header (title, count, extra tools, ✕), scrolling body.
function shell(key, title, count, tools, body, head = '') {
  return `<aside class="queue-panel queue-panel--${key}" data-key="side-${key}" data-side-panel="${key}" aria-label="${title}">
    <div class="queue-resizer" title="Drag to resize · double-click to reset" aria-hidden="true"></div>
    <div class="queue-panel__head" draggable="true" data-drag-side="${key}" title="Drag to move this panel">
      <span class="queue-panel__title">${title} <span class="section__count">${count || ''}</span></span>
      <span class="queue-panel__tools">${tools}<button class="icon-btn" data-action="closeSide" aria-label="Close" title="Close">${icon('close', 18)}</button></span>
    </div>
    ${head}
    <div class="queue-panel__body">${body}</div>
  </aside>`;
}

// The one chat section: Queue and Messages both dock an open chat here, so only a single chat is ever on screen.
function chatPanel(chat) {
  return `<aside class="queue-panel queue-panel--chat" data-key="side-chat" aria-label="Chat">
    <div class="queue-resizer" title="Drag to resize · double-click to reset" aria-hidden="true"></div>${chat}</aside>`;
}

// ---------------------------------------------------------------- queue
// What's running right now (and anything rate-limited, waiting to resume): Stop, or Review when it's waiting on you.
function nowRow(c, t) {
  const p = projectById(t.project_id);
  const waiting = c.w.apTask.has(t.id), limited = t.status === 'rate_limited';
  const step = waiting ? 'waiting for your OK' : limited ? (t.resume_at ? 'rate-limited · resumes ' + clock(t.resume_at) : 'rate-limited') : t.current_step || 'running';
  const lead = waiting ? `<span class="dot dot--pulse" style="--c:var(--wait)"></span>` : limited ? `<span class="dot" style="--c:var(--sleep)"></span>` : spinner();
  const act = waiting
    ? `<button class="btn btn--sm" data-action="openApprovalForTask" data-id="${t.id}">Review</button>`
    : limited
      ? `<button class="btn btn--sm" data-action="retry" data-id="${t.id}">Retry now</button>`
      : `<button title="Stop - move on to the next task" aria-label="Stop" data-action="stopTask" data-id="${t.id}">■</button>`;
  return `<div class="qrow qrow--now" data-id="${t.id}" data-key="pn${t.id}">
    <span class="qrow__lead">${lead}</span>
    <div class="qrow__body" data-action="openChat" data-id="${t.id}">
      <span class="qrow__title truncate">${esc(taskName(t))}</span>
      <div class="meta-row"><span class="task-num">#${t.id}</span><span class="nowrap">${esc(p ? p.name : '?')}</span>${acctDots([t])}${chipFor(t)}${restartTag(t)}<span class="truncate">· ${esc(step)}</span></div>
      ${progressBar(t, c.now)}
    </div>
    <div class="qrow__acts">${act}</div>
  </div>`;
}

function upNextRow(c, t) {
  const p = projectById(t.project_id);
  const held = c.w.isPaused(t), on = !!t.paused, sel = !!c.ui.qSel[t.id];
  const rest = priorityLabel(t.priority) + (t.thread_id ? ' · reply' : '') + (t.run_now ? ' · starting now' : '') + (on ? ' · paused' : '') + (held ? ' · project paused' : '');
  return `<div class="qrow${c.ui.qOpen[t.id] ? ' is-open' : ''}${held ? ' is-held' : ''}${on ? ' is-paused' : ''}${sel ? ' is-selected' : ''}" data-id="${t.id}" data-key="pq${t.id}">
    <span class="handle" aria-label="Drag to reorder">⋮⋮</span>
    <button class="check" role="checkbox" aria-checked="${sel}" data-action="qCheck" data-id="${t.id}" aria-label="Select">${sel ? '✓' : ''}</button>
    <span class="qrow__swatch" style="background:${projectColor(p)}"></span>
    <div class="qrow__body" data-action="qRowTap" data-id="${t.id}">
      <span class="qrow__title truncate">${esc(taskName(t))}</span>
      <div class="meta-row"><span class="task-num">#${t.id}</span><span class="nowrap">${esc(p ? p.name : '?')}</span>${acctDots([t])}${chipFor(t)}<span class="truncate">· ${esc(rest)}</span></div>
      ${progressBar(t, c.now)}
    </div>
    <div class="qrow__acts"><button class="qrow__chat" title="Open chat" aria-label="Open chat" data-action="openChat" data-id="${t.id}">${icon('messages', 16)}</button>${on
      ? `<button title="Play - put it back in line" aria-label="Play" data-action="playTask" data-id="${t.id}">${icon('play', 16)}</button>`
      : `<button title="Proceed now - start alongside what's running" aria-label="Proceed now" data-action="runNow" data-id="${t.id}">▶</button><button title="Pause - skip it until played" aria-label="Pause" data-action="pauseTask" data-id="${t.id}">${icon('pause', 16)}</button>`}<button title="Edit" aria-label="Edit" data-action="editTask" data-id="${t.id}">✎</button><button title="Move to top" aria-label="Move to top" data-action="toTop" data-id="${t.id}">↑</button><button title="Cancel" aria-label="Cancel" data-action="cancelQueued" data-id="${t.id}">✕</button><button title="Delete - move to the Recycle bin" aria-label="Delete" data-action="deleteQueued" data-id="${t.id}">${icon('trash', 16)}</button></div>
  </div>`;
}

// Same hold notes as Home's Up next: why nothing is moving, else how to reorder.
function queueNote(c) {
  const { w } = c;
  if (!w.queue.length) return ['', ''];
  if (w.approval) return ['held · waiting on you', 'var(--wait)'];
  if (w.rateLimited && w.rateLimited.resume_at) { const a = accountById(c, w.rateLimited.claude_account); return [(a ? shortName(a) + ' ' : w.agentRun ? 'some ' : '') + 'held · until ' + clock(w.rateLimited.resume_at), 'var(--sleep)']; }
  return ['drag ⋮⋮ to reorder', 'var(--text-meta)'];
}

// Project filter chips (Queue sidebar): one tap narrows both tabs to a project, tap again (or All) to clear.
const inProject = c => t => c.ui.sideProject === 'all' || t.project_id === c.ui.sideProject;
function projectChips(c) {
  const cur = c.ui.sideProject;
  const chip = (id, label, color) => `<button class="chip${cur === id ? ' is-on' : ''}" data-action="sideProject" data-key="sp-${id}" data-project="${id}">${color ? `<span class="chip__dot" style="background:${color}"></span>` : ''}${esc(label)}</button>`;
  return `<div class="queue-panel__filter"><div class="chip-row" data-noswipe>${chip('all', 'All')}${c.d.projects.map(p => chip(p.id, p.name, projectColor(p, c.d))).join('')}</div></div>`;
}

function queueBody(c) {
  const { w } = c;
  const mine = inProject(c);
  const q = w.queue.filter(mine);
  const now = [...w.running, ...c.d.tasks.filter(t => t.status === 'rate_limited')].filter(mine);
  const lz = lazy('upnext', q, 6, 6);   // same key as Home's Up next; the sidebar is tall, so more loads as soon as you scroll
  const picked = q.filter(t => c.ui.qSel[t.id]);
  const allPicked = q.length && picked.length === q.length;
  const [note, noteColor] = queueNote(c);
  const nowHTML = now.length ? `<div class="qp-group" data-key="qp-now"><span class="qp-group__label">NOW</span><div class="list">${now.map(t => nowRow(c, t)).join('')}</div></div>` : '';
  if (!q.length) return nowHTML + (nowHTML ? '' : emptyState({ title: c.ui.sideProject === 'all' ? 'Your queue is empty' : 'Nothing queued for this project', body: 'Tasks you queue run one after another, even while you are away.', label: 'Add your first task', attrs: 'data-action="openPalette"' }));
  return `${nowHTML}
    <div class="qp-group" data-key="qp-next">
      <div class="qp-group__head"><span class="qp-group__label">UP NEXT · ${q.length}</span><span class="section__note" style="color:${noteColor}">${note}</span>
        <button class="btn--text" data-action="qSelectAll" data-ids="${allPicked ? '' : q.map(t => t.id).join(',')}">${allPicked ? 'Clear' : 'Select all'}</button></div>
      ${bulkBar(q, 'bulk-side')}
      <div class="list list--queue" data-drag-list="queue">${lz.items.map(t => upNextRow(c, t)).join('')}</div>${lz.more}
    </div>
    ${picked.length ? queueSelBar(picked) : ''}`;
}

// Completed: finished chats, newest first - same list as Home's Completed tab.
function doneBody(c) {
  const done = threadSummaries(c.d).filter(t => (t.status === 'done' || t.status === 'cancelled') && inProject(c)(t))
    .sort((a, b) => finishedAt(b).localeCompare(finishedAt(a)));
  if (!done.length) return emptyState({ title: 'Nothing finished yet', body: 'Finished tasks and their results will collect here.' });
  const lz = lazy('side-done', done, 12, 12);
  return `<div class="list">${lz.items.map(t => histRow(t, { key: 'pd' })).join('')}</div>${lz.more}
    <button class="btn--text qp-all" data-action="go" data-view="activity">All activity →</button>`;
}

export function queuePanel(c, chat = '') {
  // A docked chat takes over the Queue sidebar too; the tabs' list button brings the queue back.
  if (chat && c.ui.pane.type !== 'empty') return chatPanel(chat);
  const { w } = c;
  const tab = c.ui.sideTab;
  const midnight = new Date(c.now); midnight.setHours(0, 0, 0, 0);
  const today = threadSummaries(c.d).filter(t => (t.status === 'done' || t.status === 'cancelled') && new Date(finishedAt(t)) >= midnight).length;
  const opt = (k, label, n) => `<button class="tabs__opt${tab === k ? ' is-on' : ''}" role="tab" aria-selected="${tab === k}" data-action="sideTab" data-tab="${k}">${label}${n ? `<span class="tabs__n">${n}</span>` : ''}</button>`;
  const tabs = `<div class="queue-panel__tabs"><div class="tabs" role="tablist">${opt('queue', 'In queue', w.queue.length + w.running.length)}${opt('done', 'Completed', today ? today + ' today' : '')}</div></div>`;
  return shell('queue', 'Queue', w.queue.length + w.running.length, '', tab === 'done' ? doneBody(c) : queueBody(c), `${c.ui.pane ? paneTabs(c) : ''}${tabs}${projectChips(c)}`);
}

// ---------------------------------------------------------------- messages
// The message centre (views/messages.js) docked beside the page: same search, filters and chats.
// A docked chat takes over the whole sidebar (its own tabs, head and reply box); its tabs' list button comes back here.
export function messagesPanel(c, chat = '') {
  if (chat && c.ui.pane.type !== 'empty') return chatPanel(chat);
  const data = c.d.messages;
  const page = c.ui.view === 'messages' ? '' : `<button class="icon-btn" data-action="go" data-view="messages" aria-label="Open the Messages page" title="Open the Messages page">${icon('messages', 18)}</button>`;
  return shell('messages', 'Messages', data.loaded ? data.total : '', page, messageList(c),
    `${c.ui.pane ? paneTabs(c) : ''}<div class="queue-panel__controls">${messageControls(c, 'side')}</div>`);
}
