// Settings: how much Octopus may do on its own, when it shows its plan, what each capability is allowed,
// how it talks, and (read-only) which agents and tools it has. Agents live in agents.json and tools in
// tools.json - edit and save, no restart.

import { esc } from '../ui.js';
import { tourSettings } from './tour.js';
import { accountsSection } from './accounts.js';
import { timelineSettings } from './calendar.js';
import { backupSection } from './backup.js';
import { sitesSection } from './sites.js';
import { SHORTCUTS, FIXED, bindingOf, isDefault, label } from '../shortcuts.js';

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

function shortcutsSection(c) {
  const rec = c.ui.shortcutRec, msg = c.ui.shortcutMsg;
  const rows = SHORTCUTS.map(([id, what]) => `<div class="key-row set-key${rec === id ? ' is-rec' : ''}" data-key="sk-${id}"><span>${esc(what)}</span>
    <span class="set-key__act">${rec === id ? '<span class="kbd kbd--rec">Press keys… · Esc cancels · ⌫ turns off</span>' : `<span class="kbd">${esc(label(bindingOf(id)))}</span>`}
      ${isDefault(id) ? '' : `<button class="btn btn--sm btn--ghost" data-action="shortcutReset" data-id="${id}">Reset</button>`}
      <button class="btn btn--sm" data-action="shortcutRecord" data-id="${id}">${rec === id ? 'Cancel' : 'Change'}</button></span></div>`).join('');
  return `<span class="meta prov-note">Click Change, then press the keys you want. Saved on this device.</span>
    ${msg ? `<span class="meta set-key__msg">${esc(msg)}</span>` : ''}
    <div class="list">${rows}</div>
    <div class="set-group"><span class="set-group__t">Built in</span>${FIXED.map(([k, what]) => `<div class="key-row"><span>${esc(what)}</span><span class="kbd">${esc(k.includes('+') || k === 'Escape' ? label(k) : k)}</span></div>`).join('')}</div>`;
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

const TABS = [['general', 'General'], ['octopus', 'Octopus'], ['sites', 'Sites'], ['agents', 'Agents & tools'], ['schedule', 'Calendar & voice'], ['shortcuts', 'Shortcuts'], ['backup', 'Backup']];

const sec = (key, title, body, extra = '') => `<section class="section" data-key="${key}"><div class="section__head"><span class="section__title">${title}</span>${extra}</div>${body}</section>`;

export function settings(c) {
  const s = c.d.settings || {};
  const tab = TABS.some(t => t[0] === c.ui.settingsTab) ? c.ui.settingsTab : 'general';
  const panes = {
    general: sec('set-account', 'Claude account', accountsSection(c)) + sec('set-tour', 'Tour and help', tourSettings()),
    octopus: sec('set-autonomy', 'Autonomy', `<span class="meta prov-note">How much Octopus may do without asking. Dangerous things - deploying, deleting, touching production, sending email - stay behind your approval unless you set them to Allow below.</span>
      ${radio('setAutonomy', s.autonomy || 'execute', AUTONOMY)}`)
      + sec('set-review', 'Plan preview', radio('setPlanReview', s.plan_review || 'risky', PLAN_REVIEW))
      + sec('set-perms', 'Permissions', `<span class="meta prov-note">What each kind of action is allowed to do. Claude Code's individual commands are still approved one by one on your phone; these say what a whole kind of action may do.</span>
      ${permissions(c)}`),
    sites: sec('set-sites', 'Learn a website', sitesSection(c)),
    agents: sec('set-agents', 'Agents', agentsSection(c), '<span class="section__note">agents.json</span>')
      + sec('set-tools', 'Tools', `<span class="meta prov-note">A task is only given the tools its request needs - never every tool.</span>${toolsSection(c)}`, '<span class="section__note">tools.json</span>'),
    schedule: sec('set-calendar', 'Calendar and briefing', timelineSettings(c)) + sec('set-voice', 'Voice', voiceSection()),
    shortcuts: sec('set-keys', 'Shortcuts', shortcutsSection(c), '<button class="btn btn--sm btn--ghost" data-action="shortcutResetAll">Reset all</button>'),
    backup: sec('set-backup', 'Export and backup', backupSection(c)),
  };
  return `<div class="page page--narrow page--settings" data-key="page-settings">
    <div class="segmented set-tabs" role="tablist" data-noswipe>${TABS.map(([k, l]) => `<button class="segmented__opt${tab === k ? ' is-on' : ''}" role="tab" aria-selected="${tab === k}" data-action="settingsTab" data-tab="${k}">${l}</button>`).join('')}</div>
    ${panes[tab]}
  </div>`;
}
