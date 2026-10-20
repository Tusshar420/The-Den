// Token usage: how many tokens (and dollars) every chat, project and model burned over a date range -
// the last 30 days by default, any past month, or a custom range - with project / model / type filters.
// Claude Code runs on the subscription, so its cost is only an API-price estimate and is kept apart
// from real spend (OpenAI, Gemini...). Side calls - naming chats, summaries, the classifier, polishing
// a prompt - show up as "Background".

import { fmtCost, mmss, secs, plural } from '../format.js';
import { esc, modelLabel } from '../ui.js';
import { projectById, projectColor } from '../data.js';
import { usageRange } from '../octopus-data.js';

const PERIODS = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['month', 'This month'], ['lastmonth', 'Last month']];
export const LANES = { agent: 'Claude Code', api: 'API models', assistant: 'Tools', background: 'Background' };
const LANE_COLOR = { agent: 'var(--coral)', api: 'var(--ok)', assistant: 'var(--review)', background: 'var(--text-meta)' };
const KIND = { title: 'Naming a chat', summary: 'Summarising a chat', classifier: 'Sorting a request', polish: 'Polishing a prompt' };

// 1234 → "1,234", 45678 → "45.7K", 98765432 → "98.8M"
export function tok(n) {
  n = n || 0;
  if (n < 10000) return n.toLocaleString();
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e5 ? 1 : 0) + 'K';
  return (n / 1e6).toFixed(n < 1e8 ? 1 : 0) + 'M';
}
const full = n => (n || 0).toLocaleString() + ' tokens';
const money = n => (n ? (n < 0.01 ? '<$0.01' : '$' + n.toFixed(2)) : '$0');
const monthName = ym => { const [y, m] = ym.split('-').map(Number); return new Date(y, m - 1, 1).toLocaleDateString([], { month: 'long', year: 'numeric' }); };
const dayName = (iso, o = { day: 'numeric', month: 'short' }) => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString([], o); };
const when = iso => new Date(iso).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
const cost = r => (r.spend_usd || 0) + (r.claude_estimate_usd || 0);
// A group's money in one figure: "~" as soon as any of it is Claude Code's estimate.
const costOf = r => (!cost(r) ? '' : r.claude_estimate_usd ? '~' + money(cost(r)) : money(r.spend_usd));
// One row's money: real spend as is, Claude Code's as "~" (it's an estimate).
const costText = (r, lane) => (lane === 'agent' ? (r.cost_usd != null ? fmtCost(r.cost_usd) : '') : r.cost_usd ? money(r.cost_usd) : '');

function delta(now, before) {
  if (!before) return now ? '<span class="tk-delta">new this period</span>' : '';
  const pct = Math.round((now - before) / before * 100);
  if (!pct) return '<span class="tk-delta">same as the period before</span>';
  return `<span class="tk-delta">${pct > 0 ? '▲' : '▼'} ${Math.abs(pct)}% vs the period before</span>`;
}

