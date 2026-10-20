// Small markup helpers shared by the views. Pure functions: data in, HTML string out.
// Every user- or model-supplied string goes through esc().

import { store } from './api.js';
import { escapeHTML, modelChip, modelChipHTML, statusOf, agoShort, ago, mmss, secs, fmtCost, taskName, taskProgress, attribution, attributionParts } from './format.js';
import { octopusSVG } from './octopus.js';
import { icon } from './icons.js';
import { projectById, projectColor } from './data.js';

export const esc = escapeHTML;
export { icon };

// Octopus at a fixed pixel size (the SVG fills its box).
export const octo = (mood, size, extra = '') =>
  `<span class="octo-box" style="width:${size}px;height:${size}px${extra}">${octopusSVG(mood)}</span>`;

// Empty state: what this place is for, plus one primary action (data-* attrs as a string, e.g. 'data-action="openPalette"').
export const emptyState = ({ title, body = '', label = '', attrs = '', mood = 'sleep', key = '', cls = '' }) =>
  `<div class="estate ${cls}"${key ? ` data-key="${key}"` : ''}>${octo(mood, 72)}<span class="estate__title">${escapeHTML(title)}</span>${body ? `<span class="estate__body">${escapeHTML(body)}</span>` : ''}${label ? `<button class="btn btn--primary btn--sm" ${attrs}>${escapeHTML(label)}</button>` : ''}</div>`;

export const spinner = (cls = '') => `<span class="spinner ${cls}"></span>`;

// A task's progress bar and percentage (see taskProgress), or '' when there's nothing to show.
export function progressBar(t, now, cls = '') {
  const p = taskProgress(t, now);
  if (!p) return '';
  return `<span class="prog${p.estimate ? ' prog--guess' : ''} ${cls}" title="${esc(p.title)}"><span class="prog__bar"><span style="width:${p.percent}%"></span></span><span class="prog__pct">${esc(p.label)}</span></span>`;
}

// "Polish text" for any text box: `id` is the textarea's id, `text` its current value (empty = disabled).
// kind 'note' polishes plain notes (calendar agenda...) rather than a request to an assistant.
export const polishBtn = (id, text, { kind = '', cls = '' } = {}) => {
  const busy = store.ui.polishing === id;
  return `<button type="button" class="tool-btn polish-btn ${cls}" data-action="polish" data-target="${id}"${kind ? ` data-kind="${kind}"` : ''} title="Polish text" aria-label="Polish text"${!String(text || '').trim() || busy ? ' disabled' : ''}>${busy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 17, { strokeWidth: 1.6 })}</button>`;
};
// A field's label with the polish button on its right.
export const polishLabel = (label, id, text, opts) =>
  `<div class="field-head"><span class="pal-label">${label}</span>${polishBtn(id, text, opts)}</div>`;
export const dot = color => `<span class="dot" style="--c:${color}"></span>`;
export const swatch = (color, cls = 'swatch-sq') => `<span class="${cls}" style="background:${color}"></span>`;

export const chipFor = t => modelChipHTML(modelChip(t, store.data.models));
// "Restart needed": this task (or a message in its chat) edited Octopus's own code, which only loads on the next
// restart (GET /queue/status restart_needed). A button opens the Restart prompt; pass 'span' inside another button.
export function restartTag(t, el = 'button') {
  const ids = store.data.status?.restart_needed;
  if (!ids?.length || ![t, ...(t.msgs || [])].some(m => ids.includes(m.id))) return '';
  const tip = "This task changed Octopus's own code - restart Octopus to load it";
  return el === 'button'
    ? `<button class="restart-tag" data-action="restartOctopus" title="${tip}">${icon('restart', 11)}Restart needed</button>`
    : `<span class="restart-tag" title="${tip}">${icon('restart', 11)}Restart needed</span>`;
}
export const modelLabel = id => (store.data.models.find(m => m.id === id) || {}).label || (id === 'claude-code' || !id ? 'Claude Code' : id);
export const laneColor = lane => (lane === 'api' ? 'var(--lane-api-fg)' : lane === 'assistant' ? 'var(--lane-assistant-fg)' : 'var(--lane-agent-fg)');

