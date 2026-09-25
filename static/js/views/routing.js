// Routing: every request Octopus has routed - what it was, how it was classified, which project, agent,
// model and tools it picked, why, and what came of it. So its decisions can be understood and improved.

import { ago, secs, mmss, fmtCost } from '../format.js';
import { esc } from '../ui.js';
import { candidates, permissionRows, riskPill } from './plan-card.js';

const STATUSES = [['all', 'All'], ['queued', 'Run'], ['proposed', 'Waiting on you'], ['done', 'Answered'], ['cancelled', 'Cancelled'], ['failed', 'Failed']];
const CATEGORIES = ['all', 'coding', 'writing', 'research', 'image', 'general', 'simple', 'salesforce', 'system', 'assistant', 'control', 'project'];
const SOURCE = { voice: '🎙 voice', dashboard: 'dashboard', api: 'API', shortcut: 'Shortcut', schedule: 'schedule', recipe: 'recipe' };

// How the request ended: the routing status, refined by what the task did.
function outcome(r) {
  if (r.task_status === 'done') return ['Done', 'var(--ok)'];
  if (r.task_status === 'error' || r.task_status === 'needs_review') return ['Failed', 'var(--err)'];
  if (r.task_status === 'running') return ['Running', 'var(--run)'];
  if (r.task_status === 'rate_limited') return ['Rate-limited', 'var(--sleep)'];
  if (r.task_status === 'cancelled' || r.status === 'cancelled') return ['Cancelled', 'var(--cancelled)'];
  if (r.task_status === 'queued') return ['Queued', 'var(--queued)'];
  if (r.status === 'proposed') return ['Waiting on you', 'var(--wait)'];
  if (r.status === 'needs_input') return ['Needs an answer', 'var(--wait)'];
  if (r.status === 'expired') return ['Expired', 'var(--text-meta)'];
  if (r.status === 'failed') return ['No model', 'var(--err)'];
  return [r.status, 'var(--text-meta)'];
}

const took = r => (r.duration_ms == null ? '' : r.task_lane === 'agent' ? mmss(r.duration_ms / 1000) : secs(r.duration_ms));

function chain(r) {
  const p = r.plan || {};
  const bits = [
    `<b class="rt-cat">${esc(r.category || p.category || '?')}</b>`,
    p.project && !p.project.is_general ? esc(p.project.name) : null,
    esc(r.agent_label || (p.agent && p.agent.label) || ''),
    esc(r.model_label || (p.model && p.model.label) || 'no model'),
    (p.tools || []).length ? esc(p.tools.map(t => t.label).join(' + ')) : null,
  ].filter(Boolean);
  return bits.join('<span class="rt-arrow">→</span>');
}

function detail(r) {
  const p = r.plan || {};
  const rows = [
    ['Classified', `${esc(p.category || r.category)} by ${esc(p.classified_by || 'rules')}${p.classification_reason ? ' · ' + esc(p.classification_reason) : ''}`],
    ['Reason', esc(p.reason || '')],
    ['Model choice', esc((p.model && p.model.why) || p.unavailable || '')],
    ['Decided by', r.decided_by === 'user' ? 'you (a plan you approved or overrode)' : 'Octopus'],
    ['Result', esc(r.result_excerpt || r.task_error || '')],
    ['Usage', [r.input_tokens != null ? `${r.input_tokens} in / ${r.output_tokens || 0} out tokens` : '', r.cost_usd != null ? fmtCost(r.cost_usd) : (p.est_cost_usd != null ? 'about $' + p.est_cost_usd : '')].filter(Boolean).join(' · ')],
  ].filter(([, v]) => v);
  return `<div class="rt-detail">
    ${rows.map(([k, v]) => `<div class="plan-row"><span class="plan-row__k">${k.toUpperCase()}</span><span class="plan-row__v">${v}</span></div>`).join('')}
    ${p.permissions ? permissionRows(p) : ''}
    ${p.candidates && p.candidates.length ? candidates(p) : ''}
    <div class="rt-detail__acts">
      ${r.task_id ? `<button class="btn btn--sm" data-action="openChat" data-id="${r.task_id}">Open task #${r.task_id}</button>` : ''}
      ${r.status === 'proposed' || r.status === 'needs_input' ? `<button class="btn btn--sm btn--primary" data-action="planReview" data-id="${r.id}">Review plan</button>` : ''}
      <button class="btn btn--sm btn--ghost" data-action="askAgain" data-text="${esc(r.request)}">Ask again</button>
    </div>
  </div>`;
}

export function routing(c) {
  const f = c.ui.octo.routing;
  const rows = c.d.octo.routing;
  const list = (rows || []).filter(r => r.status !== 'expired' || f.status === 'all');
  return `<div class="page page--narrow page--routing" data-key="page-routing">
    <div class="rt-intro"><span class="rt-intro__t">Routing history</span><span class="meta">Every request, how Octopus classified it, and where it went. Tap a row for the reasoning.</span></div>
    <div class="act-filters" data-noswipe>${STATUSES.map(([k, l]) => `<button class="act-chip${f.status === k ? ' is-on' : ''}" data-action="routingFilter" data-k="status" data-v="${k}">${l}</button>`).join('')}</div>
    <div class="act-filters" data-noswipe>${CATEGORIES.map(k => `<button class="act-chip${f.category === k ? ' is-on' : ''}" data-action="routingFilter" data-k="category" data-v="${k}">${k === 'all' ? 'Any type' : k}</button>`).join('')}</div>
    ${rows == null ? '<div class="empty">Loading…</div>' : list.length ? `<div class="list rt-list">${list.map(r => {
      const [label, color] = outcome(r);
      const open = f.open === r.id;
      const p = r.plan || {};
      return `<div class="rt-row${open ? ' is-open' : ''}" data-key="rt${r.id}">
        <button class="rt-row__head" data-action="routingOpen" data-id="${r.id}" aria-expanded="${open}">
          <span class="rt-row__req truncate">${esc(r.request)}</span>
          <span class="rt-row__chain">${chain(r)}</span>
          <span class="meta-row"><span class="rt-status" style="--c:${color}">${label}</span>${p.risk && p.risk !== 'low' ? riskPill(p.risk) : ''}<span class="truncate">${[took(r), r.cost_usd != null ? fmtCost(r.cost_usd) : '', SOURCE[r.source] || r.source, r.decided_by === 'user' ? 'you chose' : '', ago(r.created_at, c.now)].filter(Boolean).join(' · ')}</span></span>
        </button>
        ${open ? detail(r) : ''}
      </div>`;
    }).join('')}</div>` : `<div class="empty">Nothing routed${f.status !== 'all' || f.category !== 'all' ? ' with these filters' : ' yet'}.</div>`}
  </div>`;
}
