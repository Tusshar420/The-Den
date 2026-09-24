// Home: hero, Ask Octopus, rollups, needs you, running now, rate-limit card, up next,
// scheduled, usage, models, recent. Onboarding replaces it when there are no projects.

import { rollups } from '../api.js';
import { agoShort, startedAgo, hms, money, modelState, usageSummary, priorityLabel, plural } from '../format.js';
import { projectById, projectColor, orderedProjects, threadSummaries, finishedAt, generalProject } from '../data.js';
import { commandOf, limitWindowWords } from '../derive.js';
import { octopusSVG } from '../octopus.js';
import { esc, octo, spinner, chipFor, icon, toggle, clock, dayWord, histRow, laneColor, modelLabel } from '../ui.js';
import { sessionColor } from './shell.js';
import { FEATURES } from '../features.js';

const tildify = p => (p || '').replace(/^\/Users\/[^/]+/, '~');
const CADENCE = { once: 'Once', daily: 'Every day', weekdays: 'Weekdays' };
export const cadenceLabel = s => s.cadence === 'weekly'
  ? ['Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays', 'Sundays'][s.weekday ?? 0]
  : CADENCE[s.cadence] || s.cadence;
export const scheduleTarget = s => (s.project_id ? s.project_name || '?' : 'all projects');

