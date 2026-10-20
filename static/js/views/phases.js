// A project's Phases tab (see phases.py): ordered stages of work, each with its own to-do list, that
// Octopus works through on its own - when a phase's dates come up, when the usage limit has room, or
// when you press Start. Also the add / edit sheet and the report sheet.

import { plural } from '../format.js';
import { esc, spinner, polishLabel, toggle } from '../ui.js';
import { icon } from '../icons.js';
import { crmOnly, NO_FOLDER_HINT } from '../data.js';

const MODES = [
  ['manual', 'On my command', 'Octopus only works on a phase after you press Start.'],
  ['timeline', 'By the timeline', 'A phase starts on its start date, in order.'],
  ['capacity', 'When the limit allows', 'Phases run back to back whenever your Claude usage has room.'],
];
const STATUS = {
  planned: ['Planned', 'var(--queued)'], active: ['In progress', 'var(--run)'],
  paused: ['Paused', 'var(--sleep)'], done: ['Done', 'var(--ok)'],
};
const TODO_STATE = {
  pending: ['○', 'var(--text-faint)', 'Waiting'], queued: ['◔', 'var(--queued)', 'In the queue'], running: ['●', 'var(--run)', 'Running'],
  rate_limited: ['◔', 'var(--sleep)', 'Waiting for the limit'], done: ['✓', 'var(--ok)', 'Done'], error: ['✕', 'var(--err)', 'Failed'],
  needs_review: ['!', 'var(--err)', 'Needs a look'], cancelled: ['–', 'var(--cancelled)', 'Cancelled'],
};
const dayLabel = s => new Date(...s.split('-').map((n, i) => (i === 1 ? n - 1 : Number(n)))).toLocaleDateString([], { month: 'short', day: 'numeric' });

// "Jun 3 → Jun 20", "from Jun 3", "until Jun 20"
function dates(ph) {
  if (ph.start_date && ph.end_date) return `${dayLabel(ph.start_date)} → ${dayLabel(ph.end_date)}`;
  if (ph.start_date) return `from ${dayLabel(ph.start_date)}`;
  if (ph.end_date) return `until ${dayLabel(ph.end_date)}`;
  return '';
}

function autopilotCard(c, p, v) {
  // A CRM-only project's phases are a plan to keep, not work Octopus can pick up.
  if (crmOnly(p)) return `<div class="card card--pad ph-auto crm-nofolder" data-key="ph-auto"><span class="card__title">Octopus autopilot</span>
    <span class="card__desc">${NO_FOLDER_HINT}. Until then phases are for planning: dates, goals, to-dos and Mark done all work.</span>
    <button class="btn btn--sm" data-action="ovLinkStart">Link a folder</button></div>`;
  const a = v.autopilot, cap = a.capacity || {};
  const room = cap.ok === false ? `<span class="ph-room ph-room--wait">Holding: ${esc(cap.reason)}</span>`
    : `<span class="ph-room">${cap.weekly_pct != null ? `Weekly limit ${cap.weekly_pct}% used. ` : ''}Room to work.</span>`;
  return `<div class="card card--pad ph-auto" data-key="ph-auto">
    <span class="card__title">Octopus autopilot</span>
    <div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Autopilot" data-noswipe>${MODES.map(([k, l]) =>
      `<button class="chip${a.mode === k ? ' is-on' : ''}" role="radio" aria-checked="${a.mode === k}" data-action="autopilotMode" data-mode="${k}" data-key="apm-${k}">${l}</button>`).join('')}</div>
    <span class="card__desc">${esc(MODES.find(m => m[0] === a.mode)[2])}</span>
    ${a.mode === 'manual' ? '' : `<div class="ph-ceiling"><span class="card__desc">Hold work when the weekly limit passes</span>
      <div class="stepper"><button data-action="ceilingStep" data-d="-5" aria-label="Lower ceiling">−</button><span>${a.ceiling}%</span><button data-action="ceilingStep" data-d="5" aria-label="Raise ceiling">+</button></div></div>
      ${room}`}
    <span class="meta">One item at a time. If one fails, Octopus stops the phase and tells you instead of piling on more.</span>
  </div>`;
}

