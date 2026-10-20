// The CRM page (#/crm, crm_reports.dashboard on the server): what needs attention across every project -
// overdue / due in 7 and 30 days, at risk, follow-ups due, gone quiet - then the pipeline by stage, active
// value by client and workload by due week, and the client / project reports to download.
// Filters: the dashboard's Work / Personal / All switch (ui.scope) and one client (ui.crmClient).
// Home shows a compact card of the same figures (crmHomeCard).
// Data: c.d.crm[crmKey(ui)] (GET /crm/dashboard), c.d.crmMissing when the server predates it.
// UI state (crm-dash-ui.js): crmClient (a client id or ''), crmReport { kind, id, busy }.

import { esc, scopeSwitch, emptyState } from '../ui.js';
import { plural } from '../format.js';
import { SCOPES, crmKey, crmClientOf } from '../data.js';
import { stageInfo, money } from '../crm-table.js';
import { dueBadge, HEALTHS } from './overview.js';
import { fuRow } from './crm-activity.js';

const LIST_MAX = 6;
const valueText = rows => (rows || []).map(r => money(r.total ?? r.active, r.currency)).join(' + ');
const asProject = r => ({ ...r, schedule: { days_left: r.days_left, due_state: r.due_state } });
const weekLabel = iso => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }); };

// One project in a list: name, client, stage, health and how it stands against its due date.
function projectRow(r, extra = '') {
  const [, stage, col] = stageInfo(r.stage);
  const health = r.health && r.health !== 'on_track' ? HEALTHS.find(h => h[0] === r.health) : null;
  const meta = [`<span class="cl-status" style="--c:${col}">${stage}</span>`, r.client_name ? `<span class="truncate">${esc(r.client_name)}</span>` : '',
    health ? `<span style="color:${health[2]}">${health[1]}</span>` : '', extra].filter(Boolean).join('');
  return `<button class="cd-row" data-action="openProject" data-id="${r.id}" data-key="cdp${r.id}">
    <span class="cd-row__text"><span class="cd-row__name truncate">${esc(r.name)}</span><span class="cd-row__meta">${meta}</span></span>
    <span class="cd-row__badge">${dueBadge(asProject(r), { compact: true })}</span></button>`;
}

function listCard(key, title, rows, empty, { tone = '', render = projectRow } = {}) {
  const shown = rows.slice(0, LIST_MAX);
  return `<section class="card card--pad cd-card${tone ? ' cd-card--' + tone : ''}" id="cd-${key}" data-key="cd-${key}">
    <div class="cd-card__head"><span class="card__title">${title}</span><span class="cd-count">${rows.length}</span></div>
    ${rows.length ? `<div class="cd-list">${shown.map(r => render(r)).join('')}</div>${rows.length > LIST_MAX ? `<span class="meta cd-more">+${rows.length - LIST_MAX} more</span>` : ''}`
      : `<span class="card__desc">${empty}</span>`}</section>`;
}

function tiles(n) {
  const t = (k, label, v, tone) => `<button class="cd-tile${v && tone ? ' cd-tile--' + tone : ''}" data-action="crmJump" data-k="${k}" data-key="cdt-${k}"><b>${v}</b><span>${label}</span></button>`;
  return `<div class="cd-tiles" data-key="cd-tiles">${t('overdue', 'Overdue', n.overdue, 'err')}${t('week', 'Due in 7 days', n.due_week, 'wait')}
    ${t('month', 'Due in 30 days', n.due_month)}${t('risk', 'At risk / off track', n.at_risk, 'err')}
    ${t('fu', 'Follow-ups due', n.followups_due, n.followups_overdue ? 'err' : 'wait')}${t('quiet', 'Quiet 14+ days', n.stale, 'wait')}</div>`;
}

// A bar per row, one hue; the figures are written beside it, and each bar says the same on hover.
function bars(rows, max) {
  return `<div class="cd-bars">${rows.map(r => `<div class="cd-bar" title="${esc(r.tip)}" data-key="${r.key}">
    <span class="cd-bar__label truncate">${r.label}</span>
    <span class="cd-bar__track"><span class="cd-bar__fill" style="width:${max ? Math.max(r.n ? 3 : 0, Math.round(100 * r.n / max)) : 0}%"></span></span>
    <span class="cd-bar__fig">${r.fig}</span></div>`).join('')}</div>`;
}

function pipeline(d) {
  const max = Math.max(...d.pipeline.map(s => s.count), 0);
  const rows = d.pipeline.map(s => {
    const v = valueText(s.value);
    return { key: `cdpl-${s.stage}`, n: s.count, label: `<span class="cl-status" style="--c:${stageInfo(s.stage)[2]}">${esc(s.label)}</span>`,
      fig: `<b>${s.count}</b>${v ? `<span class="meta">${esc(v)}</span>` : ''}`, tip: `${s.label}: ${plural(s.count, 'project')}${v ? ' · ' + v : ''}` };
  });
  return `<section class="card card--pad cd-card" data-key="cd-pipeline"><span class="card__title">Pipeline by stage</span>${bars(rows, max)}</section>`;
}