export const toggle = (on, action, attrs = '', sleep = false) =>
  `<button class="toggle${sleep ? ' toggle--sleep' : ''}" role="switch" aria-checked="${on ? 'true' : 'false'}" data-action="${action}" ${attrs}></button>`;

export const sk = (w, h, r = 6, extra = '') => `<div class="sk" style="width:${w};height:${h}px;border-radius:${r}px;flex:none${extra}"></div>`;

export const sectionHead = (title, right = '', count = '') =>
  `<div class="section__head"><span class="section__title">${title}${count !== '' ? ` <span class="section__count">${count}</span>` : ''}</span>${right}</div>`;

// ---------------------------------------------------------------- time
export const clock = iso => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
export function dayWord(iso) {
  const d = new Date(iso), now = new Date();
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  return d.toLocaleDateString([], { weekday: 'short' });
}

// "4:12" for Claude Code turns, "2.1s" for answer-only calls - the prototype's split.
export const took = t => (t.duration_ms == null ? '' : t.lane !== 'agent' ? secs(t.duration_ms) : mmss(t.duration_ms / 1000));

// ---------------------------------------------------------------- status glyphs
const GLYPH = { done: ['✓', 'var(--ok)'], cancelled: ['–', 'var(--cancelled)'], error: ['✕', 'var(--err)'], needs_review: ['◐', 'var(--review)'] };
export const glyph = t => GLYPH[t.status] || ['·', statusOf(t).color];

// "Routed: simple: short factual question" - the router's reason already starts
// with its category, so only add it when it doesn't.
export function routeNote(t) {
  const r = t.route_reason || '';
  if (!r) return '';
  if (t.category === 'manual' || !t.category || r.startsWith(t.category)) return 'Routed: ' + r;
  return `Routed: ${t.category}: ${r}`;
}

// ---------------------------------------------------------------- lazy lists
// Shows the first `first` items of a long list; the rest load `step` at a time as the sentinel below it scrolls into
// view (app.js watches [data-lazy]) or its button is tapped. The sentinel sits outside the list so drag-reorder ignores it.
export function lazy(key, items, first = 20, step = 20) {
  const shown = Math.max(first, (store.ui.lazy || {})[key] || 0);
  const left = items.length - shown;
  return {
    items: items.slice(0, shown),
    more: left > 0 ? `<div class="lazy-more" data-lazy="${key}" data-first="${first}" data-step="${step}" data-key="lazy-${key}"><button class="btn--text" data-action="lazyMore" data-lazy="${key}" data-first="${first}" data-step="${step}">Show ${Math.min(step, left)} more · ${left} left</button></div>` : '',
  };
}

// A list of ~6 rows in its own short scroll box; the sentinel inside it loads the next batch when you scroll to the end.
export const lazyScroll = (list, more) => `<div class="lazy-scroll" data-noswipe>${list}${more}</div>`;

// ---------------------------------------------------------------- history row (Recent, Activity, Done recently)
// t is a thread summary (data.threadSummaries) or a plain task.
// Models and Claude account(s) a chat's tasks recorded: { models: "Claude Code · GPT-6 Luna", account: "Account: Work" }.
export function usedBy(msgs) {
  const list = (store.data.settings && store.data.settings.claude_accounts) || [];
  const name = id => { const a = list.find(x => x.id === id); return a ? (a.alias || (a.kind === 'mac' ? (a.org || 'Mac login') : a.label)) : id + ' (removed)'; };
  return attributionParts(attribution(msgs), modelLabel, name);
}

// ---------------------------------------------------------------- Claude account colors
// Each account has a color (Settings › Claude account). A row shows the account(s) its task ran / will run
// on as dots.
const acctList = () => (store.data.settings && store.data.settings.claude_accounts) || [];
// Account ids for a task or a chat's tasks: what they recorded, else (not started yet) the pinned account,
// else the project's own / the default one.
export function accountIdsOf(tasks) {
  const ids = [];
  const add = id => { if (id && !ids.includes(id)) ids.push(id); };
  for (const t of tasks || []) {
    if ((t.lane || 'agent') !== 'agent') continue;
    if (t.started_at || t.claude_account) add(t.claude_account);
    else add(t.pinned_account || (projectById(t.project_id) || {}).claude_account);
  }
  return ids;
}
export function acctDots(tasks) {
  const list = acctList();
  return accountIdsOf(tasks).map(id => list.find(a => a.id === id)).filter(Boolean).map(a =>
    `<span class="dot acct-dot" style="--c:${a.color}" title="${escapeHTML(a.alias || a.label)}"></span>`).join('');
}

