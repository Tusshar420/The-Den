// The OCTOPUS PLAN card: what Octopus understood and decided before anything runs.
// Shown as a sheet when a request needs a look first (risky, blocked, needs an answer, or you asked to
// see every plan), and read out by voice ("Say yes to go ahead"). [Cancel] [Run], plus Advanced
// overrides for project / model / agent. Everything here is data from POST /octopus/plan.

import { modelState } from '../format.js';
import { esc, spinner, octo } from '../ui.js';
import { projectColor, orderedProjects } from '../data.js';

const POLICY = { allow: ['runs automatically', 'var(--ok)'], ask: ['asks first', 'var(--wait)'], deny: ['blocked', 'var(--err)'] };
const RISK = { low: ['low risk', 'risk-chip--ok'], medium: ['medium risk', ''], high: ['high risk', 'risk-chip--warn'] };
const LANES = { agent: 'works inside the project', api: 'answers directly', assistant: 'your email · calendar · reminders', control: 'instant' };
const PARTS = { quality: 'strength', cost: 'low cost', speed: 'speed', history: 'track record', preference: 'your order', scarcity: 'saves Claude Code' };

const row = (k, v, extra = '') => `<div class="plan-row${extra}"><span class="plan-row__k">${k}</span><span class="plan-row__v">${v}</span></div>`;
const pill = (text, cls = '') => `<span class="risk-chip ${cls}">${esc(text)}</span>`;

export function riskPill(risk) {
  const [l, cls] = RISK[risk] || RISK.low;
  return pill(l, cls);
}

// One line per candidate model with its score, so "why this one?" has an answer.
export function candidates(plan) {
  const rows = plan.candidates || [];
  if (!rows.length) return '';
  const max = Math.max(1, ...rows.filter(r => r.score != null).map(r => r.score));
  return `<div class="plan-why">
    <span class="plan-why__head">How Octopus chose · ${esc(plan.optimize || 'balanced')} weighting</span>
    ${rows.map(r => {
      const win = plan.model && r.id === plan.model.id;
      const parts = Object.entries(r.parts || {}).filter(([, v]) => v).sort((a, b) => b[1] - a[1]).slice(0, 3)
        .map(([k, v]) => `${PARTS[k] || k} ${v > 0 ? '+' : ''}${Math.round(v)}`).join(' · ');
      return `<div class="plan-cand${win ? ' is-win' : ''}${r.available ? '' : ' is-off'}">
        <span class="plan-cand__name">${win ? '✓ ' : ''}${esc(r.label)}${r.scarce ? ' <i>limited</i>' : ''}</span>
        ${r.available ? `<span class="plan-cand__bar"><span style="width:${Math.max(4, Math.round((r.score || 0) / max * 100))}%"></span></span><span class="plan-cand__score">${Math.round(r.score || 0)}</span>`
          : `<span class="plan-cand__skip">skipped · ${esc(r.reason)}</span>`}
        ${r.available && parts ? `<span class="plan-cand__parts">${esc(parts)}</span>` : ''}
      </div>`;
    }).join('')}
  </div>`;
}

export function permissionRows(plan) {
  const rows = plan.permissions || [];
  if (!rows.length) return '';
  return `<div class="plan-perms">${rows.map(p => {
    const [l, color] = POLICY[p.policy] || POLICY.ask;
    return `<div class="plan-perm${p.flagged ? ' is-flagged' : ''}"><span class="plan-perm__name">${esc(p.label)}${p.flagged ? ' <i>in your request</i>' : ''}</span><span class="plan-perm__pol" style="color:${color}">${l}</span></div>`;
  }).join('')}</div>`;
}

function overrides(c, st) {
  const ov = st.over;
  const plan = st.plan;
  const projects = orderedProjects(c.d).filter(p => !p.is_general && p.active);
  const models = c.d.models.filter(m => m.lane !== 'control').map(m => modelState(m, c.now));
  const agents = c.d.octo.agents;
  const chip = (action, id, label, on, extra = '') => `<button class="pal-chip${on ? ' is-on' : ''}" data-action="${action}" data-id="${esc(id)}"${extra}>${label}</button>`;
  const currentProject = ov.project ?? (plan.project && !plan.project.is_general ? plan.project.id : null);
  return `<div class="plan-adv">
    <span class="plan-adv__label">PROJECT</span>
    <div class="pal-chips" data-noswipe>${chip('planProject', '', 'None', currentProject == null)}${projects.map(p =>
      chip('planProject', p.id, `<span class="swatch-sq" style="background:${projectColor(p)}"></span>${esc(p.name)}`, currentProject === p.id)).join('')}</div>
    <span class="plan-adv__label">MODEL</span>
    <div class="pal-chips" data-noswipe>${chip('planModel', 'auto', 'Auto', !ov.model)}${models.map(m => chip('planModel', m.id,
      `<span class="dot" style="--c:${m.dot};width:7px;height:7px"></span>${esc(m.label)}`, ov.model === m.id, ` style="opacity:${m.available || m.rate ? 1 : 0.5}"`)).join('')}</div>
    <span class="plan-adv__label">AGENT</span>
    <div class="pal-chips" data-noswipe>${chip('planAgent', 'auto', 'Auto', !ov.agent)}${agents.map(a => chip('planAgent', a.id, esc(a.label), ov.agent === a.id)).join('')}</div>
  </div>`;
}

