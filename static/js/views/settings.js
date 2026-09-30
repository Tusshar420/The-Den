// Settings: how much Octopus may do on its own, when it shows its plan, what each capability is allowed,
// how it talks, and (read-only) which agents and tools it has. Agents live in agents.json and tools in
// tools.json - edit and save, no restart.

import { esc } from '../ui.js';
import { accountsSection } from './accounts.js';
import { timelineSettings } from './calendar.js';

const AUTONOMY = [
  ['assist', 'Assist', 'Investigates and proposes. Coding tasks run read-only, and anything that would change something asks first.'],
  ['execute', 'Execute', 'Normal actions run on their own; risky ones ask first. The everyday setting.'],
  ['autonomous', 'Autonomous', 'Also runs what would normally ask - except protected actions (deploys, force-push, deleting, production, sending email, admin), which always ask.'],
];
const PLAN_REVIEW = [
  ['risky', 'Standard', 'Show the plan for risky requests, questions Octopus needs answered, and agents that always show theirs.'],
  ['always', 'Every request', 'Show the plan first for everything, then Run.'],
  ['never', 'Only risky requests', 'Skip the plan for everyday requests. Risky, blocked and unanswered ones still stop for you.'],
];
const GROUPS = { coding: 'Coding agent', assistant: 'Email, calendar and reminders', web: 'Web', media: 'Images', workspace: 'Octopus itself' };
const POLICIES = [['allow', 'Allow'], ['ask', 'Ask'], ['deny', 'Deny']];
const RISK = { low: '', medium: 'risk-chip', high: 'risk-chip risk-chip--warn' };

const radio = (action, current, opts) => `<div class="set-radio" role="radiogroup">${opts.map(([k, l, help]) =>
  `<button class="set-radio__opt${current === k ? ' is-on' : ''}" role="radio" aria-checked="${current === k}" data-action="${action}" data-v="${k}">
    <span class="set-radio__dot"></span><span class="set-radio__txt"><b>${l}</b><span>${esc(help)}</span></span></button>`).join('')}</div>`;

function permissions(c) {
  const pol = c.d.octo.policies;
  if (!pol) return '<div class="empty">Loading…</div>';
  const groups = Object.keys(GROUPS).filter(g => pol.some(p => p.group === g));
  return groups.map(g => `<div class="set-group"><span class="set-group__t">${GROUPS[g]}</span>
    <div class="list">${pol.filter(p => p.group === g).map(p => `<div class="set-cap" data-key="cap-${p.capability}">
      <div class="set-cap__txt"><span class="set-cap__name">${esc(p.label)}${p.protected ? ' <i class="set-prot" title="Always asks in every autonomy mode unless you set it to Allow yourself">protected</i>' : ''}${RISK[p.risk] ? ` <span class="${RISK[p.risk]}">${p.risk} risk</span>` : ''}</span>
        <span class="meta">${esc(p.help)}${p.effective !== p.policy ? ` <b class="set-eff">→ ${p.effective === 'ask' ? 'asks' : p.effective === 'allow' ? 'runs' : 'blocked'} at the current autonomy level</b>` : ''}</span></div>
      <div class="segmented set-seg" data-noswipe>${POLICIES.map(([k, l]) => `<button class="segmented__opt${p.policy === k ? (k === 'deny' ? ' is-urgent' : ' is-on') : ''}" data-action="octoPolicy" data-cap="${p.capability}" data-policy="${k}">${l}</button>`).join('')}</div>
    </div>`).join('')}</div></div>`).join('');
}

function voiceSection() {
  return `<div class="set-line"><span class="meta">How Octopus listens and speaks, and which voice reads which language.</span><button class="btn btn--sm" data-action="go" data-view="voice">Voice settings</button></div>`;
}

