// Project detail: header, paused banner, stats, and the Work / Activity / Recipes /
// Settings tabs. Backlog = the existing todos API.

import { mmss, money, plural, priorityLabel, agoShort, ago, statusOf, fmtCost, taskName, PERMISSION_MODES, modeInfo } from '../format.js';
import { projectColor, projectFill, queuedTasks, threadSummaries, finishedAt, PROJECT_STATUSES, NOTE_KINDS, crmOnly, NO_FOLDER_HINT } from '../data.js';
import { acctDots, esc, octo, chipFor, histRow, lazy, lazyScroll, toggle, glyph, took, scopeSwitch, polishBtn, icon, progressBar, modelLabel, usedBy, emptyState, sk } from '../ui.js';
import { octopusSVG } from '../octopus.js';
import { recipeCard } from './recipes.js';
import { timelineTab, meetingNotes, meetingWhen } from './calendar.js';
import { phasesTab } from './phases.js';
import { projectAccountCard, accountById, shortName, accountField, accountMenuPart, accountShort, usesClaudeAccount } from './accounts.js';
import { imageStrip, sentStrip } from '../attach.js';
import { FEATURES } from '../features.js';
import { docThumb } from '../preview.js';
import { todoModels } from './run-review.js';
import { ask } from './home.js';
import { overviewTab } from './overview.js';
import { checklistTab, openCount } from './checklist.js';

// Overview (the CRM record) comes first; General has no record, so it doesn't get one.
const TABS = [['overview', 'Overview'], ['work', 'Work'],['backlog', 'Backlog'], ['todos', 'To-dos'], ['phases', 'Phases'], ['timeline', 'Timeline'], ['notes', 'Notes'], ['documents', 'Documents'], ['activity', 'Activity'], ['recipes', 'Recipes']];
const chevSVG = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5l4 4 4-4"/></svg>';
const tildify = p => (p || '').replace(/^\/Users\/[^/]+/, '~');

// In place of anything that would start work, on a CRM-only project (no folder).
export const noFolderCard = (key, what = 'Nothing can run here yet') => `<div class="card card--pad crm-nofolder" data-key="${key}">
  <span class="card__title">${what}</span><span class="card__desc">${NO_FOLDER_HINT}. Until then this project is a CRM record: overview, phases, notes, documents and calendar all work.</span>
  <button class="btn btn--sm" data-action="ovLinkStart">Link a folder</button></div>`;