// "Run chunk P1.4. Cart, checkout and test payment from docs/CHUNKS.md." -> P1.4 · Cart, checkout and test payment.
const CHUNK = /^(run|finish|prepare)\s+chunk\s+([A-Za-z]{0,3}\.?\d+(?:\.\d+)*)\.?\s*\(?(.*)$/i;
function itemLabel(text) {
  const first = (text || '').trim().split('\n')[0];
  const m = first.match(CHUNK);
  if (m) {
    const title = m[3].split(/,?\s+(?:from\s+)?docs\/|\s\(|\)\.|\.\s/)[0].replace(/[).:]+$/, '').trim();
    return { verb: m[1].toLowerCase(), code: m[2], title: title || first };
  }
  const sentence = first.split(/(?<=[.!?])\s/)[0];
  return { verb: '', code: '', title: sentence.length > 110 ? sentence.slice(0, 108).trimEnd() + '…' : sentence };
}

// A final report as plain text: markdown emphasis and heading marks dropped, line breaks kept.
const plainText = s => (s || '').replace(/\r/g, '').replace(/\*\*|__|`/g, '').replace(/^#{1,6}\s*/gm, '').trim();

// The opening paragraph of the report - the basic idea of what was done.
function gist(report) {
  const para = plainText(report).split(/\n\s*\n/).map(x => x.trim()).find(Boolean) || '';
  if (para.length <= 320) return para;
  const cut = para.slice(0, 320);
  return cut.slice(0, Math.max(cut.lastIndexOf(' '), 200)).trimEnd() + '…';
}

// The Den's summary ("What was done:", "Where it stands:" sections) split into its parts.
function summaryParts(text) {
  const parts = {}, order = [];
  let key = 'intro';
  for (const line of text.split('\n')) {
    const h = line.match(/^([A-Z][A-Za-z' ]{2,40}):\s*(.*)$/);
    if (h) { key = h[1].toLowerCase(); order.push(key); parts[key] = h[2] ? [h[2]] : []; continue; }
    (parts[key] = parts[key] || []).push(line);
  }
  const get = k => (parts[k] || []).join('\n').trim();
  return { done: plainText(get('what was done') || text), stands: plainText(get('where it stands')) };
}

const todoState = t => (t.status === 'pending' ? 'pending' : t.task_status === 'deleted' ? 'cancelled' : (t.task_status || t.status));

function todoLine(c, t, upNext) {
  const state = todoState(t);
  const [g, col, label] = TODO_STATE[state] || TODO_STATE.pending;
  const lb = itemLabel(t.text);
  const st = (c.ui.phItem || {})[t.id] || {};
  const tag = upNext && state === 'pending' ? 'Up next' : state === 'pending' ? '' : label;
  const head = `<button class="ph-todo__head" data-action="phaseItem" data-id="${t.id}" data-part="open" aria-expanded="${!!st.open}">
      <span class="ph-todo__g" style="color:${col}" aria-hidden="true">${g}</span>
      <span class="ph-todo__t${state === 'done' ? ' is-done' : ''}">${lb.code ? `<span class="ph-todo__code">${esc(lb.code)}</span>` : ''}${esc(lb.title)}${lb.verb === 'finish' ? '<span class="ph-todo__verb">finish</span>' : ''}</span>
      ${tag ? `<span class="ph-todo__state" style="--c:${upNext && state === 'pending' ? 'var(--run)' : col}">${esc(tag)}</span>` : ''}
      <span class="ph-todo__chev${st.open ? ' is-open' : ''}" aria-hidden="true">${icon('chevR', 14)}</span>
    </button>`;
  if (!st.open) return `<div class="ph-todo${state === 'running' ? ' is-running' : ''}" data-key="pht-${t.id}">${head}</div>`;

  const report = t.task_result_summary || '';
  let body = '';
  if (state === 'done') {
    const sum = (c.ui.phSum || {})[t.task_id] || {};
    const parts = sum.text ? summaryParts(sum.text) : null;
    const did = parts
      ? `<div class="ph-did"><span class="ph-did__label">What was done</span><p class="ph-did__text">${esc(parts.done)}</p></div>
         ${parts.stands ? `<div class="ph-did"><span class="ph-did__label">Where it stands</span><p class="ph-did__text">${esc(parts.stands)}</p></div>` : ''}`
      : report
        ? `<div class="ph-did"><span class="ph-did__label">What was done</span><p class="ph-did__text">${esc(gist(report))}</p>
            ${sum.loading ? `<span class="ph-did__none">${spinner()} Summarising in plain words…</span>` : ''}</div>`
        : `<span class="ph-did__none">Finished. Octopus didn't leave a report for this one.</span>`;
    const full = report && st.full ? `<div class="ph-did"><span class="ph-did__label">Full report</span><p class="ph-did__text">${esc(plainText(report))}</p></div>` : '';
    body = did + full + (report ? `<button class="ph-link ph-link--start" data-action="phaseItem" data-id="${t.id}" data-part="full">${st.full ? 'Hide full report' : 'Show full report'}</button>` : '');
  } else if (state === 'error' || state === 'needs_review') {
    body = `<div class="ph-did ph-did--err"><span class="ph-did__label">What went wrong</span><p class="ph-did__text">${esc(plainText(t.task_error_message || report) || 'No details were recorded.')}</p></div>`;
  } else if (state === 'running') {
    body = `<span class="ph-did__none">Octopus is working on this now.</span>`;
  } else if (state === 'pending') {
    body = `<span class="ph-did__none">${upNext ? 'Runs next, as soon as the item before it is done.' : 'Waiting its turn. Items run top to bottom, one at a time.'}</span>`;
  }
  const instr = st.instr ? `<p class="ph-instr">${esc(t.text)}</p>` : '';
  return `<div class="ph-todo is-open${state === 'running' ? ' is-running' : ''}" data-key="pht-${t.id}">${head}
    <div class="ph-todo__body">${body}${instr}
      <div class="ph-todo__acts">
        <button class="ph-link" data-action="phaseItem" data-id="${t.id}" data-part="instr">${st.instr ? 'Hide instructions' : 'Show instructions'}</button>
        ${t.task_id ? `<button class="ph-link" data-action="openChat" data-id="${t.task_id}">Open chat #${t.task_id}</button>` : ''}
      </div>
    </div>
  </div>`;
}

function phaseCard(c, p, ph, i, last) {
  const [sl, sc] = STATUS[ph.status] || STATUS.planned;
  const toggled = (c.ui.phOpen || {})[ph.id];
  const open = toggled === undefined ? ph.status === 'active' : !!toggled;
  const todos = (c.d.todos[p.id] || []).filter(t => ph.todo_ids.includes(t.id));
  const states = todos.map(todoState);
  const nextIdx = ph.status === 'active' && !states.some(x => ['queued', 'running', 'rate_limited'].includes(x)) ? states.indexOf('pending') : -1;
  const running = states.filter(x => ['queued', 'running', 'rate_limited'].includes(x)).length;
  const waiting = states.filter(x => x === 'pending').length;
  const busy = c.ui.busy[`ph${ph.id}`];
  const left = ph.days_left == null ? '' : ph.late ? `${-ph.days_left}d late` : ph.days_left === 0 ? 'due today' : `${ph.days_left}d left`;
  const count = ph.total
    ? [`${ph.counts.done} of ${ph.total} done`, running ? `${running} running` : '', waiting && ph.status !== 'done' ? `${waiting} waiting` : ''].filter(Boolean).join(' · ')
    : ph.status === 'done' ? 'completed' : 'nothing in it yet';
  const meta = [dates(ph), left, count].filter(Boolean).join(' · ');
  const act = ph.status === 'active'
    ? `<button class="btn btn--sm" data-action="phaseDo" data-id="${ph.id}" data-verb="pause"${busy ? ' disabled' : ''}>Pause</button>`
    : ph.status === 'done'
      ? `<button class="btn btn--ghost btn--sm" data-action="phaseDo" data-id="${ph.id}" data-verb="reopen"${busy ? ' disabled' : ''}>Reopen</button>`
      : crmOnly(p) ? `<button class="btn btn--primary btn--sm" disabled title="${NO_FOLDER_HINT}">${ph.status === 'paused' ? 'Resume' : 'Start'}</button>`
      : `<button class="btn btn--primary btn--sm" data-action="phaseDo" data-id="${ph.id}" data-verb="start"${busy ? ' disabled' : ''}>${busy ? spinner() : ph.status === 'paused' ? 'Resume' : 'Start'}</button>`;
  return `<div class="card card--pad ph${ph.late || ph.failed ? ' ph--warn' : ''}" data-key="ph-${ph.id}">
    <div class="ph__head">
      <span class="ph__num">${i + 1}</span>
      <button class="ph__title" data-action="phaseToggle" data-id="${ph.id}" aria-expanded="${open}"><span class="ph__name">${esc(ph.name)}</span><span class="meta">${esc(meta)}</span></button>
      <span class="ph__chev${open ? ' is-open' : ''}" aria-hidden="true">${icon('chevR', 16)}</span>
      <span class="ph__status" style="--c:${sc}">${sl}</span>
    </div>
    <div class="bar6"><div style="width:${ph.status === 'done' ? 100 : ph.progress}%;background:${ph.late ? 'var(--err)' : ph.status === 'done' ? 'var(--ok)' : 'var(--run)'}"></div></div>
    ${ph.blocked_note ? `<span class="ph__note${ph.failed ? ' ph__note--err' : ''}">${esc(ph.blocked_note)}</span>` : ''}
    <div class="ph__acts">${act}
      ${ph.status === 'done' ? '' : `<button class="btn btn--ghost btn--sm" data-action="phaseDo" data-id="${ph.id}" data-verb="complete" title="Close this phase as it is">Mark done</button>`}
      ${ph.status === 'done' ? '' : `<button class="btn btn--ghost btn--sm" data-action="openGen" data-id="${ph.id}" title="Have a model write this phase's tasks from its goal and description">Generate tasks</button>`}
      <button class="btn btn--ghost btn--sm" data-action="openPhase" data-id="${ph.id}">Edit</button>
      <button class="btn btn--ghost btn--sm" data-action="openReport" data-id="${ph.id}" title="A progress report on this phase">Report</button>
      <span class="ph__move"><button class="icon-btn" data-action="phaseMove" data-id="${ph.id}" data-d="-1" aria-label="Move earlier"${i === 0 ? ' disabled' : ''}>↑</button><button class="icon-btn" data-action="phaseMove" data-id="${ph.id}" data-d="1" aria-label="Move later"${last ? ' disabled' : ''}>↓</button></span>
    </div>
    ${open ? `<div class="ph__todos">${ph.description ? `<div class="ph__goal"><span class="ph-did__label">Description</span><span class="card__desc">${esc(ph.description)}</span></div>` : ''}${ph.goal ? `<div class="ph__goal"><span class="ph-did__label">Goal</span><span class="card__desc">${esc(ph.goal)}</span></div>` : ''}${todos.length
      ? todos.map((t, k) => todoLine(c, t, k === nextIdx)).join('')
      : `<span class="section__empty">${ph.status === 'done' ? 'Finished before its items were tracked here. The goal above lists what it covered.' : 'No items yet. Press Generate tasks, or edit the phase to add some.'}</span>`}</div>` : ''}
  </div>`;
}

export function phasesTab(c, p) {
  const v = c.d.phases[p.id];
  if (!v) return `<div class="empty" data-key="tab-phases">${spinner()} Loading phases…</div>`;
  const loose = (c.d.todos[p.id] || []).filter(t => v.unassigned.includes(t.id));
  const open = v.phases.filter(ph => ph.status !== 'done');
  return `<div class="phases split" data-key="tab-phases">
    <div class="split__main">
    <div class="section">
      <div class="section__head"><span class="section__title">Phases <span class="section__count">${v.phases.length}</span></span>
        <span class="ph-head-acts">${v.phases.length ? `<button class="btn btn--ghost btn--sm" data-action="openReport" title="A progress report on the project or some of its phases">Report</button>` : ''}<button class="btn btn--sm" data-action="openPhase">+ Phase</button></span></div>
      ${v.phases.length ? `<div class="ph-list">${v.phases.map((ph, i) => phaseCard(c, p, ph, i, i === v.phases.length - 1)).join('')}</div>`
        : `<div class="empty"><span>Split this project into stages, put the to-dos in each, and let Octopus work through them in order.</span><button class="btn btn--primary btn--sm" data-action="openPhase">Add the first phase</button></div>`}
      ${v.phases.length && !open.length ? `<span class="section__hint">Every phase is done.</span>` : ''}
    </div></div>
    <div class="split__side">
    ${autopilotCard(c, p, v)}
    ${loose.length && v.phases.length ? `<div class="section"><div class="section__head"><span class="section__title">Not in a phase <span class="section__count">${loose.length}</span></span><span class="section__note">pick where each belongs</span></div>
      <div class="list">${loose.map(t => `<div class="ph-loose"><span class="ph-loose__t">${esc(t.text)}</span><span class="chip-row chip-row--wrap" data-noswipe>${v.phases.filter(ph => ph.status !== 'done').map(ph =>
        `<button class="chip chip--sm" data-action="assignPhase" data-id="${t.id}" data-phase="${ph.id}">${esc(ph.name)}</button>`).join('')}</span></div>`).join('')}</div></div>` : ''}
    <span class="section__hint">${plural(open.length, 'phase')} to go. Octopus keeps each task inside its phase's goal and sees the whole plan.</span>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- add / edit sheet