function agentsSection(c) {
  const agents = c.d.octo.agents;
  if (!agents.length) return '<div class="empty">Loading…</div>';
  return `<div class="list">${agents.map(a => `<div class="set-agent" data-key="ag-${a.id}">
    <div class="set-agent__top"><b>${esc(a.label)}</b><span class="prov-lane">${esc((a.execution || {}).lane || '')}</span><span class="meta">${a.categories.map(esc).join(', ')}</span></div>
    <span class="meta">${esc(a.description || '')}</span>
    <div class="set-agent__chips">${a.preferred_models.map((m, i) => `<span class="risk-chip">${i ? '' : '★ '}${esc(m.label)}</span>`).join('')}${a.tools.map(t => `<span class="risk-chip risk-chip--ok">${esc(t.label)}</span>`).join('')}</div>
    <span class="meta">${a.permissions.length ? `may use ${a.permissions.length} capabilities` : 'no capabilities'} · plan card: ${esc((a.execution || {}).show_plan || 'risky')}${(a.execution || {}).max_cost_usd ? ` · asks over $${a.execution.max_cost_usd}` : ''}${a.context.needs_project ? ' · needs a project' : ''}</span>
  </div>`).join('')}</div>`;
}

function toolsSection(c) {
  const tools = c.d.octo.tools;
  if (!tools.length) return '<div class="empty">Loading…</div>';
  return `<div class="list">${tools.map(t => `<div class="set-agent${t.enabled ? '' : ' is-off'}" data-key="tl-${t.id}">
    <div class="set-agent__top"><b>${esc(t.label)}</b><span class="prov-lane">${t.kind === 'mcp' ? 'MCP server' : 'built in'}</span>${t.enabled ? '' : '<span class="meta">not connected</span>'}</div>
    <span class="meta">${esc(t.description || '')}</span>
    <div class="set-agent__chips">${t.capabilities.map(k => `<span class="risk-chip${k.policy === 'deny' ? ' risk-chip--warn' : k.policy === 'allow' ? ' risk-chip--ok' : ''}" title="${esc(k.label)} · ${esc(k.policy)}">${esc(k.label)}</span>`).join('')}</div>
    <span class="meta">given to: ${t.agents.length ? t.agents.map(esc).join(', ') : 'no agent'}</span>
  </div>`).join('')}</div>`;
}

export function settings(c) {
  const s = c.d.settings || {};
  return `<div class="page page--narrow page--settings" data-key="page-settings">
    <section class="section" data-key="set-account"><div class="section__head"><span class="section__title">Claude account</span></div>${accountsSection(c)}</section>
    <section class="section" data-key="set-autonomy"><div class="section__head"><span class="section__title">Autonomy</span></div>
      <span class="meta prov-note">How much Octopus may do without asking. Dangerous things - deploying, deleting, touching production, sending email - stay behind your approval unless you set them to Allow below.</span>
      ${radio('setAutonomy', s.autonomy || 'execute', AUTONOMY)}</section>
    <section class="section" data-key="set-review"><div class="section__head"><span class="section__title">Plan preview</span></div>
      ${radio('setPlanReview', s.plan_review || 'risky', PLAN_REVIEW)}</section>
    <section class="section" data-key="set-perms"><div class="section__head"><span class="section__title">Permissions</span></div>
      <span class="meta prov-note">What each kind of action is allowed to do. Claude Code's individual commands are still approved one by one on your phone; these say what a whole kind of action may do.</span>
      ${permissions(c)}</section>
    <section class="section" data-key="set-calendar"><div class="section__head"><span class="section__title">Calendar and briefing</span></div>${timelineSettings(c)}</section>
    <section class="section" data-key="set-voice"><div class="section__head"><span class="section__title">Voice</span></div>${voiceSection()}</section>
    <section class="section" data-key="set-agents"><div class="section__head"><span class="section__title">Agents</span><span class="section__note">agents.json</span></div>${agentsSection(c)}</section>
    <section class="section" data-key="set-tools"><div class="section__head"><span class="section__title">Tools</span><span class="section__note">tools.json</span></div>
      <span class="meta prov-note">A task is only given the tools its request needs - never every tool.</span>${toolsSection(c)}</section>
  </div>`;
}