export function scheduleRow(s, { compactMeta = true } = {}) {
  const time = s.next_run_at ? clock(s.next_run_at) : s.time_of_day;
  const meta = `${cadenceLabel(s)} · ${scheduleTarget(s)}${s.enabled && s.next_run_at ? ' · next ' + dayWord(s.next_run_at) : ' · off'}`;
  return `<div class="sched-row" data-key="s${s.id}">
    <span class="sched-row__time">${esc(time)}</span>
    <div class="sched-row__body"><span class="sched-row__label truncate">${esc(s.label)}</span>${compactMeta ? `<span class="meta hide-compact">${esc(meta)}</span>` : `<span class="meta">${esc(meta)}</span>`}</div>
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

export function modelStates(c) { return c.d.models.map(m => modelState(m, c.now)); }

function ask(c) {
  const a = c.ui.ask, d = c.d;
  const ms = modelStates(c);
  const pick = a.model === 'auto' ? null : ms.find(m => m.id === a.model);
  const target = a.projectId == null ? null : projectById(a.projectId);
  const targetLabel = !target || target.is_general ? 'no project (General)' : 'in ' + target.name;
  const dis = a.sending ? ' disabled' : '';
  const empty = !a.text.trim();
  const res = a.result;
  const polishBusy = c.ui.polishing === 'ask';
  let menu = '';
  if (a.menu === 'pick') {
    const opts = [{ id: 'auto', l: 'Auto - Octopus decides', sub: 'picks a model for each request', dot: 'var(--text)', ok: true },
      ...ms.map(m => ({ id: m.id, l: m.label + m.pickerSuffix, sub: m.status, dot: m.dot, ok: m.available || m.rate }))];
    menu = `<div class="menu-scrim" data-action="closeMenus"></div>
      <div class="menu menu--model" role="menu"><span class="menu__label">MODEL</span>
      ${opts.map(o => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${a.model === o.id}" data-action="askPick" data-id="${o.id}"${o.ok ? '' : ' aria-disabled="true"'}>
        <span class="dot" style="--c:${o.dot}"></span><span class="menu__two"><span class="menu__main">${esc(o.l)}</span><span class="menu__sub">${esc(o.sub)}</span></span><span class="menu__check">${a.model === o.id ? '✓' : ''}</span></button>`).join('')}
      </div>`;
  } else if (a.menu === 'target') {
    const gen = generalProject(d);
    const opts = [{ id: '', l: 'No project (General)', color: 'var(--p-general)', on: a.projectId == null || (gen && a.projectId === gen.id) },
      ...orderedProjects(d).filter(p => !p.is_general).map(p => ({ id: p.id, l: p.name, color: projectColor(p), on: a.projectId === p.id }))];
    menu = `<div class="menu-scrim" data-action="closeMenus"></div>
      <div class="menu menu--target" role="menu"><span class="menu__label">SEND TO</span>
      ${opts.map(o => `<button class="menu__opt" role="menuitemradio" aria-checked="${o.on}" data-action="askTarget" data-id="${o.id}"><span class="nav-proj__swatch" style="background:${o.color}"></span><span class="grow truncate">${esc(o.l)}</span><span class="menu__check">${o.on ? '✓' : ''}</span></button>`).join('')}
      </div>`;
  }
  let status = '';
  if (a.sending) status = `<div class="ask__status ask__status--routing">${octo('dance', 22)}Routing… picking a model</div>`;
  else if (res && res.error) status = `<div class="ask__status ask__status--error">Could not send: ${esc(res.error)}</div>`;
  else if (res) status = `<div class="ask__status"><span>→</span><b class="lane-${res.lane}">${esc(res.model)}</b><span>·</span><span>${esc(res.why)}</span><span>·</span><button class="ask__open" data-action="openChat" data-id="${res.id}">open #${res.id}</button></div>`;
  return `<section class="ask reveal" style="--i:30" data-key="ask">
    <div class="ask__wrap">
      <div class="ask__box${a.sending ? ' is-sending' : ''}${res && res.error ? ' is-error' : ''}">
        <textarea id="askInput" class="ask__input" rows="1" placeholder="🐙 Ask Octopus anything…" aria-label="Ask Octopus" data-input="ask" data-enter="submitAsk" data-autogrow="180" data-live-style${dis}>${esc(a.text)}</textarea>
        <div class="ask__controls">
          <button class="ask__model" data-action="askMenu" data-menu="pick" aria-label="Model"${dis}><span class="dot" style="--c:${pick ? pick.dot : 'var(--text)'};width:7px;height:7px"></span>${esc(pick ? pick.label : 'Auto')}${chev}</button>
          ${FEATURES.images ? `<label class="tool-btn" aria-label="Attach image"><input type="file" accept="image/*" multiple hidden>${icon('image', 18)}</label>` : ''}
          <button class="tool-btn" data-action="polish" data-target="ask" title="Polish text" aria-label="Polish text"${empty || polishBusy || a.sending ? ' disabled' : ''}>${polishBusy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 18, { strokeWidth: 1.6 })}</button>
          <button class="ask__target" data-action="askMenu" data-menu="target"${dis}>${esc(targetLabel)} ▾</button>
          <button class="ask__send" data-action="submitAsk" aria-label="Send"${empty || a.sending ? ' disabled' : ''}>${a.sending ? spinner() : '↑'}</button>
        </div>
      </div>
      ${menu}
    </div>
    ${status}
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
  return `<div class="swipe" data-key="ap${a.id}">
    <div class="swipe__reveal" data-live-style><span></span><span></span></div>
    <div class="swipe__card need need--approval" data-swipe data-id="${a.id}" data-live-style>
      ${needHead('approval', 'APPROVE', t)}
      <span class="need__title">${esc(t.prompt)}</span>
      <div class="cmd"><span><span class="cmd__prompt">$</span> ${esc(commandOf(a))}</span>${p ? `<span class="cmd__cwd">in ${esc(tildify(p.path))}</span>` : ''}</div>
      ${riskChips(c.d.approvalCtx[a.id])}
      <div class="need__actions">
        <button class="btn btn--ghost" data-action="openApproval" data-id="${a.id}">Details</button>
        <button class="btn btn--ghost" data-action="deny" data-id="${a.id}">Deny</button>
        <button class="btn btn--approve" data-action="approve" data-id="${a.id}"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Approving…' : `Approve<span class="btn__hint">${c.desk ? 'A' : 'or swipe →'}</span>`}</button>
      </div>
    </div>
  </div>`;
}

function errorCard(c, t) {
  const busy = c.ui.busy['rt' + t.id];
  return `<div class="need need--error" data-key="er${t.id}">
    ${needHead('error', 'FAILED', t)}
    <span class="need__title">${esc(t.prompt)}</span>
    <span class="need__err">${esc((t.error_message || 'Failed').slice(0, 400))}</span>
    <div class="need__row">
      <button class="btn btn--danger btn--44" data-action="retry" data-id="${t.id}"${busy ? ' disabled' : ''}>${busy ? '<span class="spinner spinner--err"></span>Retrying…' : 'Retry'}</button>
      <button class="btn btn--ghost btn--44" data-action="openChat" data-id="${t.id}">Open chat</button>
      <button class="btn-dismiss" data-action="dismiss" data-id="${t.id}">Dismiss</button>
    </div>
  </div>`;
}

function reviewCard(c, t) {
  const note = t.result_summary || t.error_message || 'Octopus flagged this for a human look.';
  return `<div class="need need--review" data-key="rv${t.id}">
    ${needHead('review', 'REVIEW', t)}
    <span class="need__title">${esc(t.prompt)}</span>
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

// ---------------------------------------------------------------- running now
function runningNow(c) {
  const rows = c.w.running;
  return `<section class="section reveal" style="--i:150" data-key="running">
    <div class="section__head section__head--start"><span class="section__title">Running now</span><span class="section__count">${rows.length || ''}</span></div>
    ${rows.length ? `<div class="running">${rows.map(t => {
      const p = projectById(t.project_id);
      const agent = t.lane !== 'api';
      const waiting = c.w.apTask.has(t.id);
      const step = waiting ? 'Waiting for your OK' : t.current_step;
      return `<button class="running__row" data-action="openChat" data-id="${t.id}" data-key="r${t.id}">
        <span class="spinner spinner--run${agent ? '' : ' spinner--api'}"></span>
        <span class="running__body">
          <span class="running__title truncate">${esc(t.prompt)}</span>
          <span class="meta-row"><span class="nowrap">${esc(p ? p.name : '?')}</span>${chipFor(t)}<span class="truncate">· ${t.started_at ? startedAgo(t.started_at, c.now) : 'starting'}</span></span>
          ${agent && step ? `<span class="running__step truncate hide-compact">› ${esc(step)}</span>` : ''}
        </span>
      </button>`;
    }).join('')}</div>` : `<div class="empty">Nothing running</div>`}
  </section>`;
}

// ---------------------------------------------------------------- rate limited
function rateLimitedCard(c) {
  const t = c.w.rateLimited;
  if (!t || !t.resume_at) return '';
  const p = projectById(t.project_id);
  const left = (new Date(t.resume_at).getTime() - c.now) / 1000;
  return `<section class="section reveal" style="--i:165" data-key="sleep">
    <div class="section__head"><span class="section__title">Now</span></div>
    <div class="sleep-card">
      <div class="need__head"><span class="need__kind" style="color:var(--sleep)">RATE-LIMITED</span><span>·</span><span class="need__project">${esc(p ? p.name : '?')}</span></div>
      <span class="need__title">${esc(t.prompt)}</span>
      <div class="sleep-card__timer"><span class="sleep-card__count">${hms(left)}</span><span class="meta">until ${clock(t.resume_at)}${t.current_step ? ' · paused at: ' + esc(t.current_step) : ''}</span></div>
      <div class="sleep-card__bar"></div>
      <span class="sleep-card__text">${esc(limitWindowWords(c.d).full)} I'll resume this exact session automatically, so you don't need to do anything.</span>
      <div class="need__row">
        <button class="btn btn--sleep btn--44" data-action="retry" data-id="${t.id}">Retry now</button>
        <button class="btn btn--ghost btn--44" data-action="openChat" data-id="${t.id}">Open chat</button>
      </div>
    </div>
  </section>`;
}

// ---------------------------------------------------------------- up next
function upNext(c) {
  const { w } = c;
  if (w.empty) return '';
  const q = w.queue;
  let note = '', noteColor = 'var(--text-meta)';
  if (w.approval && q.length) { note = 'held · waiting on you'; noteColor = 'var(--wait)'; }
  else if (w.rateLimited && w.rateLimited.resume_at && q.length) { note = 'held · until ' + clock(w.rateLimited.resume_at); noteColor = 'var(--sleep)'; }
  else if (q.length) note = c.desk ? 'drag ⋮⋮ to reorder' : 'drag to reorder';
  return `<section class="section reveal" style="--i:180" data-key="upnext">
    <div class="section__head"><span class="section__title">Up next <span class="section__count">${q.length || ''}</span></span><span class="section__note" style="color:${noteColor}">${note}</span></div>
    ${q.length ? `<div class="list list--queue" data-drag-list="queue">${q.map((t, i) => {
      const p = projectById(t.project_id);
      const held = w.isPaused(t);
      const rest = priorityLabel(t.priority) + (t.thread_id ? ' · reply' : '') + (held ? ' · skipped, project paused' : '');
      return `<div class="qrow${held ? ' is-held' : ''}" data-id="${t.id}" data-key="q${t.id}" style="animation-delay:${Math.min(i, 8) * 45}ms">
        <span class="handle" aria-label="Drag to reorder">⋮⋮</span>
        <span class="qrow__swatch" style="background:${projectColor(p)}"></span>
        <div class="qrow__body" data-action="openChat" data-id="${t.id}">
          <span class="qrow__title truncate">${esc(t.prompt)}</span>
          <div class="meta-row"><span class="nowrap">${esc(p ? p.name : '?')}</span>${chipFor(t)}<span class="truncate">· ${esc(rest)}</span></div>
        </div>
        <div class="qrow__acts"><button title="Move to top" aria-label="Move to top" data-action="toTop" data-id="${t.id}">↑</button><button title="Cancel" aria-label="Cancel" data-action="cancelQueued" data-id="${t.id}">✕</button></div>
      </div>`;
    }).join('')}</div>` : `<div class="empty"><span>Queue's empty.</span><button class="btn btn--sm" data-action="openPalette">Queue something</button></div>`}
  </section>`;
}

// ---------------------------------------------------------------- right column
function scheduled(c) {
  const rows = c.w.schedules.slice(0, 3);
  return `<section class="section reveal" style="--i:120" data-key="scheduled">
    <div class="section__head"><span class="section__title">Scheduled</span><button class="btn--text" data-action="go" data-view="recipes">Manage</button></div>
    ${rows.length ? `<div class="list">${rows.map(s => scheduleRow(s)).join('')}</div>` : `<div class="empty"><span>Nothing scheduled.</span><button class="btn btn--sm" data-action="go" data-view="recipes">Recipes</button></div>`}
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
    <div class="section__head"><span class="section__title">Usage</span><button class="btn--text" data-action="openSheet" data-sheet="usage">Details</button></div>
    <div class="card usage-card">
      ${windows}
      <div class="usage-spend">
        <div class="usage-spend__txt"><span class="usage-spend__v">${money(s ? s.today_usd : 0)}</span><span class="usage-spend__l">API spend today · ${money(s ? s.month_usd : 0)} of ${money(s ? s.monthly_budget_usd : 100)} this month</span></div>
        <div class="sparks">${spendBars(s)}</div>
      </div>
    </div>
  </section>`;
}

// usageSummary() without the trailing " · " when a model has no cost (free tier).
const usageLine = u => (u && u.tasks && u.cost_usd == null ? `${u.tasks} tasks · ${Math.round(u.done / u.tasks * 100)}% ok` : usageSummary(u));

function models(c) {
  const ms = modelStates(c);
  return `<section class="section reveal" style="--i:210" data-key="models">
    <div class="section__head section__head--start"><span class="section__title">Models <span class="section__count">· last 30 days</span></span></div>
    <div class="list">${ms.map(m => {
      const u = c.d.usage[m.id];
      const off = !m.available && !m.rate;
      return `<div class="model-row${m.rate ? ' is-rate' : ''}${off ? ' is-off' : ''}" data-key="m${esc(m.id)}">
        <span class="model-row__dot" style="--c:${m.dot};--halo:${m.rate ? 'rgba(165,139,255,.18)' : m.available ? 'rgba(111,207,151,.14)' : 'transparent'}"></span>
        <span class="model-row__name">${esc(m.label)}</span>
        <span class="model-row__usage${u && u.tasks ? '' : ' is-unused'}">${esc(usageLine(u))}</span>
        <span class="model-row__status">${esc(m.status)}</span>
      </div>`;
    }).join('') || '<div class="empty">No models configured.</div>'}</div>
  </section>`;
}

function recent(c) {
  const rows = threadSummaries(c.d).filter(t => t.status === 'done' || t.status === 'cancelled')
    .sort((a, b) => finishedAt(b).localeCompare(finishedAt(a))).slice(0, 4);
  return `<section class="section reveal" style="--i:240" data-key="recent">
    <div class="section__head"><span class="section__title">Recent</span><button class="btn--text" data-action="go" data-view="activity">All activity</button></div>
    ${rows.length ? `<div class="list">${rows.map(t => histRow(t)).join('')}</div>` : `<div class="empty">Nothing finished yet.</div>`}
  </section>`;
}

// ---------------------------------------------------------------- onboarding (no projects)
function onboarding(c) {
  const ob = c.ui.onboard, busy = c.ui.busy.addProject;
  const topic = c.d.settings && c.d.settings.ntfy_topic;
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
        <div class="grow"><span class="step__t">Get pinged</span><span class="meta">${topic ? 'ntfy topic · ' + esc(topic) : 'set NTFY_TOPIC in the launchd plist'}</span></div>
        ${topic ? `<button class="btn--text btn--muted" data-action="testPush">Send test push</button>` : ''}
      </div>
    </div>
  </div>`;
}

export function home(c) {
  if (c.w.empty) return `<div class="page page--onboard" data-key="page-onboard">${onboarding(c)}</div>`;
  return `<div class="page page--home" data-key="page-home">
    <div class="page__col">${hero(c)}${ask(c)}${rollupsHTML(c)}${needsYou(c)}${runningNow(c)}${rateLimitedCard(c)}${upNext(c)}</div>
    <div class="page__col">${scheduled(c)}${usage(c)}${models(c)}${recent(c)}</div>
  </div>`;
}

export { laneColor, modelLabel };