export function planSheet(c) {
  const st = c.ui.octo.plan;
  if (!st) return '';
  const p = st.plan;
  const need = p.needs_input;
  const blocked = (p.blocked || []).length > 0;
  const ready = !need && p.model && !blocked;
  const project = p.project && !p.project.is_general
    ? `${esc(p.project.name)}${p.project.how ? ` <span class="plan-how">${esc(p.project.how)}</span>` : ''}`
    : '<span class="plan-none">none needed</span>';
  const model = p.model ? `${esc(p.model.label)} <span class="plan-how">${esc(LANES[p.lane] || p.lane)}</span>` : `<span class="plan-none">${esc(p.unavailable || 'none available')}</span>`;
  const tools = (p.tools || []).length ? p.tools.map(t => pill(t.label)).join('') : '<span class="plan-none">none</span>';
  const cost = p.est_cost_usd != null ? ` · about $${p.est_cost_usd < 0.01 ? '<0.01' : p.est_cost_usd.toFixed(2)}` : '';
  const why = p.show_plan_reason ? p.show_plan_reason[0].toUpperCase() + p.show_plan_reason.slice(1) : 'Here is what I would do';
  const modeText = p.permission_mode === 'plan' ? 'Read-only: investigates and proposes, changes nothing'
    : p.permission_mode === 'manual' ? 'Asks before every edit and command' : 'Edits files freely; asks before risky commands';
  const inner = `
    <div class="plan__top">${octo(st.busy ? 'dance' : 'wait', 40)}<div class="plan__titles"><span class="plan__label">OCTOPUS PLAN</span><span class="plan__why">${esc(why)}</span></div>${riskPill(p.risk)}</div>
    <div class="plan__body">
      ${row('TASK', esc(p.title))}
      ${row('TYPE', `${esc(p.category)} <span class="plan-how">${esc(p.classification_reason || '')}</span>`)}
      ${row('PROJECT', project)}
      ${row('AGENT', esc(p.agent.label))}
      ${row('MODEL', model)}
      ${row('TOOLS', `<span class="plan-tools">${tools}</span>`)}
      ${row('REASON', esc(p.reason) + esc(cost))}
      ${p.model && p.model.why ? row('WHY THIS MODEL', esc(p.model.why), ' plan-row--sub') : ''}
      ${p.permission_mode ? row('CLAUDE CODE', esc(modeText)) : ''}
      ${permissionRows(p)}
      ${need ? `<div class="plan-ask"><span class="plan-ask__q">${esc(need.question)}</span><div class="pal-chips" data-noswipe>${need.options.map(o =>
        `<button class="pal-chip${st.over.project === o.id ? ' is-on' : ''}" data-action="planProject" data-id="${o.id}">${esc(o.name)}</button>`).join('')}</div></div>` : ''}
      ${blocked ? `<div class="plan-blocked">Blocked by your permission settings: ${esc(p.blocked.join(', '))}. Change it in Settings › Permissions to run this.</div>` : ''}
      ${st.error ? `<div class="plan-blocked">${esc(st.error)}</div>` : ''}
      <div class="plan-toggles">
        <button class="btn--text" data-action="planWhy">${st.why ? 'Hide' : 'Show'} how it chose</button>
        <button class="btn--text" data-action="planAdvanced">${st.advanced ? 'Hide' : 'Advanced'} overrides</button>
      </div>
      ${st.why ? candidates(p) : ''}
      ${st.advanced ? overrides(c, st) : ''}
    </div>
    <div class="plan__actions">
      <button class="btn btn--ghost" data-action="planCancel"${st.busy ? ' disabled' : ''}>Cancel</button>
      <button class="btn btn--primary" data-action="planRun"${ready && !st.busy ? '' : ' disabled'}>${st.busy ? spinner() + 'Starting…' : 'Run'}</button>
    </div>`;
  return `<div class="scrim" data-action="planClose" data-key="sheet-plan"><div class="sheet sheet--plan" role="dialog" aria-modal="true" aria-label="Octopus plan">${inner}</div></div>`;
}

// A compact line for lists (Home › plans waiting).
export function planLine(w) {
  const p = w.plan;
  return `<div class="plan-wait" data-key="pw${w.routing_id}">
    <div class="plan-wait__body"><span class="plan-wait__title truncate">${esc(p.title)}</span>
      <span class="meta">${esc(p.agent.label)} · ${esc(p.model ? p.model.label : 'no model')}${p.project && !p.project.is_general ? ' · ' + esc(p.project.name) : ''} · ${w.status === 'needs_input' ? 'needs an answer' : esc(p.risk + ' risk')}</span></div>
    <button class="btn btn--sm" data-action="planReview" data-id="${w.routing_id}">Review</button>
  </div>`;
}