// What opens under a backlog item: everything it says, and how it will run once it joins the queue.
// Model / account / mode / priority save as you tap; empty means the defaults (Claude Code, Auto, Accept edits, Normal).
function todoDetail(c, t) {
  const models = todoModels(c);
  const model = t.model_id || 'claude-code', mode = t.permission_mode || 'acceptEdits', pri = t.priority || 0;
  const label = s => `<span class="pal-label">${s}</span>`;
  const acct = accountField(c, { model, value: t.only_account, action: 'todoAccount', projectId: t.project_id });
  const onlyOn = acct ? `<div class="brow__acct" data-todo="${t.id}">${acct}</div>` : '';
  return `<div class="brow__detail">
    <div class="brow__full">${esc(t.text)}</div>
    ${sentStrip(t.images)}
    <span class="meta">Added ${esc(ago(t.created_at, c.now))}</span>
    ${label('MODEL')}<div class="pal-chips pal-chips--flush" data-noswipe>${models.map(m =>
      `<button class="pal-model${model === m.id ? ' is-on' : ''}" style="opacity:${m.ok ? 1 : 0.5}" data-action="todoSet" data-id="${t.id}" data-k="model" data-v="${esc(m.id)}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>
    ${onlyOn}
    ${label('MODE')}<div class="pal-chips pal-chips--flush" data-noswipe>${PERMISSION_MODES.map(m =>
      `<button class="pal-model${mode === m.id ? ' is-on' : ''}" title="${esc(m.sub)}" data-action="todoSet" data-id="${t.id}" data-k="permission_mode" data-v="${m.id}"><span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}</button>`).join('')}</div>
    ${label('PRIORITY')}<div class="segmented">${[[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']].map(([v, l]) =>
      `<button class="segmented__opt${pri === v ? (v === 1 ? ' is-urgent' : ' is-on') : ''}" data-action="todoSet" data-id="${t.id}" data-k="priority" data-v="${v}">${l}</button>`).join('')}</div>
    <div class="need__row"><button class="btn btn--ghost btn--38" data-action="openTodoEdit" data-id="${t.id}">${t.images && t.images.length ? 'Edit text or pictures' : 'Edit text'}</button><span class="meta">Used when this item is queued.</span></div>
  </div>`;
}

// Chat-style box for the backlog: pick the model and mode up front, and the item is parked (not run) with them.
function blogComposer(c, p) {
  const { ui } = c;
  const models = todoModels(c);
  const on = ui.blModel || 'auto';
  const cur = models.find(m => m.id === on) || models[0];
  const acct = usesClaudeAccount(c, on) ? accountShort(c, ui.blAccount, p.id) : '';
  const mode = modeInfo(ui.blMode);
  const text = ui.blText, busy = ui.polishing === 'blogInput';
  const menu = ui.blMenu === 'model' ? `<div class="menu-scrim" data-action="closeMenus"></div>
    <div class="menu menu--mode menu--up" role="menu"><span class="menu__label">RUN THIS ITEM ON</span>
    ${models.map(m => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${on === m.id}" data-action="blModel" data-id="${esc(m.id)}"${m.ok ? '' : ' aria-disabled="true"'}>
      <span class="dot" style="--c:${m.dot}"></span><span class="menu__two"><span class="menu__main">${esc(m.label)}</span></span><span class="menu__check">${on === m.id ? '✓' : ''}</span></button>`).join('')}
    ${accountMenuPart(c, { model: on, value: ui.blAccount, action: 'blAccount', projectId: p.id })}</div>` : '';
  return `<div class="blog-composer">
    ${menu}
    <span class="blog-composer__tag">Saving to backlog · won't run yet</span>
    ${imageStrip((ui.blogImages || {})[p.id], 'blog')}
    <div class="composer__box">
      <button class="ask__model composer__model" data-action="blModelMenu" title="Model and account this item will run on" aria-label="Model: ${esc(cur.label)}${acct ? ', account: ' + esc(acct) : ''}"><span class="dot" style="--c:${cur.dot};width:7px;height:7px"></span><span class="truncate">${esc(cur.label)}${acct ? ` · ${esc(acct)}` : ''}</span></button>
      <div class="segmented mode-seg composer__mode" role="group" aria-label="How Claude Code works on this item" data-noswipe>${PERMISSION_MODES.map(o => `<button class="segmented__opt${mode.id === o.id ? ' is-on' : ''}" title="${esc(o.label + ' - ' + o.sub)}" aria-pressed="${mode.id === o.id}" data-action="blMode" data-id="${o.id}">${esc(o.short)}</button>`).join('')}</div>
      ${FEATURES.images ? `<label class="tool-btn tool-btn--40" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="blog">${icon('image', 20)}</label>` : ''}
      <button class="tool-btn tool-btn--40" data-action="polish" data-target="blogInput" title="Polish text" aria-label="Polish text"${!text.trim() || busy ? ' disabled' : ''}>${busy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 18, { strokeWidth: 1.6 })}</button>
      <textarea id="blogInput" rows="1" placeholder="Save to backlog with a model and mode…" aria-label="Save to backlog" data-input="blogText" data-enter="submitBlog" data-autogrow="120" data-live-style>${esc(text)}</textarea>
      <button class="ask__send" data-action="submitBlog" aria-label="Save to backlog"${!text.trim() ? ' disabled' : ''}>↑</button>
    </div>
  </div>`;
}

// The parked todos for one project: add, reorder, edit, and queue them.
function backlogTab(c, p) {
  const { d, ui } = c;
  const queue = queuedTasks(d);
  const todos = (d.todos[p.id] || []).filter(t => t.status === 'pending');
  const sel = todos.filter(t => t.checked);
  const allSel = todos.length && sel.length === todos.length;
  const starred = todos.filter(t => t.important).length;

  return `<div class="section section--backlog" data-key="tab-backlog">
    <div class="section__head"><span class="section__title">Backlog <span class="section__count">${todos.length}</span>${starred ? `<span class="section__count brow__starcount" title="${plural(starred, 'important item')}">${icon('star', 12)}${starred}</span>` : ''}</span>${todos.length ? `<span class="section__acts"><button class="btn--text" data-action="backlogSelectAll" data-all="${allSel ? 1 : 0}">${allSel ? 'Clear' : 'Select all'}</button>${crmOnly(p) ? '' : '<button class="btn--text" data-action="backlogRun">Queue…</button>'}</span>` : ''}</div>
    ${todos.length ? `<div class="list list--backlog" data-drag-list="backlog" data-pid="${p.id}">${todos.map(t => { const open = !!ui.todoOpen[t.id]; return `<div class="qrow brow${open ? ' is-open' : ''}${t.important ? ' is-important' : ''}" data-id="${t.id}" data-key="b${t.id}">
        <span class="handle" aria-label="Drag to reorder">⋮⋮</span>
        <button class="check" role="checkbox" aria-checked="${t.checked ? 'true' : 'false'}" data-action="todoCheck" data-id="${t.id}" aria-label="Select">${t.checked ? '✓' : ''}</button>
        <span class="brow__title truncate" data-action="todoCheck" data-id="${t.id}">${esc(t.text)}</span>
        <span class="brow__acts">
          ${t.images && t.images.length ? `<span class="brow__pics meta" title="${plural(t.images.length, 'picture')} attached">${icon('image', 14)}${t.images.length > 1 ? t.images.length : ''}</span>` : ''}
          <button class="icon-btn icon-btn--ghost brow__star" aria-pressed="${t.important ? 'true' : 'false'}" data-action="todoStar" data-id="${t.id}" aria-label="${t.important ? 'Unmark important' : 'Mark important'}" title="${t.important ? 'Important - tap to unmark' : 'Mark important'}">${icon('star', 16)}</button>
          <button class="icon-btn icon-btn--ghost brow__toggle" aria-expanded="${open}" data-action="todoToggle" data-id="${t.id}" aria-label="${open ? 'Hide details' : 'Show details'}">${chevSVG}</button>
          <button class="icon-btn icon-btn--ghost brow__edit" data-action="openTodoEdit" data-id="${t.id}" aria-label="Edit">✎</button>
        </span>
        ${open ? todoDetail(c, t) : ''}
      </div>`; }).join('')}</div>` : ''}
    ${sel.length ? `<div class="bulkbar"><span class="bulkbar__text">${sel.length} selected · joins after ${plural(queue.length, 'task')}</span>${allSel ? '' : `<button class="bulkbar__all" data-action="backlogSelectAll" data-all="0">Select all</button>`}<button class="bulkbar__del" data-action="backlogDelete">Delete</button>${crmOnly(p) ? '' : '<button class="bulkbar__run" data-action="backlogRun">Run selected…</button>'}</div>` : ''}
    ${crmOnly(p) ? `<span class="section__hint">${NO_FOLDER_HINT} - then these can join the queue.</span>` : ''}
    <span class="section__hint">${todos.some(t => ui.todoOpen[t.id]) ? 'Close open items to drag them. ' : 'Drag to set the order. '}Selected items join the queue in this order.</span>
    ${blogComposer(c, p)}
  </div>`;
}

function work(c, p) {
  const { d, w, ui } = c;
  const pt = d.tasks.filter(t => t.project_id === p.id);
  const live = pt.filter(t => !t.dismissed && (w.apTask.has(t.id) || ['running', 'error', 'needs_review', 'rate_limited'].includes(t.status)));
  const queue = queuedTasks(d);
  const mine = queue.map((t, i) => ({ t, pos: i + 1 })).filter(x => x.t.project_id === p.id);
  const done = threadSummaries(d).filter(t => t.project_id === p.id && (t.status === 'done' || t.status === 'cancelled'))
    .sort((a, b) => finishedAt(b).localeCompare(finishedAt(a)));
  const todos = (d.todos[p.id] || []).filter(t => t.status === 'pending');

  const liveHTML = live.length ? `<div class="section"><div class="section__head"><span class="section__title">Live</span></div>
    ${live.map(t => {
      const st = w.uiStatus(t);
      const s = st === 'approval' ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(t);
      const a = w.apTask.get(t.id);
      const act = a ? `data-action="openApproval" data-id="${a.id}"` : `data-action="openChat" data-id="${t.id}"`;
      const meta = [ago(t.started_at || t.created_at, c.now), t.current_step && st === 'running' ? t.current_step : '', fmtCost(t.cost_usd)].filter(Boolean).join(' · ');
      return `<button class="live-card" ${act} style="--c:${s.color}" data-key="lv${t.id}"><span class="live-card__label">${s.label.toUpperCase()}</span><span class="live-card__title">${esc(taskName(t))}</span>${['running', 'rate_limited'].includes(t.status) ? progressBar(t, c.now) : ''}<span class="meta">${esc(meta)}</span></button>`;
    }).join('')}</div>` : '';


  const week = weekStats(c, p, pt);
  const running = pt.filter(t => t.status === 'running').length;
  const stat = (label, v, note, tone) => `<div class="pstat"><span class="pstat__l">${label}</span><span class="pstat__v"${tone ? ` style="color:${tone}"` : ''}>${v}</span><span class="pstat__n">${esc(note)}</span></div>`;
  const stats = `<div class="pstats pstats--side">
    ${stat('BACKLOG', todos.length, 'parked, not queued')}
    ${stat('UP NEXT', mine.length, mine.length ? 'waiting in line' : 'nothing waiting')}
    ${stat('RUNNING', running, running ? 'working now' : 'idle')}
    ${stat('DONE THIS WEEK', week.done, week.rate == null ? 'nothing yet' : week.rate + '% succeeded', 'var(--ok, #5fd39a)')}
  </div>`;

  const lzQ = lazy('proj-upnext-' + p.id, mine, 6, 6), lzD = lazy('proj-done-' + p.id, done, 6, 6);
  const upNext = `<div class="section"><div class="section__head"><span class="section__title">Up next <span class="section__count">${mine.length}</span></span></div>
    ${mine.length ? lazyScroll(`<div class="list">${lzQ.items.map(({ t, pos }) => `<div class="pq-row" data-action="openChat" data-id="${t.id}" data-key="pq${t.id}">
        <span class="pq-row__pos">#${pos}</span><span class="pq-row__main"><span class="truncate pq-row__title">${esc(taskName(t))}</span>${progressBar(t, c.now)}</span><span class="pq-row__meta">${chipFor(t)}<span class="meta">${priorityLabel(t.priority)}</span></span></div>`).join('')}</div>`, lzQ.more)
      : `<div class="pempty"><b>Nothing queued for this project.</b>${todos.length ? `<span>You have ${plural(todos.length, 'backlog item')} ready. Queue them in order, or ask for ideas.</span>` : ''}
        ${crmOnly(p) ? '' : `<div class="pempty__acts">${todos.length ? `<button class="btn btn--sm btn--accent" data-action="backlogRun">Queue backlog (${todos.length})</button>` : ''}${p.is_general ? '' : `<button class="btn btn--sm" data-action="suggestImprovements">Suggest tasks</button>`}</div>`}</div>`}
  </div>`;

  const doneBlock = `<div class="section"><div class="section__head"><span class="section__title">Done recently <span class="section__count">${week.done} this week</span></span><button class="btn--text" data-action="projectTab" data-tab="activity">All activity →</button></div>
    ${done.length ? lazyScroll(`<div class="list">${lzD.items.map(t => histRow(t, { project: false, key: 'pd' })).join('')}</div>`, lzD.more) : `<span class="section__empty">Nothing finished yet.</span>`}</div>`;

  const msg = running ? `Working on ${running === 1 ? 'a task' : running + ' tasks'} in ${p.name}.` : todos.length ? `All clear. ${plural(todos.length, 'backlog item')} ${todos.length === 1 ? 'is' : 'are'} ready when you are.` : 'All clear. Nothing waiting.';
  const octoCard = `<div class="card card--pad pside-octo"><div class="pside-octo__row">${octo(running ? 'run' : 'idle', 52)}<div><span class="pside-octo__state"><i class="dot" style="--c:${running ? 'var(--run)' : 'var(--ok, #5fd39a)'}"></i>${running ? 'WORKING' : 'IDLE'}</span><span class="pside-octo__msg">${esc(msg)}</span></div></div>
    ${todos.length && !crmOnly(p) ? `<button class="btn btn--block" data-action="backlogRun">Queue backlog in order</button>` : ''}</div>`;

  const max = Math.max(1, ...week.days.map(x => x.n));
  const chart = `<div class="card card--pad pside-week"><div class="pside-week__head"><b>This week</b><span class="meta">tasks done / day</span></div>
    <div class="pside-week__bars">${week.days.map((x, i) => `<div class="pside-week__col"><span class="pside-week__bar${i === 6 ? ' is-today' : ''}" style="height:${Math.max(6, Math.round(x.n / max * 100))}%" title="${x.n} done"></span><span class="meta">${x.l}</span></div>`).join('')}</div>
    <div class="pside-week__kpis"><div><b style="color:var(--ok, #5fd39a)">${week.rate == null ? '–' : week.rate + '%'}</b><span class="meta">succeeded</span></div><div><b>${week.avg == null ? '–' : mmss(week.avg / 1000)}</b><span class="meta">avg run</span></div></div>
    ${s0(c, p)}</div>`;

  const ph = c.d.phases[p.id];
  const phCard = ph && ph.phases.length ? `<div class="card card--pad pside-list"><div class="pside-list__head"><b>Phases</b><button class="btn--text" data-action="projectTab" data-tab="phases">Open</button></div>
    ${ph.phases.slice(0, 5).map(x => `<div class="pside-ph${x.status === 'active' ? ' is-active' : ''}"><span class="pside-ph__dot${x.status === 'done' ? ' is-done' : ''}">${x.status === 'done' ? '✓' : ''}</span><span class="pside-ph__name truncate">${esc(x.name)}</span><span class="meta">${x.counts ? x.counts.done : 0}/${x.total || 0}</span></div>`).join('')}</div>` : '';

  const pin = (c.d.notes[p.id] || [])[0];
  const pinCard = pin ? `<div class="card card--pad pside-list"><div class="pside-list__head"><b>Pinned note</b><button class="btn--text" data-action="projectTab" data-tab="notes">All notes · ${(c.d.notes[p.id] || []).length}</button></div>
    ${pin.text ? `<span class="pside-note">${esc(pin.text)}</span>` : ''}${sentStrip(pin.images)}<span class="meta">Octopus reads this before every run</span></div>` : '';

  return `<div class="proj-work" data-key="tab-work">
    <div class="proj-work__main">${crmOnly(p) ? noFolderCard('ask-nofolder', "Octopus can't work on this project yet") : ask(c)}${liveHTML}
      <div class="proj-work__two">${upNext}${doneBlock}</div></div>
    <aside class="proj-work__side">${stats}${pinCard}${chart}${phCard}</aside>
  </div>`;
}

// This week for one project: tasks done per day (oldest first, today last), success rate, average run time.
function weekStats(c, p, pt) {
  const day = 86400000, start = new Date(); start.setHours(0, 0, 0, 0);
  const days = Array.from({ length: 7 }, (_, i) => { const d0 = start.getTime() - (6 - i) * day; return { t: d0, n: 0, l: new Date(d0).toLocaleDateString('en', { weekday: 'narrow' }) }; });
  const since = start.getTime() - 6 * day;
  const wk = pt.filter(t => ['done', 'error'].includes(t.status) && new Date(t.finished_at || t.created_at).getTime() >= since);
  const ok = wk.filter(t => t.status === 'done');
  for (const t of ok) { const at = new Date(t.finished_at || t.created_at).getTime(); const d = days.find(x => at >= x.t && at < x.t + day); if (d) d.n++; }
  const durs = ok.map(t => t.duration_ms).filter(x => x != null && x > 0);
  return { days, done: ok.length, rate: wk.length ? Math.round(ok.length / wk.length * 100) : null, avg: durs.length ? durs.reduce((a, b) => a + b, 0) / durs.length : null };
}
const s0 = (c, p) => {
  const s = c.d.projectSettings[p.id];
  if (!s || !(s.budget_usd > 0)) return '';
  const pct = Math.min(100, Math.round((s.month_spend_usd || 0) / s.budget_usd * 100));
  return `<div class="pside-week__spend"><span>Spent this month <b>${money(s.month_spend_usd || 0)}</b></span><span class="meta">of ${money(s.budget_usd).replace(/\.00$/, '')}</span></div><div class="bar6"><div style="width:${pct}%;background:${pct > 80 ? 'var(--wait)' : 'var(--run)'}"></div></div>`;
};

// What you've told me about this project. Every task I run here starts with these in mind.
function notes(c, p) {
  const { d, ui } = c;
  const list = d.notes[p.id] || [];
  const kindOf = k => NOTE_KINDS.find(x => x[0] === k) || NOTE_KINDS[2];
  const meets = meetingNotes(d, p.id);
  return `<div class="notes split split--rev" data-key="tab-notes">
    <div class="section split__side notes__add">
      <div class="section__head"><span class="section__title">Project notes <span class="section__count">${list.length}</span></span></div>
      <span class="section__hint">Guidelines, requirements, anything I should know. I keep all of these in mind for every task on this project.</span>
      <form class="note-add" data-submit="addNote">
        <div class="chip-row" role="radiogroup" aria-label="Kind of note" data-noswipe>${NOTE_KINDS.map(([k, l, hint]) =>
          `<button type="button" class="chip${ui.noteKind === k ? ' is-on' : ''}" role="radio" aria-checked="${ui.noteKind === k}" title="${esc(hint)}" data-action="setNoteKind" data-kind="${k}" data-key="nk-${k}">${l}</button>`).join('')}</div>
        ${imageStrip((ui.noteImages || {})[p.id], 'note')}
        <div class="backlog-add"><textarea id="noteInput" class="input grow backlog-add__input" rows="3" placeholder="Add a ${kindOf(ui.noteKind)[1].toLowerCase()}… (Shift+Enter for a new line, paste a picture to attach it)" data-input="noteText" data-enter="submitNote" data-autogrow="260" aria-label="Add a project note">${esc(ui.noteText)}</textarea><div class="backlog-add__side"><button class="btn btn--48" type="submit">Add</button>${polishBtn('noteInput', ui.noteText)}${FEATURES.images ? `<label class="tool-btn polish-btn" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="note">${icon('image', 18)}</label>` : ''}</div></div>
      </form>
    </div>
    <div class="section split__main">
      ${list.length ? `<div class="list list--notes">${list.map(n => `<div class="qrow note-row" data-id="${n.id}" data-key="n${n.id}">
        <span class="note-row__kind note-row__kind--${esc(n.kind)}">${kindOf(n.kind)[1]}</span>
        <span class="note-row__text">${esc(n.text)}${sentStrip(n.images)}</span>
        <button class="icon-btn icon-btn--ghost brow__edit" data-action="openNoteEdit" data-id="${n.id}" aria-label="Edit note">✎</button>
      </div>`).join('')}</div>` : `<span class="section__empty">Nothing yet. Write the first one in the box.</span>`}
      ${meets.length ? `<div class="section__head"><span class="section__title">Meeting notes <span class="section__count">${meets.length}</span></span></div>
      <span class="section__hint">Notes written on this project's calendar items. Tap one to read and edit it full-page.</span>
      <div class="list list--notes">${meets.map(e => `<button class="qrow note-row note-row--meeting" data-action="openMeetingNotes" data-id="${e.id}" data-key="nm${e.id}">
        <span class="note-row__kind note-row__kind--meeting">Meeting notes</span>
        <span class="note-row__text"><b>${esc(e.title)}</b> <span class="meta">${esc(meetingWhen(e))}</span>\n${esc(e.notes.trim())}</span>
      </button>`).join('')}</div>` : ''}
    </div>
  </div>`;
}

const fmtBytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1).replace(/\.0$/, '')} MB`;

function documentsSkeleton(p) {
  const row = () => `<div class="sk-row sk-row--64">${sk('38px', 38, 12)}<div class="sk-col" style="gap:7px">${sk('55%', 12, 6)}${sk('32%', 9, 5)}</div></div>`;
  return `<div class="section" data-key="tab-documents" aria-busy="true" aria-label="Loading documents">
    <div class="section__head"><span class="section__title">Documents ${sk('22px', 14, 6, ';display:inline-block;vertical-align:middle')}</span></div>
    <span class="section__hint">Keep specs, contracts and reference files with ${esc(p.name)}. Its tasks can read them when they need to. Up to 100 MB each.</span>
    <div class="sk-list">${[0, 1, 2, 3].map(row).join('')}</div>
  </div>`;
}

// Files kept for this project: add, preview (tap), download, delete. Tasks for the project are told about them and can open them.
const docExt = x => (x.name.includes('.') ? x.name.split('.').pop().toLowerCase() : '');
const DOC_TYPES = [['all', 'All'], ['pdf', 'PDF'], ['image', 'Images'], ['text', 'Text'], ['other', 'Other']];
const docKind = x => {
  const e = docExt(x), m = x.mime || '';
  return e === 'pdf' || m === 'application/pdf' ? 'pdf' : m.startsWith('image/') ? 'image' : (m.startsWith('text/') || ['txt', 'md', 'csv', 'json', 'log'].includes(e)) ? 'text' : 'other';
};
const DOC_VIEWS = [['list', 'List'], ['compact', 'Compact'], ['grid', 'Thumbnails']];
const DOC_SORTS = [['new', 'Newest'], ['old', 'Oldest'], ['name', 'Name A–Z'], ['size', 'Largest']];

// Files kept for this project: add, search, sort, filter, rename, preview (tap), download, delete. Tasks for the project are told about them and can open them.
function documents(c, p) {
  const loading = c.d.documents[p.id] === undefined;   // not fetched yet (vs. fetched and empty)
  if (loading) return documentsSkeleton(p);
  const list = c.d.documents[p.id];
  const u = c.ui.docs, q = u.q.trim().toLowerCase();
  let shown = list.filter(x => (u.type === 'all' || docKind(x) === u.type) && (!q || x.name.toLowerCase().includes(q)));
  const by = { new: (a, b) => b.id - a.id, old: (a, b) => a.id - b.id, name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }), size: (a, b) => b.size - a.size };
  shown = [...shown].sort(by[u.sort] || by.new);
  const row = x => u.editId === x.id
    ? `<div class="qrow doc-row" data-key="d${x.id}">
        <input id="docRenameInput" class="input" maxlength="120" value="${esc(u.editName)}" data-input="doc.name" data-enter="docRenameSave" aria-label="File name" autocomplete="off" autocapitalize="off" spellcheck="false">
        <span class="doc-row__acts"><button class="btn btn--primary btn--sm" data-action="docRenameSave">Save</button><button class="btn btn--ghost btn--sm" data-action="docRenameCancel">Cancel</button></span>
      </div>`
    : `<div class="qrow doc-row" data-key="d${x.id}">
        <button class="doc-row__main" data-action="previewDoc" data-id="${x.id}" title="Preview"><span class="doc-row__name truncate">${esc(x.name)}</span><span class="meta">${esc(fmtBytes(x.size))} · added ${esc(ago(x.created_at, c.now))}</span></button>
        <span class="doc-row__acts"><button class="icon-btn icon-btn--ghost brow__edit" data-action="docRename" data-id="${x.id}" title="Rename" aria-label="Rename ${esc(x.name)}">${icon('edit', 16)}</button>
        <button class="icon-btn icon-btn--ghost brow__edit" data-action="docDownload" data-id="${x.id}" title="Download" aria-label="Download ${esc(x.name)}">${icon('download', 16)}</button>
        <button class="icon-btn icon-btn--ghost brow__edit" data-action="docDelete" data-id="${x.id}" aria-label="Delete ${esc(x.name)}">✕</button></span>
      </div>`;
  const tile = x => {
    const th = u.view === 'grid' && docKind(x) === 'image' ? docThumb(x) : '', ext = docExt(x).slice(0, 4).toUpperCase() || 'FILE';
    return `<div class="doc-tile" data-key="d${x.id}">
        <button class="doc-tile__main" data-action="previewDoc" data-id="${x.id}" title="Preview ${esc(x.name)}">
          <span class="doc-tile__thumb">${th ? `<img src="${th}" alt="">` : `<span class="doc-tile__ext">${esc(ext)}</span>`}</span>
          <span class="doc-tile__name truncate">${esc(x.name)}</span><span class="meta">${esc(fmtBytes(x.size))}</span>
        </button>
        <span class="doc-tile__acts"><button class="icon-btn icon-btn--ghost brow__edit" data-action="docRename" data-id="${x.id}" title="Rename" aria-label="Rename ${esc(x.name)}">${icon('edit', 16)}</button>
        <button class="icon-btn icon-btn--ghost brow__edit" data-action="docDownload" data-id="${x.id}" title="Download" aria-label="Download ${esc(x.name)}">${icon('download', 16)}</button>
        <button class="icon-btn icon-btn--ghost brow__edit" data-action="docDelete" data-id="${x.id}" aria-label="Delete ${esc(x.name)}">✕</button></span>
      </div>`;
  };
  const tools = list.length ? `<div class="doc-tools">
      <div class="search">${icon('search', 16, { strokeWidth: 1.8 })}<input id="docSearch" type="search" placeholder="Search documents" value="${esc(u.q)}" data-input="doc.q" aria-label="Search documents" autocomplete="off">${u.q ? `<button class="search__x" data-action="docSearchClear" aria-label="Clear search">✕</button>` : ''}</div>
      <span class="seg doc-view" role="radiogroup" aria-label="Document view" data-noswipe>${DOC_VIEWS.map(([k, l]) => `<button class="chip${u.view === k ? ' is-on' : ''}" role="radio" aria-checked="${u.view === k}" data-action="docView" data-v="${k}">${l}</button>`).join('')}</span>
      <select class="input doc-sort" data-doc-sort aria-label="Sort documents">${DOC_SORTS.map(([k, l]) => `<option value="${k}"${u.sort === k ? ' selected' : ''}>${l}</option>`).join('')}</select>
    </div>
    <div class="chip-row" data-noswipe>${DOC_TYPES.map(([k, l]) => `<button class="chip${u.type === k ? ' is-on' : ''}" data-action="docType" data-v="${k}">${l}</button>`).join('')}</div>` : '';
  return `<div class="section" data-key="tab-documents">
    <div class="section__head"><span class="section__title">Documents <span class="section__count">${shown.length === list.length ? list.length : `${shown.length}/${list.length}`}</span></span>
      <span class="section__acts"><button class="icon-btn icon-btn--ghost" data-action="docRefresh" title="Refresh" aria-label="Refresh documents">${icon('refresh', 16)}</button><button class="btn btn--ghost btn--38" data-action="docPick">Add documents</button></span></div>
    <input type="file" multiple hidden data-doc-file>
    <span class="section__hint">Keep specs, contracts and reference files with ${esc(p.name)}. Its tasks can read them when they need to. Up to 100 MB each.</span>
    ${tools}
    ${shown.length ? (u.view === 'grid' ? `<div class="doc-grid">${shown.map(x => (u.editId === x.id ? row(x) : tile(x))).join('')}</div>` : `<div class="list list--docs${u.view === 'compact' ? ' list--compact' : ''}">${shown.map(row).join('')}</div>`) : list.length ? `<span class="section__empty">No documents match.</span>` : `<span class="section__empty">No documents yet. Add the first one.</span>`}
  </div>`;
}

function activity(c, p) {
  const byId = new Map(c.d.tasks.map(t => [t.id, t]));
  const isStar = t => { const r = byId.get(t.thread_id || t.id); return !!(r && r.important); };
  const tasks = c.d.tasks.filter(t => t.project_id === p.id).sort((a, b) => (isStar(b) - isStar(a)) || b.id - a.id).slice(0, 60);
  if (!tasks.length && crmOnly(p)) return noFolderCard('tab-activity', 'No activity yet');
  if (!tasks.length) return emptyState({ key: 'tab-activity', title: 'No activity yet', body: 'Finished tasks for this project show up here.', label: 'Add a task', attrs: `data-action="openPalette" data-pid="${p.id}"` });
  const movable = tasks.filter(t => !['running', 'deleted'].includes(t.status));
  const picked = movable.filter(t => c.ui.actSel[t.id]);
  const allPicked = movable.length && picked.length === movable.length;
  const bar = `<div class="bulk bulk--act" data-key="act-bulk">
    <button class="btn--text" data-action="actSelectAll" data-ids="${allPicked ? '' : movable.map(t => t.id).join(',')}">${allPicked ? 'Clear' : 'Select all'}</button>
    ${picked.length ? `<span class="meta">${picked.length} selected</span>
    <button class="btn btn--ghost btn--sm" data-action="actMoveTo">Move…</button>
    <button class="btn btn--ghost btn--sm" data-action="actPriority">Priority…</button>
    <button class="btn btn--ghost btn--sm" data-action="actRerun">Re-run</button>
    <button class="btn btn--ghost btn--sm bulk__danger" data-action="actCancel">Cancel</button>
    <button class="btn btn--ghost btn--sm bulk__danger" data-action="actDelete">Delete</button>` : ''}</div>`;
  return `<div class="list list--act" data-key="tab-activity">${bar}${tasks.map(t => {
    const st = c.w.uiStatus(t);
    const s = st === 'approval' ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(t);
    const [g, gc] = glyph(t);
    const meta = ['#' + t.id, modelLabel(t.model_id), usedBy([t]).account, s.label, fmtCost(t.cost_usd), took(t)].filter(Boolean).join(' · ');
    const rootId = t.thread_id || t.id;
    const canPick = !['running', 'deleted'].includes(t.status), sel = !!c.ui.actSel[t.id];
    return `<div class="event-row${sel ? ' is-selected' : ''}" data-key="ev${t.id}" data-action="openChat" data-id="${t.id}">
      ${canPick ? `<button class="check" role="checkbox" aria-checked="${sel}" data-action="actCheck" data-id="${t.id}" aria-label="Select">${sel ? '✓' : ''}</button>` : '<span></span>'}
      <span class="row__glyph" style="color:${g === '·' ? s.color : gc}">${g === '·' ? '●' : g}</span>
      <div class="event-row__body"><span class="row__title">${esc(taskName(t))}</span><span class="meta">${acctDots([t])}${esc(meta)}</span></div>
      <span class="meta event-row__time">${esc(agoShort(t.finished_at || t.created_at, c.now))}</span>
      <button class="icon-btn icon-btn--ghost brow__star" aria-pressed="${isStar(t)}" data-action="chatStar" data-id="${t.id}" aria-label="${isStar(t) ? 'Unmark important' : 'Mark important'}" title="${isStar(t) ? 'Important - tap to unmark' : 'Mark important'}">${icon('star', 16)}</button>
      ${['running', 'deleted'].includes(t.status) ? '<span></span>' : `<button class="icon-btn icon-btn--ghost brow__edit" data-action="chatActions" data-id="${rootId}" title="Move to another project or delete" aria-label="Move or delete">⋯</button>`}
    </div>`;
  }).join('')}</div>`;
}

function recipes(c, p) {
  const list = c.d.recipes || [];
  if (crmOnly(p)) return noFolderCard('tab-recipes', 'Recipes run in a project folder');
  if (!list.length) return emptyState({ key: 'tab-recipes', title: 'No recipes yet', body: 'Recipes are saved prompts you can run here in two taps or put on a schedule.', label: 'Add a task', attrs: `data-action="openPalette" data-pid="${p.id}"` });
  return `<div class="recipe-grid" data-key="tab-recipes">${list.map(r => recipeCard(c, r, { here: p })).join('')}</div>`;
}

function settings(c, p) {
  const s = c.d.projectSettings[p.id];
  if (!s) return `<div class="empty" data-key="tab-settings">Loading settings…</div>`;
  const spent = s.month_spend_usd || 0;
  const pct = s.budget_usd > 0 ? Math.min(100, Math.round(spent / s.budget_usd * 100)) : 0;
  const notifs = [['notify_approvals', 'Approvals', 'Urgent push that breaks through Focus'], ['notify_failures', 'Failures', 'Push right away'],
    ['notify_completions', 'Completions', 'Quiet push for each one that finishes'], ['quiet_hours', 'Quiet hours 23:00 to 08:00', 'Hold everything except approvals']];
  const group = (key, title, body) => `<section class="section settings__group" data-key="${key}"><div class="section__head"><span class="section__title">${title}</span></div>${body}</section>`;
  return `<div class="settings settings--grid" data-key="tab-settings">
    ${group('pset-run', 'How tasks run', `${p.is_general ? '' : projectAccountCard(c, s)}
    <div class="card card--pad"><span class="card__title">Default priority</span>
      <div class="segmented segmented--start">${[[-1, 'Low'], [0, 'Normal'], [1, 'Urgent']].map(([v, l]) =>
        `<button class="segmented__opt${s.default_priority === v ? ' is-on' : ''}" data-action="setProjectSetting" data-k="default_priority" data-v="${v}">${l}</button>`).join('')}</div></div>
    <div class="card card--list"><div class="set-row"><div class="grow"><span class="set-row__label">Mark in progress automatically</span><span class="set-row__desc">When a new task under this project starts, set its status to In progress</span></div>${toggle(!!s.auto_in_progress, 'setProjectSetting', `data-k="auto_in_progress" data-v="${s.auto_in_progress ? 'false' : 'true'}" aria-label="Mark in progress automatically"`)}</div></div>`)}
    ${group('pset-notify', 'Notifications', `<div class="card card--list">${notifs.map(([k, l, desc]) => `<div class="set-row"><div class="grow"><span class="set-row__label">${l}</span><span class="set-row__desc">${desc}</span></div>${toggle(!!s[k], 'setProjectSetting', `data-k="${k}" data-v="${s[k] ? 'false' : 'true'}" aria-label="${l}"`)}</div>`).join('')}</div>`)}
    ${group('pset-perm', p.is_general ? 'Permissions' : 'Permissions and deploy', `<div class="card card--pad"><span class="card__title">Always allowed commands</span>
      <span class="card__desc">These run without asking. Add to the list with "Always allow" on an approval.</span>
      ${s.allow_commands.length ? `<div class="allow-chips">${s.allow_commands.map((cmd, i) => `<span class="allow-chip">${esc(cmd)}<button data-action="removeAllowed" data-i="${i}" aria-label="Remove ${esc(cmd)}">✕</button></span>`).join('')}</div>` : '<span class="card__desc">Nothing yet - everything that needs permission asks first.</span>'}</div>
    ${p.is_general ? '' : `<div class="card card--pad"><span class="card__title">Deploy targets</span>
      <span class="card__desc">Where the Deploy button in a chat ships this project (GitHub, an org, a server...). Say how in plain words.</span>
      ${(s.deploy_targets || []).length ? `<div class="allow-chips">${s.deploy_targets.map((t, i) => `<span class="allow-chip">${esc(t.name)}${t.instructions ? ` · ${esc(t.instructions)}` : ''}<button data-action="removeDeployTarget" data-i="${i}" aria-label="Remove ${esc(t.name)}">✕</button></span>`).join('')}</div>` : '<span class="card__desc">None yet - the Deploy button will bring you here.</span>'}
      <input id="deployName" class="input" placeholder="Name, e.g. GitHub" maxlength="60" autocomplete="off">
      <input id="deployHow" class="input" placeholder="How, e.g. push main to origin" maxlength="1000" autocomplete="off">
      <button class="btn btn--ghost btn--38" data-action="addDeployTarget">Add target</button></div>`}`)}
    ${group('pset-budget', 'Spending', `<div class="card card--pad">
      <div class="budget-head"><span class="card__title">Monthly budget</span><div class="stepper"><button data-action="budgetStep" data-d="-5" aria-label="Lower budget">−</button><span>${money(s.budget_usd).replace(/\.00$/, '')}</span><button data-action="budgetStep" data-d="5" aria-label="Raise budget">+</button></div></div>
      <div class="bar6"><div style="width:${pct}%;background:${spent / (s.budget_usd || 1) > 0.8 ? 'var(--wait)' : 'var(--run)'}"></div></div>
      <span class="card__desc">${money(spent)} API spend this month. At 100% I'll pause this project and ask before continuing.</span>
    </div>`)}
  </div>`;
}

export function project(c) {
  const p = c.project;
  if (!p) return `<div class="page page--narrow" data-key="page-project-missing"><div class="empty"><span>That project doesn't exist any more.</span><button class="btn btn--sm" data-action="go" data-view="projects">All projects</button></div></div>`;
  const { d, ui } = c;
  const tab = ui.projectTab === 'overview' && p.is_general ? 'work' : ui.projectTab;
  const s = d.projectSettings[p.id];
  const avatar = p.is_general ? `<span class="avatar avatar--general">${octopusSVG('idle')}</span>` : `<span class="avatar" style="background:${projectFill(p)}">${p.image ? '' : esc(p.name.charAt(0).toUpperCase())}</span>`;
  const body = tab === 'overview' ? overviewTab(c, p) : tab === 'backlog' ? backlogTab(c, p) : tab === 'todos' ? checklistTab(c, p) : tab === 'phases' ? phasesTab(c, p) : tab === 'timeline' ? timelineTab(c, p) : tab === 'notes' ? notes(c, p) : tab === 'documents' ? documents(c, p) : tab === 'activity' ? activity(c, p) : tab === 'recipes' ? recipes(c, p) : tab === 'settings' ? settings(c, p) : work(c, p);
  const stat = PROJECT_STATUSES.find(x => x[0] === p.status) || PROJECT_STATUSES[0];
  const count = { todos: openCount(d, p.id) || '', backlog: (d.todos[p.id] || []).filter(t => t.status === 'pending').length || '', phases: (d.phases[p.id] || {}).phases ? d.phases[p.id].phases.filter(x => x.status === 'done').length + '/' + d.phases[p.id].phases.length : '', notes: (d.notes[p.id] || []).length + meetingNotes(d, p.id).length || '', documents: (d.documents[p.id] || []).length || '', recipes: (d.recipes || []).length || '' };
  const statusPill = p.is_general ? '' : `<details class="pstatus" data-key="pstatus"><summary class="pstatus__pill"><span class="dot" style="--c:${stat[2]};width:7px;height:7px"></span>${stat[1]}${chevSVG}</summary>
      <div class="pstatus__menu" role="radiogroup" aria-label="Project status">${PROJECT_STATUSES.map(([k, l, col]) =>
        `<button class="pstatus__opt${p.status === k ? ' is-on' : ''}" role="radio" aria-checked="${p.status === k}" data-action="setProjectStatus" data-status="${k}"><span class="dot" style="--c:${col};width:7px;height:7px"></span>${l}</button>`).join('')}</div></details>`;
  return `<div class="page page--project page--project-wide" data-key="page-project-${p.id}">
    <div class="proj-head">${avatar}
      <div class="proj-head__text"><span class="proj-head__line"><span class="proj-head__name">${esc(p.name)}</span>${statusPill}</span>
        <span class="proj-head__path">${crmOnly(p) ? `<span class="crm-badge" title="${NO_FOLDER_HINT}">CRM only</span><button class="btn--text" data-action="ovLinkStart">Link a folder</button>` : esc(p.is_general ? 'no folder · answers only' : tildify(p.path))}${p.is_general ? '' : ` ${scopeSwitch(p.category, 'setCategory', [['work', 'Work'], ['personal', 'Personal']], 'proj-head__cat')}`}</span></div>
      <div class="proj-head__acts">${crmOnly(p) ? '<button class="btn btn--40" data-action="hideProject">Hide</button>' : `${p.is_general ? '' : `<button class="btn btn--40" data-action="suggestImprovements"${ui.busy.suggest ? ' disabled' : ''}>${ui.busy.suggest ? 'Reviewing…' : 'Suggest'}</button>`}${p.is_general ? '' : '<button class="btn btn--40" data-action="hideProject">Hide</button>'}<button class="btn btn--40" data-action="togglePause">${p.active ? 'Pause' : 'Resume'}</button><button class="btn btn--primary btn--40" data-action="openPalette" data-pid="${p.id}">+ Task</button>`}</div>
    </div>
    ${p.active ? '' : `<div class="paused-banner">${octo('sleep', 34)}Paused. Queued tasks for this project are skipped but kept. Resume and I'll pick them up in order.</div>`}
    <div class="tabs tabs--proj" data-noswipe>${TABS.filter(([k]) => !(k === 'overview' && p.is_general)).map(([k, l]) => `<button class="tabs__opt${tab === k ? ' is-on' : ''}" data-action="projectTab" data-tab="${k}">${l}${count[k] ? ` <span class="tabs__n">${count[k]}</span>` : ''}</button>`).join('')}<span class="tabs__sp"></span><button class="tabs__opt${tab === 'settings' ? ' is-on' : ''}" data-action="projectTab" data-tab="settings">${icon('settings', 15)} Settings</button></div>
    ${body}
  </div>`;
}