function byClient(d) {
  if (!d.by_client.length) {
    return `<section class="card card--pad cd-card" data-key="cd-clients"><span class="card__title">Active value by client</span>
      <span class="card__desc">No active work yet. Projects in the Active or On hold stage add up here.</span></section>`;
  }
  // Bars compare one currency (the one most values are in - the server ranks by it too); other currencies are written beside them.
  const cur = d.currency;
  const mixed = d.by_client.some(r => r.value.some(v => v.currency !== cur));
  const amount = r => (r.value.find(v => v.currency === cur) || {}).active || 0;
  const max = Math.max(...d.by_client.map(amount), 0);
  const rows = d.by_client.slice(0, 10).map(r => {
    const v = r.value.map(x => money(x.active, x.currency)).join(' + ');
    const name = r.client_name || 'No client';
    const label = r.client_id ? `<button class="btn--text cd-link truncate" data-action="openClient" data-id="${r.client_id}">${esc(name)}</button>` : `<span class="meta">${esc(name)}</span>`;
    return { key: `cdc-${r.client_id || 0}`, n: amount(r), label, fig: `${v ? `<b>${esc(v)}</b>` : '<span class="meta">no value set</span>'}<span class="meta">${plural(r.active_projects, 'active project')}</span>`,
      tip: `${name}: ${v || 'no value set'} · ${plural(r.active_projects, 'active project')}` };
  });
  return `<section class="card card--pad cd-card" data-key="cd-clients"><span class="card__title">Active value by client</span>${bars(rows, max)}
    ${mixed ? `<span class="meta">Bars compare ${esc(cur)} amounts.</span>` : ''}</section>`;
}

function workload(d) {
  const wl = d.workload, cols = [];
  if (wl.overdue.projects.length || wl.overdue.followups) cols.push({ key: 'late', label: 'Overdue', late: true, ...wl.overdue });
  wl.weeks.forEach((w, i) => cols.push({ key: w.start, label: i === 0 ? 'This week' : i === 1 ? 'Next week' : weekLabel(w.start), ...w }));
  const max = Math.max(...cols.map(c => c.projects.length + c.followups), 1);
  const col = c => {
    const n = c.projects.length, tip = `${c.label}${c.end ? ` (${weekLabel(c.start)} - ${weekLabel(c.end)})` : ''}: ${plural(n, 'project')} due, ${plural(c.followups, 'follow-up')}`
      + (n ? '\n' + c.projects.map(p => '• ' + p.name).join('\n') : '');
    return `<div class="cd-wk${c.late ? ' cd-wk--late' : ''}" title="${esc(tip)}" data-key="cdw-${c.key}">
      <span class="cd-wk__fig">${n + c.followups || ''}</span>
      <span class="cd-wk__col"><span class="cd-wk__fu" style="height:${Math.round(100 * c.followups / max)}%"></span><span class="cd-wk__gap"></span><span class="cd-wk__pr" style="height:${Math.round(100 * n / max)}%"></span></span>
      <span class="cd-wk__label">${esc(c.label)}</span></div>`;
  };
  return `<section class="card card--pad cd-card cd-card--wide" data-key="cd-workload">
    <div class="cd-card__head"><span class="card__title">Workload by due week</span>
      <span class="cd-legend"><span class="cd-key cd-key--pr"></span>Projects due<span class="cd-key cd-key--fu"></span>Follow-ups</span></div>
    <div class="cd-weeks">${cols.map(col).join('')}</div></section>`;
}

function reports(c) {
  const r = { kind: 'client', id: '', ...(c.ui.crmReport || {}) };
  const list = r.kind === 'client' ? (c.d.clients || []).map(x => [x.id, x.name]) : c.d.projects.filter(p => !p.is_general).map(p => [p.id, p.name]);
  const seg = (k, l) => `<button class="segmented__opt${r.kind === k ? ' is-on' : ''}" role="radio" aria-checked="${r.kind === k}" data-action="crmReportKind" data-v="${k}">${l}</button>`;
  const ready = r.id && list.some(([id]) => String(id) === String(r.id));
  return `<section class="card card--pad cd-card cd-card--wide" id="cd-reports" data-key="cd-reports"><span class="card__title">Reports</span>
    <span class="card__desc">A summary of one ${r.kind} - details, value, progress, stage history, activities and follow-ups - to keep or send on.</span>
    <div class="cd-report">
      <div class="segmented segmented--start crm-seg" role="radiogroup" data-noswipe>${seg('client', 'Client')}${seg('project', 'Project')}</div>
      <select class="input cd-report__pick" data-cl-change="crmReportPick" aria-label="Which ${r.kind}">
        <option value="">${list.length ? `Pick a ${r.kind}…` : `No ${r.kind}s yet`}</option>
        ${list.map(([id, name]) => `<option value="${id}"${String(id) === String(r.id) ? ' selected' : ''}>${esc(name)}</option>`).join('')}</select>
      <button class="btn btn--sm" data-action="crmDownload" data-fmt="csv"${ready && !r.busy ? '' : ' disabled'}>Download CSV</button>
      <button class="btn btn--sm" data-action="crmDownload" data-fmt="md"${ready && !r.busy ? '' : ' disabled'}>Download Markdown</button>
    </div></section>`;
}

