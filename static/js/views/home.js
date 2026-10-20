// Home: hero, Ask Octopus, the Octopus panel (what's running now), rollups, needs you, rate-limit card, up next,
// scheduled, usage, models. Up next has a Completed tab beside it. Onboarding replaces it when there are no projects.

import { rollups } from '../api.js';
import { agoShort, hms, money, modelState, PERMISSION_MODES, modeInfo, usageSummary, priorityLabel, plural, taskName } from '../format.js';
import { projectById, projectColor, orderedProjects, threadSummaries, finishedAt, generalProject, crmOnly } from '../data.js';
import { commandOf, limitWindowWords } from '../derive.js';
import { octopusSVG } from '../octopus.js';
import { acctDots, foldTitle, folded, esc, octo, spinner, chipFor, icon, toggle, clock, dayWord, histRow, lazy, lazyScroll, laneColor, modelLabel, bulkBar, progressBar, emptyState } from '../ui.js';
import { sessionColor } from './shell.js';
import { accountById, shortName, accountPicker } from './accounts.js';
import { isAssistant, previewOf } from './assistant-card.js';
import { FEATURES } from '../features.js';
import { imageStrip } from '../attach.js';
import { octopusPanel, askControls, askAgentMenu, askVoiceStatus, askVoiceMenu } from './octopus-panel.js';
import { quickSection } from './quick.js';
import { comingUp } from './calendar.js';
import { followupsHome } from './crm-activity.js';
import { crmHomeCard } from './crm-dash.js';

const tildify = p => (p || '').replace(/^\/Users\/[^/]+/, '~');
const CADENCE = { once: 'Once', daily: 'Every day', weekdays: 'Weekdays' };
export const cadenceLabel = s => s.cadence === 'weekly'
  ? ['Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays', 'Sundays'][s.weekday ?? 0]
  : CADENCE[s.cadence] || s.cadence;
export const scheduleTarget = s => (s.project_id ? s.project_name || '?' : 'all projects');

export function scheduleRow(s, { compactMeta = true } = {}) {
  const time = s.next_run_at ? clock(s.next_run_at) : s.time_of_day;
  const meta = `${cadenceLabel(s)} · ${scheduleTarget(s)}${s.enabled && s.next_run_at ? ' · next ' + dayWord(s.next_run_at) : ' · off'}`;
  // Tapping the row (or the pencil) opens the edit sheet: what, where, when, or delete.
  return `<div class="sched-row" data-key="s${s.id}">
    <button class="sched-row__main" data-action="editSchedule" data-id="${s.id}" aria-label="Edit schedule: ${esc(s.label)}">
      <span class="sched-row__time">${esc(time)}</span>
      <span class="sched-row__body"><span class="sched-row__label truncate">${esc(s.label)}</span>${compactMeta ? `<span class="meta hide-compact">${esc(meta)}</span>` : `<span class="meta">${esc(meta)}</span>`}</span>
    </button>
    <button class="icon-btn icon-btn--ghost sched-row__edit" data-action="editSchedule" data-id="${s.id}" title="Edit" aria-label="Edit schedule">${icon('edit', 16)}</button>
    ${toggle(!!s.enabled, 'toggleSchedule', `data-id="${s.id}" aria-label="Schedule on"`, true)}
  </div>`;
}

// ---------------------------------------------------------------- hero
function hero(c) {
  const h = c.w.hero;
  const btn = a => {
    const cls = a.kind === 'approve' ? 'hero-btn hero-btn--approve' : a.kind === 'primary' ? 'hero-btn hero-btn--primary' : 'hero-btn';
    return `<button class="${cls}" data-action="${a.action}"${a.id != null ? ` data-id="${a.id}"` : ''}>${esc(a.label)}</button>`;
  };
  return `<section class="hero reveal" style="--i:0" data-key="hero">
    <div class="hero__octo">${octopusSVG(c.w.mood)}</div>
    <div class="hero__body">
      <span class="hero__label" style="--c:${h.color}"><span class="dot dot--pulse" style="--c:${h.color};width:7px;height:7px"></span>${esc(h.label)}</span>
      <span class="hero__line">${esc(h.line)}</span>
      ${h.sub ? `<span class="hero__sub hide-compact">${esc(h.sub)}</span>` : ''}
      ${h.actions.length ? `<div class="hero__actions">${h.actions.map(btn).join('')}</div>` : ''}
    </div>
  </section>`;
}