export function phaseSheet(c, wrap) {
  const e = c.ui.phEdit;
  if (!e) return '';
  const title = e.id ? 'Edit phase' : 'Add a phase';
  const head = `<div class="sheet__head sheet__head--flush"><span class="sheet__title">${title}</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>`;
  const label = s => `<span class="pal-label">${s}</span>`;
  return wrap('phase', `<div class="sheet__body sheet__body--form">
    ${head}
    <input id="phName" class="input" maxlength="120" placeholder="e.g. Design the checkout flow" data-input="ph.name" aria-label="Phase name" value="${esc(e.name)}">
    ${polishLabel('GOAL', 'phGoal', e.goal, { kind: 'note' })}<textarea id="phGoal" class="input edit-prompt" rows="2" maxlength="2000" data-input="ph.goal" data-autogrow="160" aria-label="Goal" placeholder="What does done look like? Octopus keeps to this.">${esc(e.goal)}</textarea>
    ${label('DESCRIPTION')}<textarea id="phDesc" class="input edit-prompt" rows="3" maxlength="4000" data-input="ph.description" data-autogrow="220" aria-label="Description" placeholder="What this phase covers, in more detail. Used when generating its tasks.">${esc(e.description)}</textarea>
    ${label('DATES (OPTIONAL)')}<div class="ev-when">
      <input class="input" type="date" data-input="ph.start_date" aria-label="Start date" value="${esc(e.start_date)}">
      <input class="input" type="date" data-input="ph.end_date" aria-label="End date" value="${esc(e.end_date)}">
    </div>
    ${label(e.id ? 'ADD MORE ITEMS' : 'TO-DO ITEMS')}<textarea id="phTodos" class="input edit-prompt" rows="5" maxlength="8000" data-input="ph.todos" data-autogrow="260" aria-label="To-do items" placeholder="One item per line. They run top to bottom, one at a time.">${esc(e.todos)}</textarea>
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="savePhase"${e.saving || !e.name.trim() ? ' disabled' : ''}>${e.saving ? spinner() + 'Saving…' : e.id ? 'Save' : 'Add phase'}</button>
      ${e.id ? `<button class="btn-dismiss" data-action="deletePhase">Delete</button>` : ''}
    </div>
  </div>`, title);
}