function filters(c) {
  const cur = crmClientOf(c.ui);
  return `<div class="cd-filters" data-key="cd-filters">${scopeSwitch(c.ui.scope, 'setScope', SCOPES)}
    <select class="input cd-client" data-cl-change="crmClient" aria-label="Client">
      <option value="">All clients</option>${(c.d.clients || []).map(x => `<option value="${x.id}"${String(x.id) === String(cur) ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}</select></div>`;
}

export function crmPage(c) {
  const { d } = c;
  if (d.crmMissing || d.clientsMissing) {
    return `<div class="page page--narrow" data-key="crm"><div class="card card--pad" data-key="cd-restart"><span class="card__title">The CRM dashboard is almost ready</span>
      <span class="card__desc">Restart the Den from the dashboard to see what's overdue, due soon and at risk across your projects, and to download reports.</span></div></div>`;
  }
  const dash = (d.crm || {})[crmKey(c.ui)];
  if (!dash) return `<div class="page page--wide cd-page" data-key="crm">${filters(c)}<span class="card__desc">Loading…</span></div>`;
  const n = dash.counts;
  if (!n.projects && !crmClientOf(c.ui)) {
    return `<div class="page page--wide cd-page" data-key="crm">${filters(c)}${emptyState({ title: 'Nothing to track yet', body: 'Give projects a due date, stage, value and client on their Overview tab and they show up here.', label: 'Projects', attrs: 'data-action="go" data-view="projects"', key: 'cd-empty' })}</div>`;
  }
  const fus = [...dash.followups.overdue, ...dash.followups.today];
  const quiet = r => projectRow(r, `<span class="meta">${r.last_activity_at ? `quiet ${plural(r.idle_days, 'day')}` : `nothing logged in ${plural(r.idle_days, 'day')}`}</span>`);
  return `<div class="page page--wide cd-page" data-key="crm">
    ${filters(c)}
    ${tiles(n)}
    <div class="cd-grid">
      ${listCard('overdue', 'Overdue', dash.overdue, 'Nothing overdue.', { tone: dash.overdue.length ? 'err' : '' })}
      ${listCard('week', 'Due in the next 7 days', dash.due_week, 'Nothing due this week.')}
      ${listCard('month', 'Due in 8–30 days', dash.due_month, 'Nothing due later this month.')}
      ${listCard('risk', 'At risk / off track', dash.at_risk, 'Every open project is on track.')}
      ${listCard('fu', 'Follow-ups due today and overdue', fus, 'No follow-ups due.', { render: f => fuRow(c, f, { key: 'cdf', date: true }) })}
      ${listCard('quiet', `No activity for ${dash.stale_days}+ days`, dash.stale, 'Every open project has had something happen lately.', { render: quiet })}
      ${pipeline(dash)}
      ${byClient(dash)}
      ${workload(dash)}
      ${reports(c)}
    </div>
  </div>`;
}

// Home: one compact card - overdue, due this week, follow-ups due - that opens the CRM page.
export function crmHomeCard(c) {
  if (c.d.crmMissing) return '';
  const dash = (c.d.crm || {})[`${c.ui.scope || 'all'}|`];
  if (!dash || !dash.counts.open) return '';
  const n = dash.counts;
  const f = (v, label, tone) => `<span class="cd-mini${v && tone ? ' cd-mini--' + tone : ''}"><b>${v}</b>${label}</span>`;
  return `<section class="section reveal" style="--i:105" data-key="crm-home">
    <button class="card cd-home" data-action="go" data-view="crm" aria-label="Open the CRM dashboard">
      <span class="cd-home__head"><span class="card__title">CRM</span><span class="meta">${plural(n.open, 'open project')} ›</span></span>
      <span class="cd-home__figs">${f(n.overdue, 'overdue', 'err')}${f(n.due_week, 'due this week', 'wait')}${f(n.followups_due, n.followups_due === 1 ? 'follow-up due' : 'follow-ups due', n.followups_overdue ? 'err' : 'wait')}</span>
    </button></section>`;
}