export function histRow(t, { project = true, key = 'h' } = {}) {
  const p = projectById(t.project_id);
  const [g, gc] = glyph(t);
  const by = usedBy(t.msgs || [t]);
  const rest = [agoShort(t.finished_at || t.created_at), took(t), fmtCost(t.cost_usd)].filter(Boolean).join(' · ');
  // The account goes inside the model chip ("Claude Code · LA"); the extra line only lists other models a chat used.
  const chip = modelChip(t, store.data.models);
  const acct = /^Accounts?: /.test(by.account) ? by.account.replace(/^Accounts?: /, '') : '';
  if (acct) chip.label += ' · ' + acct;
  const used = by.models.split(' · ').filter(m => m && m !== modelChip(t, store.data.models).label).join(' · ') + (acct || !by.account ? '' : (by.models ? ' · ' : '') + by.account);
  return `<div class="row hrow" data-key="${key}${t.id}">
    <span class="row__glyph" style="color:${gc}">${g}</span>
    <div class="row__body" data-action="openChat" data-id="${t.id}">
      <span class="row__title truncate">${esc(taskName(t))}</span>
      <div class="meta-row"><span class="task-num">#${t.id}</span>${project ? `<span class="nowrap">${esc(p ? p.name : t.project_name)}</span>` : ''}${acctDots(t.msgs || [t])}${modelChipHTML(chip)}${restartTag(t)}<span class="truncate">${project ? '· ' : ''}${esc(rest)}</span></div>
      ${used ? `<div class="meta-row hrow__used"><span class="truncate">${esc(used)}</span></div>` : ''}
    </div>
    <button class="row-icon" title="Run again" aria-label="Run again" data-action="runAgain" data-id="${t.id}">↻</button>
  </div>`;
}

export const projectSwatch = p => swatch(projectColor(p));
export { ago, agoShort, statusOf, fmtCost, mmss, secs };

// Work / Personal / All switch. `action` is the data-action fired with data-scope (or data-cat).
export function scopeSwitch(current, action, options, cls = '') {
  return `<div class="chip-row scope ${cls}" role="radiogroup" aria-label="Work or personal" data-noswipe>${options.map(([k, l]) =>
    `<button class="chip${current === k ? ' is-on' : ''}" role="radio" aria-checked="${current === k}" data-action="${action}" data-scope="${k}" data-key="${action}-${k}">${l}</button>`).join('')}</div>`;
}

// Pause all / Resume all / Dismiss all for a list of queued tasks. Each button
// carries the ids it acts on; a button with nothing to do is disabled.
export function bulkBar(tasks, key = 'bulk') {
  const queued = tasks.filter(t => t.status === 'queued');
  if (!queued.length) return '';
  const ids = ts => ts.map(t => t.id).join(',');
  const running = queued.filter(t => !t.paused), held = queued.filter(t => t.paused);
  const btn = (action, label, list, extra = '') => `<button class="btn btn--ghost btn--sm${extra}" data-action="${action}" data-ids="${ids(list)}"${list.length ? '' : ' disabled'}>${label}</button>`;
  return `<div class="bulk" data-key="${key}">${btn('pauseAll', 'Pause all', running)}${btn('resumeAll', 'Resume all', held)}${btn('dismissAll', 'Dismiss all', queued, ' bulk__danger')}</div>`;
}

// Collapsible Home sections: the title is a button with a caret; the fold state is remembered per device (ui.secFold).
const FOLD_CARET = '<svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 5l4 4 4-4"/></svg>';
export const folded = (c, k) => !!(c.ui.secFold && c.ui.secFold[k]);
export const foldTitle = (c, k, title) => `<button class="fold${folded(c, k) ? ' is-folded' : ''}" data-action="secFold" data-k="${k}" aria-expanded="${!folded(c, k)}" title="${folded(c, k) ? 'Expand' : 'Collapse'}">${FOLD_CARET}<span class="section__title">${title}</span></button>`;