// ---------------------------------------------------------------- ask box
const chev = '<svg width="10" height="10" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5l4 4 4-4"/></svg>';

// Instant commands (lane control) aren't a model you can pick - they only label the tasks they answered.
export function modelStates(c) { return c.d.models.filter(m => m.lane !== 'control').map(m => modelState(m, c.now)); }

// On a phone the Ask box takes half the screen, so the page only gets a one-line bar; tapping it opens the full box
// at the top of the screen (askOverlay, mounted in app.js) with the same controls under the text.
export function ask(c) {
  if (c.desk) return askBox(c);
  const a = c.ui.ask, res = a.result;
  const status = a.open ? '' : askStatus(c, res);
  return `<section class="ask ask--bar" data-key="ask-bar">${status}
    <button class="ask-bar" data-action="askOpen" aria-label="Ask Octopus">
      <span class="ask-bar__octo" aria-hidden="true">${octopusSVG('idle')}</span>
      <span class="ask-bar__text${a.text.trim() ? ' has-text' : ''}">${a.sending ? 'Routing… picking a model' : a.text.trim() ? esc(a.text) : 'Ask Octopus anything…'}</span>
      ${a.sending ? spinner() : a.text.trim() || a.images.length ? '<span class="ask-bar__go" aria-hidden="true">↑</span>' : ''}
    </button>
  </section>`;
}

// Only where the page has an Ask bar (Home, a project's Work tab), so leaving the page doesn't leave the box hanging.
export function askOverlay(c) {
  const u = c.ui;
  if (c.desk || !u.ask.open || !(u.view === 'home' || (u.view === 'project' && u.projectTab === 'work'))) return '';
  return `<div class="ask-over" data-key="ask-over">
    <div class="ask-over__scrim" data-action="askClose"></div>
    <div class="ask-over__panel${u.ask.menu ? ' ask-over__panel--menu' : ''}" role="dialog" aria-label="Ask Octopus">
      <div class="ask-over__head"><span class="ask-over__title">Ask Octopus</span><button class="icon-btn" data-action="askClose" aria-label="Close">${icon('close', 18)}</button></div>
      ${askBox(c)}
    </div>
  </div>`;
}

function askStatus(c, res) {
  const a = c.ui.ask;
  let status = '';
  if (a.sending) status = `<div class="ask__status ask__status--routing">${octo('dance', 22)}Routing… picking a model</div>`;
  else if (res && res.error) status = `<div class="ask__status ask__status--error">Could not send: ${esc(res.error)}</div>`;
  else if (res && res.answer) status = `<div class="ask__status ask__status--answer">${octo('idle', 22)}<span class="ask__answer">${esc(res.answer)}</span></div>`;
  else if (res) status = `<div class="ask__status"><span>→</span><b class="lane-${res.lane}">${esc(res.model)}</b><span>·</span><span>${esc(res.why)}</span><span>·</span><button class="ask__open" data-action="openChat" data-id="${res.id}">open #${res.id}</button></div>`;
  return askVoiceStatus(c) || status;
}