// ---------------------------------------------------------------- report sheet
// c.ui.phReport: { pid, ids (empty = whole project), overview, loading, result, error } - see phases.report.

// The report's markdown (headings, "- " bullets, **bold**, paragraphs) as HTML.
function reportHTML(md) {
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  let html = '', list = false;
  for (const line of md.split('\n')) {
    const h = line.match(/^(#{1,3}) (.*)$/), li = line.match(/^- (.*)$/);
    if (!li && list) { html += '</ul>'; list = false; }
    if (h) html += `<h${h[1].length + 2} class="ph-rep__h${h[1].length}">${inline(h[2])}</h${h[1].length + 2}>`;
    else if (li) { if (!list) { html += '<ul>'; list = true; } html += `<li>${inline(li[1])}</li>`; }
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  return html + (list ? '</ul>' : '');
}

export function reportSheet(c, wrap) {
  const r = c.ui.phReport;
  if (!r) return '';
  const list = (c.d.phases[r.pid] || {}).phases || [];
  const whole = !r.ids.length;
  const chip = (on, label, id) => `<button class="chip chip--sm${on ? ' is-on' : ''}" aria-pressed="${on}" data-action="reportPick"${id ? ` data-id="${id}"` : ''}>${label}</button>`;
  let out = '';
  if (r.loading) out = `<div class="sum-wait">${spinner()}<span>Putting the report together…</span></div>`;
  else if (r.error) out = `<span class="form-err">${esc(r.error)}</span>`;
  else if (r.result) {
    out = `<div class="need__row">
        <button class="btn btn--38" data-action="reportCopy">Copy</button>
        <button class="btn btn--ghost btn--38" data-action="reportDownload">Download .md</button>
        ${typeof navigator !== 'undefined' && navigator.share ? `<button class="btn btn--ghost btn--38" data-action="reportShare">Share</button>` : ''}
      </div>
      ${r.result.overview_note ? `<span class="meta">${esc(r.result.overview_note)}</span>` : ''}
      <div class="ph-rep">${reportHTML(r.result.markdown)}</div>`;
  }
  return wrap('report', `<div class="sheet__body sheet__body--form">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">Report</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <span class="pal-label">COVERS</span>
    <div class="chip-row chip-row--wrap" role="group" aria-label="What the report covers" data-noswipe>
      ${chip(whole, 'Whole project')}${list.map((ph, i) => chip(r.ids.includes(ph.id), `${i + 1}. ${esc(ph.name)}`, ph.id)).join('')}
    </div>
    <div class="ph-rep__opt"><span class="ph-rep__optT"><span>Write an overview with AI</span><span class="meta">A few sentences on top about where things stand.</span></span>${toggle(r.overview, 'reportOverview', 'aria-label="Write an overview with AI"')}</div>
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="reportMake"${r.loading ? ' disabled' : ''}>${r.loading ? spinner() + 'Generating…' : r.result ? 'Generate again' : 'Generate report'}</button>
    </div>
    ${out}
  </div>`, 'Report');
}

// ---------------------------------------------------------------- generate tasks sheet
// c.ui.phGen: { id, models (null while loading), model ('' = cheapest usable), loading, error, added }
export function generateSheet(c, wrap) {
  const g = c.ui.phGen;
  if (!g) return '';
  const ph = ((c.d.phases[c.ui.projectId] || {}).phases || []).find(x => x.id === g.id);
  if (!ph) return '';
  const chip = (id, label, on, off) => `<button class="chip${on ? ' is-on' : ''}" role="radio" aria-checked="${on}" data-action="genModel" data-model="${esc(id)}"${off ? ' disabled' : ''}>${label}</button>`;
  const models = g.models === null ? `<span class="meta">${spinner()} Loading models…</span>`
    : `<div class="chip-row chip-row--wrap" role="radiogroup" aria-label="Model" data-noswipe>${chip('', 'Automatic (cheapest)', !g.model)}${g.models.map(m =>
      chip(m.id, esc(m.label) + (m.available ? '' : ' (unavailable)'), g.model === m.id, !m.available)).join('')}</div>`;
  const ready = ph.goal || ph.description;
  return wrap('genTasks', `<div class="sheet__body sheet__body--form">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">Generate tasks</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <span class="card__desc">For <strong>${esc(ph.name)}</strong>. The model writes tasks from the phase's goal and description and adds them to the end of its list.</span>
    ${ready ? '' : `<span class="form-err">This phase has no goal or description yet. Edit it and add one first.</span>`}
    <span class="pal-label">MODEL</span>${models}
    ${g.error ? `<span class="form-err">${esc(g.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="genRun"${g.loading || !ready ? ' disabled' : ''}>${g.loading ? spinner() + 'Generating…' : 'Generate tasks'}</button>
    </div>
  </div>`, 'Generate tasks');
}