function filters(c, t) {
  const f = c.ui.octo.usage;
  const opts = (t && t.options) || { months: [], projects: [], models: [], accounts: [] };
  const chip = (k, l) => `<button class="act-chip${f.period === k ? ' is-on' : ''}" data-action="usageSet" data-k="period" data-v="${k}">${l}</button>`;
  const monthOn = /^\d{4}-\d{2}$/.test(f.period);
  const select = (k, label, all, list) => `<label class="tk-sel"><span class="tk-sel__l">${label}</span>
    <select class="input input--sm" data-input="octo.usage.${k}" aria-label="${label}"><option value="">${all}</option>
    ${list.map(([v, l]) => `<option value="${esc(v)}"${String(f[k]) === String(v) ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>`;
  const any = f.project || f.model || f.lane || f.account;
  return `<div class="tk-filters" data-noswipe>
    <div class="act-filters">${PERIODS.map(([k, l]) => chip(k, l)).join('')}
      <select class="act-chip tk-month${monthOn ? ' is-on' : ''}" data-input="octo.usage.month" aria-label="A past month">
        <option value="">Past month…</option>${opts.months.map(m => `<option value="${m}"${f.period === m ? ' selected' : ''}>${monthName(m)}</option>`).join('')}</select>
      ${chip('custom', 'Custom')}</div>
    ${f.period === 'custom' ? `<div class="tk-range"><label class="tk-sel"><span class="tk-sel__l">From</span><input type="date" class="input input--sm" data-input="octo.usage.start" value="${esc(f.start)}" aria-label="From"></label>
      <label class="tk-sel"><span class="tk-sel__l">To</span><input type="date" class="input input--sm" data-input="octo.usage.end" value="${esc(f.end)}" aria-label="To"></label></div>` : ''}
    <div class="tk-selects">
      ${select('project', 'Project', 'All projects', opts.projects.map(p => [p.id, p.name]))}
      ${select('model', 'Model', 'All models', opts.models.map(m => [m, modelLabel(m)]))}
      ${select('account', 'Claude account', 'All accounts', (opts.accounts || []).map(a => [a.id, a.label]))}
      ${select('lane', 'Type', 'Everything', Object.entries(LANES))}
      ${any ? '<button class="btn--text btn--muted tk-clear" data-action="usageClear">Clear filters</button>' : ''}
    </div>
  </div>`;
}

function tiles(t) {
  const s = t.totals, p = t.previous;
  const tile = (label, value, sub = '', title = '') => `<div class="stat-card tk-tile"><span class="stat-card__l">${label}</span>
    <span class="tk-tile__v"${title ? ` title="${esc(title)}"` : ''}>${value}</span>${sub ? `<span class="meta">${sub}</span>` : ''}</div>`;
  return `<div class="tk-hero">
      <span class="stat-card__l">Tokens used</span>
      <span class="tk-hero__v" title="${esc(full(s.total_tokens))}">${tok(s.total_tokens)}</span>
      <span class="meta">${plural(s.runs, 'run')} · ${esc(dayName(t.start, { day: 'numeric', month: 'short', year: 'numeric' }))}${t.start !== t.end ? ' – ' + esc(dayName(t.end, { day: 'numeric', month: 'short', year: 'numeric' })) : ''} ${delta(s.total_tokens, p.total_tokens)}</span>
    </div>
    <div class="tk-tiles">
      ${tile('Input', tok(s.input_tokens), s.cache_tokens ? `${tok(s.cache_tokens)} of it from cache` : 'what was sent to the model', full(s.input_tokens))}
      ${tile('Output', tok(s.output_tokens), 'what the model wrote', full(s.output_tokens))}
      ${tile('API spend', money(s.spend_usd), 'real money · OpenAI, Gemini…')}
      ${tile('Claude Code', '~' + money(s.claude_estimate_usd), 'at API prices · your plan covers it')}
    </div>`;
}

// Tokens per day: one column per day, hover (or focus) for the numbers, click to see just that day.
function chart(t) {
  const days = t.by_day;
  const max = Math.max(1, ...days.map(d => d.total_tokens));
  const step = [1, 2, 5].map(m => m * 10 ** Math.floor(Math.log10(max))).find(v => v * 4 >= max) || 10 ** Math.ceil(Math.log10(max));
  const top = Math.ceil(max / step) * step;
  const ticks = [top, top / 2, 0];
  // ~7 date labels; a narrow screen keeps only the first, middle and last (the others are "minor").
  const every = Math.ceil(days.length / 7), last = days.length - 1, mid = Math.round(last / 2);
  const labelled = i => i === 0 || i === last || (i % every === 0 && last - i >= every / 2);
  const xLabel = (d, i) => (labelled(i) ? `<span class="tk-col__x${i === 0 || i === last || i === mid ? '' : ' tk-col__x--minor'}">${esc(dayName(d.date))}</span>`
    : i === mid ? `<span class="tk-col__x tk-col__x--phone">${esc(dayName(d.date))}</span>` : '');
  return `<div class="card card--pad tk-chart">
    <div class="usage-row__head"><span class="card__title">Tokens per day</span><span class="meta">busiest ${tok(max)} · tap a day to see only it</span></div>
    <div class="tk-plot">
      <div class="tk-axis">${ticks.map(v => `<span>${tok(v)}</span>`).join('')}</div>
      <div class="tk-cols${days.length > 45 ? ' tk-cols--dense' : ''}">
        ${days.map((d, i) => `<button class="tk-col" data-action="usageDay" data-v="${d.date}" aria-label="${esc(dayName(d.date))}: ${full(d.total_tokens)}">
          <span class="tk-col__bar" style="height:${d.total_tokens ? Math.max(2, d.total_tokens / top * 100) : 0}%"></span>
          <span class="tk-tip"><b>${esc(dayName(d.date, { weekday: 'short', day: 'numeric', month: 'short' }))}</b>
            <span>${full(d.total_tokens)}</span><span>${tok(d.input_tokens)} in · ${tok(d.output_tokens)} out</span>
            <span>${plural(d.runs, 'run')}${cost(d) ? ' · ' + costOf(d) : ''}</span></span>
          ${xLabel(d, i)}
        </button>`).join('')}
      </div>
    </div>
  </div>`;
}

// One breakdown card (project / model / type / category). Rows that are also filters set that filter on click.
function breakdown(title, rows, total, { label, color, filter } = {}) {
  if (!rows.length) return '';
  const shown = rows.slice(0, 8), rest = rows.slice(8);
  if (rest.length) shown.push({ key: null, label: `${rest.length} more`, other: true, ...rest.reduce((a, r) => ({ total_tokens: a.total_tokens + r.total_tokens, runs: a.runs + r.runs, spend_usd: a.spend_usd + r.spend_usd, claude_estimate_usd: a.claude_estimate_usd + r.claude_estimate_usd }), { total_tokens: 0, runs: 0, spend_usd: 0, claude_estimate_usd: 0 }) });
  return `<div class="card card--pad tk-break"><span class="card__title">${title}</span>
    ${shown.map(r => {
      const pct = total ? r.total_tokens / total * 100 : 0;
      const name = r.other ? r.label : label(r);
      const inner = `<span class="tk-break__name truncate">${esc(name)}</span>
        <span class="tk-break__bar"><span style="width:${Math.max(pct, r.total_tokens ? 1 : 0)}%;background:${r.other ? 'var(--text-faint)' : color(r)}"></span></span>
        <span class="tk-break__n mono" title="${esc(full(r.total_tokens))}">${tok(r.total_tokens)}</span>
        <span class="tk-break__pct mono">${pct >= 1 || !pct ? Math.round(pct) : '<1'}%</span>
        <span class="tk-break__meta meta">${plural(r.runs, 'run')}${cost(r) ? ' · ' + costOf(r) : ''}</span>`;
      return filter && !r.other && r.key != null
        ? `<button class="tk-break__row" data-action="usageSet" data-k="${filter}" data-v="${esc(r.key)}" title="Show only ${esc(name)}">${inner}</button>`
        : `<div class="tk-break__row">${inner}</div>`;
    }).join('')}
  </div>`;
}

// What a single run was: its chat's title, or what a background call did.
const runName = r => (r.lane === 'background' ? (KIND[r.kind] || r.kind) + (r.title ? ': ' + r.title : '') : r.title || 'Untitled');
const took = r => (r.duration_ms == null ? '' : r.lane === 'agent' ? mmss(r.duration_ms / 1000) : secs(r.duration_ms));

// Group runs into chats (a background call with no chat stays on its own).
function chats(items) {
  const byChat = new Map();
  for (const r of items) {
    const k = r.chat_id != null ? 'c' + r.chat_id : 'r' + r.id;
    let g = byChat.get(k);
    if (!g) byChat.set(k, g = { key: k, chat_id: r.chat_id, runs: [], input_tokens: 0, output_tokens: 0, total_tokens: 0, spend: 0, estimate: 0, at: r.at, models: new Set() });
    g.runs.push(r);
    g.input_tokens += r.input_tokens || 0; g.output_tokens += r.output_tokens || 0; g.total_tokens += r.total_tokens;
    r.lane === 'agent' ? g.estimate += r.cost_usd || 0 : g.spend += r.cost_usd || 0;
    if (r.at > g.at) g.at = r.at;
    if (r.model_id) g.models.add(r.model_id);
  }
  for (const g of byChat.values()) {
    const main = g.runs.find(r => r.lane !== 'background') || g.runs[0];
    Object.assign(g, { title: main.lane === 'background' && g.chat_id == null ? runName(main) : main.title || 'Untitled', project_id: main.project_id, project_name: main.project_name });
  }
  return [...byChat.values()];
}

function detailRow(r, c, { open, chat } = {}) {
  const p = projectById(r.project_id);
  const models = chat ? [...r.models].map(modelLabel).join(', ') : modelLabel(r.model_id);
  const meta = [r.project_name, models, chat ? plural(r.runs.length, 'run') : LANES[r.lane] || r.lane, !chat && took(r), when(r.at)].filter(Boolean).map(esc).join(' · ');
  // A chat mixing both shows one figure ("~" when any of it is Claude Code's estimate); the split is in the hover text.
  const money2 = chat ? (r.estimate ? fmtCost(r.spend + r.estimate) : r.spend ? money(r.spend) : '') : costText(r, r.lane);
  const moneyTip = chat && r.spend && r.estimate ? ` title="${money(r.spend)} API spend + ${fmtCost(r.estimate)} Claude Code at API prices"` : '';
  const head = `<span class="tk-row__main"><span class="tk-row__t truncate">${p ? `<span class="tk-sw" style="background:${projectColor(p)}"></span>` : ''}${esc(chat ? r.title : runName(r))}</span><span class="meta truncate">${meta}</span></span>
    <span class="tk-row__n mono" title="${esc(full(r.input_tokens))} in">${tok(r.input_tokens)}</span>
    <span class="tk-row__n mono" title="${esc(full(r.output_tokens))} out">${tok(r.output_tokens)}</span>
    <span class="tk-row__n tk-row__tot mono" title="${esc(full(r.total_tokens))}">${tok(r.total_tokens)}</span>
    <span class="tk-row__n mono"${moneyTip}>${money2}</span>`;
  if (chat) {
    const expandable = r.runs.length > 1 || r.chat_id != null;
    return `<div class="tk-row-wrap${open ? ' is-open' : ''}" data-key="tkc${esc(r.key)}">
      <button class="tk-row" data-action="usageOpen" data-v="${esc(r.key)}" aria-expanded="${!!open}"${expandable ? '' : ' disabled'}>${head}</button>
      ${open ? `<div class="tk-sub">${r.runs.map(x => detailRow(x, c)).join('')}
        ${r.chat_id != null ? `<button class="btn btn--sm btn--ghost tk-open" data-action="openChat" data-id="${r.chat_id}">Open the chat</button>` : ''}</div>` : ''}
    </div>`;
  }
  const id = r.lane === 'background' ? r.chat_id : r.id;
  return id != null ? `<button class="tk-row" data-action="openChat" data-id="${id}" title="Open the chat">${head}</button>` : `<div class="tk-row">${head}</div>`;
}

function details(c, t) {
  const f = c.ui.octo.usage;
  const rows = f.group === 'chat' ? chats(t.items) : t.items.map(r => ({ ...r }));
  const key = f.sort === 'cost' ? (r => (f.group === 'chat' ? r.spend + r.estimate : r.cost_usd || 0)) : f.sort === 'new' ? null : (r => r.total_tokens);
  rows.sort(key ? (a, b) => key(b) - key(a) : (a, b) => (a.at < b.at ? 1 : -1));
  const seg = (k, cur, opts) => `<div class="segmented" data-noswipe>${opts.map(([v, l]) => `<button class="segmented__opt${cur === v ? ' is-on' : ''}" data-action="usageSet" data-k="${k}" data-v="${v}">${l}</button>`).join('')}</div>`;
  const shown = rows.slice(0, f.limit);
  return `<section class="section tk-details" data-key="tk-details">
    <div class="section__head"><span class="section__title">Details <span class="section__count">${rows.length}</span></span>
      <span class="tk-details__ctl">${seg('group', f.group, [['chat', 'By chat'], ['run', 'Every run']])}${seg('sort', f.sort, [['tokens', 'Most tokens'], ['cost', 'Most cost'], ['new', 'Newest']])}
        <button class="btn btn--sm btn--ghost" data-action="usageCsv">Export CSV</button></span></div>
    <div class="list tk-table">
      <div class="tk-row tk-row--head"><span>${f.group === 'chat' ? 'Chat' : 'Run'}</span><span>In</span><span>Out</span><span>Total</span><span>Cost</span></div>
      ${shown.map(r => detailRow(r, c, { chat: f.group === 'chat', open: f.open === r.key })).join('')}
    </div>
    ${rows.length > shown.length ? `<button class="btn btn--ghost tk-more" data-action="usageMore">Show ${Math.min(50, rows.length - shown.length)} more</button>` : ''}
    ${t.items_truncated ? '<span class="meta">Only the biggest 2,000 runs are listed - narrow the range or filters to see the rest. The totals above count everything.</span>' : ''}
  </section>`;
}

export function usage(c) {
  const t = c.d.octo.tokens;
  const f = c.ui.octo.usage;
  const [start, end] = usageRange(f);
  const stale = t && !t.error && (t.start !== start || t.end !== end || String(t.filters.project_id ?? '') !== String(f.project)
    || (t.filters.model_id || '') !== f.model || (t.filters.lane || '') !== f.lane || (t.filters.account || '') !== f.account);
  const intro = `<div class="rt-intro"><span class="rt-intro__t">Token usage</span><span class="meta">Where your tokens go - by project, model, chat and day. Claude Code runs on your plan, so its cost is what the same work would cost at API prices; API spend is real money.</span></div>`;
  let body;
  if (!t) body = '<div class="empty">Loading…</div>';
  else if (t.error) body = `<div class="empty">Couldn't load usage: ${esc(t.error)}</div>`;
  else if (!t.totals.runs) body = `<div class="empty">No token usage ${f.project || f.model || f.lane || f.account ? 'with these filters ' : ''}in this period.</div>`;
  else {
    const total = t.totals.total_tokens;
    const modelLane = m => (t.items.find(r => r.model_id === m) || {}).lane;
    body = `${tiles(t)}${chart(t)}
      <div class="tk-breaks">
        ${breakdown('By project', t.by_project, total, { label: r => r.label || 'Not tied to a project', color: r => projectColor(projectById(r.key)), filter: 'project' })}
        ${breakdown('By model', t.by_model, total, { label: r => (r.key ? modelLabel(r.key) : 'Unknown'), color: r => LANE_COLOR[modelLane(r.key)] || 'var(--text-meta)', filter: 'model' })}
        ${breakdown('By type', t.by_lane, total, { label: r => LANES[r.key] || r.key, color: r => LANE_COLOR[r.key] || 'var(--text-meta)', filter: 'lane' })}
        ${breakdown('By Claude account', t.by_account || [], total, { label: r => r.label || r.key, color: () => 'var(--coral)', filter: 'account' })}
        ${breakdown('By kind of request', t.by_category, total, { label: r => KIND[r.key] || (r.key ? r.key[0].toUpperCase() + r.key.slice(1) : 'Unsorted'), color: () => 'var(--run)' })}
      </div>
      ${details(c, t)}`;
  }
  return `<div class="page page--wide page--usage${stale ? ' is-stale' : ''}" data-key="page-usage">${intro}${filters(c, t && !t.error ? t : null)}${body}</div>`;
}

// Every run in the current slice as CSV (what the Export button downloads).
export function usageCsv(t) {
  const q = v => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const head = ['when', 'type', 'what', 'project', 'account', 'model', 'category', 'status', 'input_tokens', 'output_tokens', 'cache_tokens', 'total_tokens', 'cost_usd', 'cost_is_estimate', 'duration_ms', 'chat_id'];
  const rows = t.items.map(r => [r.at, LANES[r.lane] || r.lane, runName(r), r.project_name, r.account, r.model_id, r.category, r.status, r.input_tokens, r.output_tokens,
    r.cache_tokens, r.total_tokens, r.cost_usd, r.lane === 'agent' ? 'yes' : 'no', r.duration_ms, r.chat_id]);
  return [head, ...rows].map(r => r.map(q).join(',')).join('\n');
}