function askBox(c) {
  const a = c.ui.ask, d = c.d;
  const ms = modelStates(c);
  const pick = a.model === 'auto' ? null : ms.find(m => m.id === a.model);
  const target = a.projectId == null ? null : projectById(a.projectId);
  const targetLabel = !target || target.is_general ? 'no project (General)' : 'in ' + target.name;
  const dis = a.sending ? ' disabled' : '';
  const mode = modeInfo(a.mode);
  const empty = !a.text.trim();
  const res = a.result;
  const polishBusy = c.ui.polishing === 'ask';
  let menu = '';
  if (a.menu === 'pick') {
    const opts = [{ id: 'auto', l: 'Auto - Octopus decides', sub: 'picks a model for each request', dot: 'var(--text)', ok: true },
      ...ms.map(m => ({ id: m.id, l: m.label + m.pickerSuffix, sub: m.status, dot: m.dot, ok: m.available || m.rate }))];
    menu = `<div class="menu-scrim" data-action="closeMenus"></div>
      <div class="menu menu--model menu--up" role="menu"><span class="menu__label">MODEL</span>
      ${opts.map(o => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${a.model === o.id}" data-action="askPick" data-id="${o.id}"${o.ok ? '' : ' aria-disabled="true"'}>
        <span class="dot" style="--c:${o.dot}"></span><span class="menu__two"><span class="menu__main">${esc(o.l)}</span><span class="menu__sub">${esc(o.sub)}</span></span><span class="menu__check">${a.model === o.id ? '✓' : ''}</span></button>`).join('')}
      </div>`;
  } else if (a.menu === 'target') {
    const gen = generalProject(d);
    const opts = [{ id: '', l: 'No project (General)', color: 'var(--p-general)', on: a.projectId == null || (gen && a.projectId === gen.id) },
      ...orderedProjects(d).filter(p => !p.is_general && !crmOnly(p)).map(p => ({ id: p.id, l: p.name, color: projectColor(p), on: a.projectId === p.id }))];
    menu = `<div class="menu-scrim" data-action="closeMenus"></div>
      <div class="menu menu--target menu--up" role="menu"><span class="menu__label">SEND TO</span>
      ${opts.map(o => `<button class="menu__opt" role="menuitemradio" aria-checked="${o.on}" data-action="askTarget" data-id="${o.id}"><span class="nav-proj__swatch" style="background:${o.color}"></span><span class="grow truncate">${esc(o.l)}</span><span class="menu__check">${o.on ? '✓' : ''}</span></button>`).join('')}
      </div>`;
  }
  if (a.menu === 'agent') menu = askAgentMenu(c);
  if (a.menu === 'voice') menu = askVoiceMenu(c);
  const status = askStatus(c, res);
  return `<section class="ask${c.desk ? ' reveal' : ''}" style="--i:30" data-key="ask">
    ${status}
    <div class="ask__wrap">
      <div class="ask__box${a.sending ? ' is-sending' : ''}${res && res.error ? ' is-error' : ''}">
        <textarea id="askInput" class="ask__input" rows="1" placeholder="Ask Octopus anything…" aria-label="Ask Octopus" data-input="ask" data-enter="submitAsk" data-autogrow="180" data-live-style${dis}>${esc(a.text)}</textarea>
        ${imageStrip(a.images, 'ask')}
        <div class="ask__controls">
          <button class="ask__model" data-action="askMenu" data-menu="pick" aria-label="Model"${dis}><span class="dot" style="--c:${pick ? pick.dot : 'var(--text)'};width:7px;height:7px"></span>${esc(pick ? pick.label : 'Auto')}${chev}</button>
          ${pick && pick.lane !== 'agent' ? '' : accountPicker(c, a.account, 'askAccount', a.sending)}
          ${pick && pick.lane !== 'agent' ? '' : `<div class="segmented mode-seg" role="group" aria-label="How Claude Code works" data-noswipe>${PERMISSION_MODES.map(o => `<button class="segmented__opt${mode.id === o.id ? ' is-on' : ''}" title="${esc(o.label + ' - ' + o.sub)}" aria-pressed="${mode.id === o.id}" data-action="askMode" data-id="${o.id}"${dis}>${esc(o.short)}</button>`).join('')}</div>`}
          ${FEATURES.images ? `<label class="tool-btn" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="ask"${dis}>${icon('image', 18)}</label>` : ''}
          <button class="tool-btn" data-action="polish" data-target="ask" title="Polish text" aria-label="Polish text"${empty || polishBusy || a.sending ? ' disabled' : ''}>${polishBusy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 18, { strokeWidth: 1.6 })}</button>
          ${askControls(c)}
          <button class="ask__target" data-action="askMenu" data-menu="target"${dis}>${esc(targetLabel)} ▾</button>
          <button class="ask__send" data-action="submitAsk" aria-label="Send"${empty || a.sending ? ' disabled' : ''}>${a.sending ? spinner() : '↑'}</button>
        </div>
      </div>
      ${menu}
    </div>
  </section>`;
}

// ---------------------------------------------------------------- rollups
function rollupsHTML(c) {
  const go = { total: 'data-action="go" data-view="activity" data-filter="all"', queue: 'data-action="go" data-view="projects" data-quick="queue"',
    done: 'data-action="go" data-view="activity" data-filter="done"', error: 'data-action="go" data-view="activity" data-filter="error"' };
  return `<div class="rollups reveal" style="--i:60" data-key="rollups">${rollups(c.d).map(r =>
    `<button class="rollup" ${go[r.key]}><span class="rollup__v" style="--c:${r.color}">${r.value}</span><span class="rollup__l">${r.label}</span></button>`).join('')}</div>`;
}

// ---------------------------------------------------------------- needs you
function needHead(kind, label, t) {
  const p = projectById(t.project_id);
  return `<div class="need__head"><span class="need__kind">${label}</span><span>·</span><span class="swatch-sq" style="background:${projectColor(p)}"></span><span class="need__project truncate">${esc(p ? p.name : '?')}</span>${chipFor(t)}<span class="need__ago">${esc(agoShort(kind === 'approval' ? t._since : finishedAt(t)))}</span></div>`;
}

export function riskChips(ctx) {
  if (!ctx) return '';
  const chips = [`<span class="risk-chip ${ctx.level >= 2 ? 'risk-chip--warn' : 'risk-chip--ok'}">${esc(ctx.risk)} risk</span>`,
    ...ctx.tags.map(t => `<span class="risk-chip">${esc(t)}</span>`),
    ...(ctx.is_git ? ['<span class="risk-chip risk-chip--ok">git repo</span>'] : []),
    ...(ctx.summary ? [`<span class="risk-chip">+${ctx.summary.added} −${ctx.summary.deleted} · ${plural(ctx.summary.files, 'file')}</span>`] : [])];
  return `<div class="risk-chips hide-compact">${chips.join('')}</div>`;
}

function approvalCard(c, n) {
  const a = n.approval, t = { ...n.task, _since: a.created_at };
  const p = projectById(t.project_id);
  const busy = c.ui.busy['ap' + a.id];
  const pa = isAssistant(a), pv = pa ? previewOf(a) : null;
  return `<div class="swipe" data-key="ap${a.id}">
    <div class="swipe__reveal" data-live-style><span></span><span></span></div>
    <div class="swipe__card need need--approval" data-swipe data-id="${a.id}" data-live-style>
      ${needHead('approval', 'APPROVE', t)}
      <span class="need__title">${esc(taskName(t))}</span>
      ${pa ? `<div class="pa-inline"><span class="pa-inline__title">${esc(pv ? pv.title : 'APPROVAL')}</span><span>${esc(commandOf(a))}</span></div>`
        : `<div class="cmd"><span><span class="cmd__prompt">$</span> ${esc(commandOf(a))}</span>${p ? `<span class="cmd__cwd">in ${esc(tildify(p.path))}</span>` : ''}</div>
      ${riskChips(c.d.approvalCtx[a.id])}`}
      <div class="need__actions">
        <button class="btn btn--ghost" data-action="openApproval" data-id="${a.id}">Details</button>
        <button class="btn btn--ghost" data-action="deny" data-id="${a.id}">Deny</button>
        <button class="btn btn--approve" data-action="approve" data-id="${a.id}"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Approving…' : `${pa && pv && pv.approve ? esc(pv.approve) : 'Approve'}<span class="btn__hint">${c.desk ? 'A' : 'or swipe →'}</span>`}</button>
      </div>
    </div>
  </div>`;
}

function errorCard(c, t) {
  const busy = c.ui.busy['rt' + t.id];
  return `<div class="need need--error" data-key="er${t.id}">
    ${needHead('error', 'FAILED', t)}
    <span class="need__title">${esc(taskName(t))}</span>
    <span class="need__err">${esc((t.error_message || 'Failed').slice(0, 400))}</span>
    <div class="need__row">
      ${crmOnly(projectById(t.project_id)) ? `<button class="btn btn--44" data-action="openProject" data-id="${t.project_id}" title="Link a folder to let Octopus work on this project">Link a folder</button>`
        : `<button class="btn btn--danger btn--44" data-action="retry" data-id="${t.id}"${busy ? ' disabled' : ''}>${busy ? '<span class="spinner spinner--err"></span>Retrying…' : 'Retry'}</button>
      <button class="btn btn--ghost btn--44" data-action="editRetry" data-id="${t.id}">Edit and retry</button>`}
      <button class="btn btn--ghost btn--44" data-action="openChat" data-id="${t.id}">Open chat</button>
      <button class="btn-dismiss" data-action="dismiss" data-id="${t.id}">Dismiss</button>
    </div>
  </div>`;
}

function reviewCard(c, t) {
  const note = t.result_summary || t.error_message || 'Octopus flagged this for a human look.';
  return `<div class="need need--review" data-key="rv${t.id}">
    ${needHead('review', 'REVIEW', t)}
    <span class="need__title">${esc(taskName(t))}</span>
    <span class="need__note hide-compact">${esc(note.slice(0, 300))}</span>
    <div class="need__row">
      <button class="btn btn--ghost btn--44" data-action="openChat" data-id="${t.id}">Open</button>
      <button class="btn btn--review btn--44" data-action="reviewed" data-id="${t.id}">Looks good</button>
    </div>
  </div>`;
}

function needsYou(c) {
  const needs = c.w.needs;
  if (!needs.length) return '';
  return `<section class="section section--needs reveal" style="--i:120" data-key="needs">
    <div class="section__head section__head--start"><span class="section__title">Needs you</span><span class="count-badge">${needs.length}</span></div>
    ${needs.map(n => n.kind === 'approval' ? approvalCard(c, n) : n.kind === 'error' ? errorCard(c, n.task) : reviewCard(c, n.task)).join('')}
  </section>`;
}

// ---------------------------------------------------------------- rate limited
function rateLimitedCard(c) {
  const t = c.w.rateLimited;
  if (!t || !t.resume_at) return '';
  const p = projectById(t.project_id);
  const acct = accountById(c, t.claude_account);
  const left = (new Date(t.resume_at).getTime() - c.now) / 1000;
  return `<section class="section reveal" style="--i:165" data-key="sleep">
    <div class="section__head"><span class="section__title">Now</span></div>
    <div class="sleep-card">
      <div class="need__head"><span class="need__kind" style="color:var(--sleep)">RATE-LIMITED</span><span>·</span><span class="need__project">${esc(p ? p.name : '?')}${acct ? ' · ' + esc(shortName(acct)) : ''}</span></div>
      <span class="need__title">${esc(taskName(t))}</span>
      ${progressBar(t, c.now)}
      <div class="sleep-card__timer"><span class="sleep-card__count">${hms(left)}</span><span class="meta">until ${clock(t.resume_at)}${t.current_step ? ' · paused at: ' + esc(t.current_step) : ''}</span></div>
      <div class="sleep-card__bar"></div>
      <span class="sleep-card__text">${esc(limitWindowWords(c.d, t.claude_account).full)} I'll resume this exact session on the same account automatically, so you don't need to do anything. Projects on other Claude accounts keep going.</span>
      <div class="need__row">
        <button class="btn btn--sleep btn--44" data-action="retry" data-id="${t.id}">Retry now</button>
        <button class="btn btn--ghost btn--44" data-action="openChat" data-id="${t.id}">Open chat</button>
      </div>
    </div>
  </section>`;
}

// ---------------------------------------------------------------- up next
export function upNext(c, tabs = '') {
  const { w } = c;
  if (w.empty) return '';
  const q = w.queue;
  let note = '', noteColor = 'var(--text-meta)';
  if (w.approval && q.length) { note = 'held · waiting on you'; noteColor = 'var(--wait)'; }
  else if (w.rateLimited && w.rateLimited.resume_at && q.length) {
    const ra = accountById(c, w.rateLimited.claude_account);   // only that account's tasks wait
    note = (ra ? shortName(ra) + ' ' : '') + 'held · until ' + clock(w.rateLimited.resume_at); noteColor = 'var(--sleep)';
  }
  else if (q.length) note = c.desk ? 'drag ⋮⋮ to reorder' : 'drag to reorder';
  const { items: shown, more } = lazy('upnext', q, 6, 6);
  const picked = q.filter(t => c.ui.qSel[t.id]);
  const allPicked = q.length && picked.length === q.length;
  const pickAll = q.length ? `<button class="btn--text" data-action="qSelectAll" data-ids="${allPicked ? '' : q.map(t => t.id).join(',')}">${allPicked ? 'Clear' : 'Select all'}</button>` : '';
  return `<section class="section reveal" style="--i:180" data-key="upnext">
    <div class="section__head${tabs ? ' section__head--tabs' : ''}">${tabs || `<span class="section__title">Up next <span class="section__count">${q.length || ''}</span></span>`}<span class="section__side"><span class="section__note" style="color:${noteColor}">${note}</span>${pickAll}</span></div>
    ${bulkBar(q, 'bulk-upnext')}
    ${q.length ? lazyScroll(`<div class="list list--queue" data-drag-list="queue">${shown.map((t, i) => {
      const p = projectById(t.project_id);
      const held = w.isPaused(t);
      const on = !!t.paused;
      const rest = priorityLabel(t.priority) + (t.thread_id ? ' · reply' : '') + (t.run_now ? ' · starting now' : '') + (on ? ' · paused' : '') + (held ? ' · skipped, project paused' : '');
      const sel = !!c.ui.qSel[t.id];
      return `<div class="qrow${c.ui.qOpen[t.id] ? ' is-open' : ''}${held ? ' is-held' : ''}${on ? ' is-paused' : ''}${sel ? ' is-selected' : ''}" data-id="${t.id}" data-key="q${t.id}" style="animation-delay:${Math.min(i, 8) * 45}ms">
        <span class="handle" aria-label="Drag to reorder">⋮⋮</span>
        <button class="check" role="checkbox" aria-checked="${sel}" data-action="qCheck" data-id="${t.id}" aria-label="Select">${sel ? '✓' : ''}</button>
        <span class="qrow__swatch" style="background:${projectColor(p)}"></span>
        <div class="qrow__body" data-action="qRowTap" data-id="${t.id}">
          <span class="qrow__title truncate">${esc(taskName(t))}</span>
          <div class="meta-row"><span class="task-num">#${t.id}</span><span class="nowrap">${esc(p ? p.name : '?')}</span>${acctDots([t])}${chipFor(t)}<span class="truncate">· ${esc(rest)}</span></div>
          ${progressBar(t, c.now)}
        </div>
        <div class="qrow__acts"><button class="qrow__chat" title="Open chat" aria-label="Open chat" data-action="openChat" data-id="${t.id}">${icon('messages', 16)}</button>${on ? `<button title="Play - put it back in line" aria-label="Play" data-action="playTask" data-id="${t.id}">${icon('play', 16)}</button>` : `<button title="Proceed now - start alongside what's running" aria-label="Proceed now" data-action="runNow" data-id="${t.id}">▶</button><button title="Pause - skip it until played" aria-label="Pause" data-action="pauseTask" data-id="${t.id}">${icon('pause', 16)}</button>`}<button title="Edit" aria-label="Edit" data-action="editTask" data-id="${t.id}">✎</button><button title="Move to top" aria-label="Move to top" data-action="toTop" data-id="${t.id}">↑</button><button title="Cancel" aria-label="Cancel" data-action="cancelQueued" data-id="${t.id}">✕</button><button title="Delete - move to the Recycle bin" aria-label="Delete" data-action="deleteQueued" data-id="${t.id}">${icon('trash', 16)}</button></div>
      </div>`;
    }).join('')}</div>`, more) : emptyState({ title: 'Your queue is empty', body: 'Tasks you queue here run one after another, even while you are away.', label: 'Add your first task', attrs: 'data-action="openPalette"' })}
    ${picked.length ? queueSelBar(picked) : ''}
  </section>`;
}

// Shown while queue rows are ticked: move them as a block, pause / resume them, cancel them, send them to another project, change their priority, or delete them (to the Recycle bin).
export function queueSelBar(picked) {
  const paused = picked.some(t => t.paused), playing = picked.some(t => !t.paused);
  const mv = (dir, label, glyph) => `<button class="qsel__move" data-action="qMove" data-dir="${dir}" title="${label}" aria-label="${label}">${glyph}</button>`;
  return `<div class="bulkbar qsel" data-key="qsel">
    <span class="bulkbar__text">${picked.length} selected</span>
    ${mv('top', 'Move to top', '⤒')}${mv('up', 'Move up', '↑')}${mv('down', 'Move down', '↓')}${mv('bottom', 'Move to bottom', '⤓')}
    ${playing ? `<button class="bulkbar__del" data-action="qPause">Pause</button>` : ''}
    ${paused ? `<button class="bulkbar__all" data-action="qResume">Resume</button>` : ''}
    <button class="bulkbar__all" data-action="qMoveTo">Move…</button>
    <button class="bulkbar__all" data-action="qPriority">Priority…</button>
    <button class="bulkbar__del" data-action="qCancel">Cancel</button>
    <button class="bulkbar__del qsel__danger" data-action="qDelete">Delete</button>
    <button class="qsel__move" data-action="qClear" title="Clear selection" aria-label="Clear selection">✕</button>
  </div>`;
}

// ---------------------------------------------------------------- right column
function scheduled(c) {
  const rows = c.w.schedules.slice(0, 3);
  return `<section class="section reveal" style="--i:120" data-key="scheduled">
    <div class="section__head">${foldTitle(c, 'scheduled', 'Scheduled')}<button class="btn--text" data-action="go" data-view="recipes">Manage</button></div>
    ${folded(c, 'scheduled') ? '' : rows.length ? `<div class="list">${rows.map(s => scheduleRow(s)).join('')}</div>` : emptyState({ title: 'Nothing scheduled', body: 'Schedules run a task at a set time, like a daily summary.', label: 'Schedule a task', attrs: `data-action="openPalette" data-when="daily"` })}
  </section>`;
}

export function spendBars(spend, big = false) {
  const days = (spend && spend.days) || [];
  const max = Math.max(0.0001, ...days.map(x => x.cost_usd));
  const letters = 'MTWTFSS';
  return days.map((x, i) => {
    const h = big ? Math.max(4, Math.round(x.cost_usd / max * 60)) : Math.max(3, Math.round(x.cost_usd / max * 34));
    const color = i === days.length - 1 ? 'var(--coral)' : 'var(--line-btn)';
    if (!big) return `<span class="spark" style="height:${h}px;background:${color}" title="${esc(x.date)} · ${money(x.cost_usd)}"></span>`;
    const dow = (new Date(x.date + 'T12:00:00').getDay() + 6) % 7;
    return `<div class="bigbar"><span style="height:${h}px;background:${color}" title="${money(x.cost_usd)}"></span><span class="bigbar__l">${letters[dow]}</span></div>`;
  }).join('');
}

function usage(c) {
  const { w, d } = c;
  const s = d.spend;
  const win = (label, u, color, resetText) => `<div class="usage-row">
      <div class="usage-row__head"><span>${label} <span class="mono" style="color:${color};font-weight:600">${u.pct}%</span></span><span class="meta">${resetText}</span></div>
      <div class="bar6"><div style="width:${u.pct}%;background:${color}"></div></div></div>`;
  const windows = w.session
    ? win('Session window', w.session, sessionColor(w.session.pct), w.session.resets ? 'resets ' + clock(w.session.resets.toISOString()) : 'reset')
      + (w.week ? win('This week', w.week, 'var(--text-meta)', w.week.resets ? 'resets ' + w.week.resets.toLocaleDateString([], { weekday: 'short' }) : 'reset') : '')
    : `<span class="usage-none">Claude usage shows up here after the next Claude Code run.</span>`;
  return `<section class="section reveal" style="--i:180" data-key="usage">
    <div class="section__head">${foldTitle(c, 'usage', 'Usage')}<button class="btn--text" data-action="openSheet" data-sheet="usage">Details</button></div>
    ${folded(c, 'usage') ? '' : `<div class="card usage-card">
      ${windows}
      <div class="usage-spend">
        <div class="usage-spend__txt"><span class="usage-spend__v">${money(s ? s.today_usd : 0)}</span><span class="usage-spend__l">API spend today · ${money(s ? s.month_usd : 0)} of ${money(s ? s.monthly_budget_usd : 100)} this month</span></div>
        <div class="sparks">${spendBars(s)}</div>
      </div>
    </div>`}
  </section>`;
}

// usageSummary() without the trailing " · " when a model has no cost (free tier).
const usageLine = u => (u && u.tasks && u.cost_usd == null ? `${u.tasks} tasks · ${Math.round(u.done / u.tasks * 100)}% ok` : usageSummary(u));

function models(c) {
  const ms = modelStates(c);
  return `<section class="section reveal" style="--i:210" data-key="models">
    <div class="section__head section__head--start">${foldTitle(c, 'models', 'Models <span class="section__count">· last 30 days</span>')}</div>
    ${folded(c, 'models') ? '' : `<div class="list">${ms.map(m => {
      const u = c.d.usage[m.id];
      const off = !m.available && !m.rate;
      return `<div class="model-row${m.rate ? ' is-rate' : ''}${off ? ' is-off' : ''}" data-key="m${esc(m.id)}">
        <span class="model-row__dot" style="--c:${m.dot};--halo:${m.rate ? 'rgba(165,139,255,.18)' : m.available ? 'rgba(111,207,151,.14)' : 'transparent'}"></span>
        <span class="model-row__name">${esc(m.label)}</span>
        <span class="model-row__usage${u && u.tasks ? '' : ' is-unused'}">${esc(usageLine(u))}</span>
        <span class="model-row__status">${esc(m.status)}</span>
      </div>`;
    }).join('') || '<div class="empty">No models configured.</div>'}</div>`}
  </section>`;
}

// Home's queue card: In queue | Completed tabs, so what just finished is one tap away instead of at the bottom.
function queueTabs(c) {
  const tab = c.ui.homeTab;
  const done = threadSummaries(c.d).filter(t => t.status === 'done' || t.status === 'cancelled')
    .sort((a, b) => finishedAt(b).localeCompare(finishedAt(a)));
  const midnight = new Date(c.now); midnight.setHours(0, 0, 0, 0);
  const today = done.filter(t => new Date(finishedAt(t)) >= midnight).length;
  const opt = (k, label, n) => `<button class="tabs__opt${tab === k ? ' is-on' : ''}" role="tab" aria-selected="${tab === k}" data-action="homeTab" data-tab="${k}">${label}${n ? `<span class="tabs__n">${n}</span>` : ''}</button>`;
  const tabs = `<div class="tabs" role="tablist" data-noswipe>${opt('queue', 'In queue', c.w.queue.length)}${opt('done', 'Completed', today ? today + ' today' : '')}</div>`;
  if (tab !== 'done') return upNext(c, tabs);
  const { items: rows } = lazy('home-done', done, 6, 6);
  const left = done.length - rows.length;
  const moreBtn = left > 0 ? `<button class="btn--text" data-action="lazyMore" data-lazy="home-done" data-first="6" data-step="6">Load ${Math.min(6, left)} more</button>` : '';
  return `<section class="section reveal" style="--i:180" data-key="completed">
    <div class="section__head section__head--tabs">${tabs}<span class="section__side">${moreBtn}<button class="btn--text" data-action="go" data-view="activity">All activity</button></span></div>
    ${rows.length ? lazyScroll(`<div class="list">${rows.map(t => histRow(t)).join('')}</div>`, '') : emptyState({ title: 'Nothing finished yet', body: 'Finished tasks and their results will collect here.', label: 'Add your first task', attrs: 'data-action="openPalette"' })}
  </section>`;
}

// ---------------------------------------------------------------- onboarding (no projects)
function onboarding(c) {
  const ob = c.ui.onboard, busy = c.ui.busy.addProject;
  const pushOn = c.d.settings && c.d.settings.push_configured;
  return `<div class="onboard" data-key="onboard">
    <div class="onboard__octo">${octo('sleep', 150)}</div>
    <span class="onboard__title">Give me something to hold.</span>
    <span class="onboard__text">Register a project folder. Queue tasks from your phone, the menu bar, or here, and I'll run Claude Code against it one task at a time while you're away.</span>
    <div class="onboard__steps">
      <form class="step step--on" data-submit="addProject">
        <div class="step__head"><span class="step__n">1</span><span class="step__t">Register a project</span></div>
        <input class="input" name="name" placeholder="Name, e.g. api-gateway" value="${esc(ob.name)}" data-input="onboard.name" autocomplete="off">
        <input class="input input--mono" name="path" placeholder="~/code/api-gateway" value="${esc(ob.path)}" data-input="onboard.path" autocomplete="off" autocapitalize="off" spellcheck="false">
        ${ob.error ? `<span class="form-err">${esc(ob.error)}</span>` : ''}
        <button class="btn btn--primary btn--46" type="submit"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Adding…' : 'Add project'}</button>
      </form>
      <div class="step step--off">
        <div class="step__head"><span class="step__n">2</span><span class="step__t">Queue a first task</span></div>
        <div class="step__chips"><span>Explain this codebase to me</span><span>Run tests</span><span>Morning status check</span></div>
      </div>
      <div class="step step--off step--row">
        <span class="step__n">3</span>
        <div class="grow"><span class="step__t">Get pinged</span><span class="meta">${pushOn ? 'ActivitySmith · connected' : 'set ACTIVITYSMITH_API_KEY in the launchd plist'}</span></div>
        ${pushOn ? `<button class="btn--text btn--muted" data-action="testPush">Send test push</button>` : ''}
      </div>
    </div>
  </div>`;
}

export function home(c) {
  if (c.w.empty) return `<div class="page page--onboard" data-key="page-onboard">${onboarding(c)}</div>`;
  return `<div class="page page--home" data-key="page-home">
    <div class="page__col">${hero(c)}${octopusPanel(c)}${rollupsHTML(c)}${needsYou(c)}${rateLimitedCard(c)}${queueTabs(c)}</div>
    <div class="page__col">${crmHomeCard(c)}${comingUp(c)}${followupsHome(c)}${scheduled(c)}${quickSection(c)}${usage(c)}${models(c)}</div>
    <div class="page__dock">${ask(c)}</div>
  </div>`;
}

export { laneColor, modelLabel };
