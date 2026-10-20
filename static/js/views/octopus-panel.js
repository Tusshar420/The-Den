// Home, the Den Home parts that are new: what Octopus is doing right now (every running task with its
// project, agent, model, progress and Pause / Play), plans waiting on you, which providers are usable, the autonomy switch and the way to the
// Routing / AI Providers / Settings pages. Plus the extra controls on the Ask box (agent override, plan
// preview, microphone).

import { plural, startedAgo, taskName } from '../format.js';
import { projectById } from '../data.js';
import { esc, chipFor, restartTag, octo, progressBar, icon } from '../ui.js';
import { agentLabel } from '../octopus-data.js';
import { micAvailable, voiceChoices } from '../voice.js';
import { planLine } from './plan-card.js';

const MIC = '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7.2" y="2.5" width="5.6" height="10" rx="2.8"/><path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5"/></svg>';
const SPEAKER = '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8v4h3l4.5 3.5v-11L6.5 8h-3zM14 7.5a3.5 3.5 0 0 1 0 5M16 5.5a6.5 6.5 0 0 1 0 9"/></svg>';
const EYE = '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 10S5 5 10 5s7.5 5 7.5 5-2.5 5-7.5 5-7.5-5-7.5-5z"/><circle cx="10" cy="10" r="2"/></svg>';
const chev = '<svg width="10" height="10" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5l4 4 4-4"/></svg>';

const AUTONOMY = [['assist', 'Assist'], ['execute', 'Execute'], ['autonomous', 'Autonomous']];
const AUTONOMY_HELP = {
  assist: 'Investigates and proposes; changes ask first.',
  execute: 'Normal actions run; risky ones ask.',
  autonomous: 'Runs more on its own; dangerous things still ask.',
};

// ---------------------------------------------------------------- Ask box extras
export function askControls(c) {
  const o = c.ui.octo, a = c.ui.ask;
  const agent = o.agent === 'auto' ? null : c.d.octo.agents.find(x => x.id === o.agent);
  const dis = a.sending ? ' disabled' : '';
  const empty = !a.text.trim();
  const talking = o.voice.conversation || o.voice.state === 'listening';
  const label = talking ? 'End conversation' : 'Talk to Octopus';
  return `<button class="ask__model ask__agent" data-action="askMenu" data-menu="agent" aria-label="Agent"${dis}>${esc(agent ? agent.label : 'Agent: Auto')}${chev}</button>
    <button class="tool-btn" data-action="askPreview" title="Preview Octopus's plan without running it" aria-label="Preview plan"${empty || a.sending ? ' disabled' : ''}>${EYE}</button>
    <button class="tool-btn ask__mic${talking ? ' is-on' : ''}${micAvailable() || talking ? '' : ' is-unsupported'}" data-action="voiceMic" title="${label}" aria-label="${label}" aria-pressed="${talking}">${MIC}</button>
    <button class="tool-btn" data-action="askMenu" data-menu="voice" title="Change Octopus's voice" aria-label="Change voice">${SPEAKER}</button>`;
}

export function askVoiceMenu(c) {
  const voices = voiceChoices({ manual: true }), auto = !voices.some(x => x.on);
  return `<div class="menu-scrim" data-action="closeMenus"></div>
    <div class="menu menu--model menu--voices menu--up menu--anchored" style="--mx:${c.ui.ask.menuX || 0}px" role="menu"><span class="menu__label">OCTOPUS'S VOICE</span>
    <button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${auto}" data-action="voicePick" data-id="">
      <span></span><span class="menu__two"><span class="menu__main">Automatic</span><span class="menu__sub">The default voice for each language</span></span><span class="menu__check">${auto ? '✓' : ''}</span></button>
    ${voices.length ? voices.map(x => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${x.on}" data-action="voicePick" data-id="${esc(x.name)}">
      <span></span><span class="menu__two"><span class="menu__main">${esc(x.name)}</span><span class="menu__sub">${esc(x.language)}</span></span><span class="menu__check">${x.on ? '✓' : ''}</span></button>`).join('')
      : '<span class="meta" style="padding:8px 12px">No voices found on this device.</span>'}
    </div>`;
}

export function askAgentMenu(c) {
  const o = c.ui.octo, agents = c.d.octo.agents;
  const opts = [{ id: 'auto', label: 'Auto - Octopus decides', sub: 'picks the agent for each request' },
    ...agents.filter(a => !['operator'].includes(a.id)).map(a => ({ id: a.id, label: a.label, sub: a.description }))];
  return `<div class="menu-scrim" data-action="closeMenus"></div>
    <div class="menu menu--model menu--up menu--anchored" style="--mx:${c.ui.ask.menuX || 0}px" role="menu"><span class="menu__label">AGENT</span>
    ${opts.map(x => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${o.agent === x.id}" data-action="askAgent" data-id="${x.id}">
      <span class="menu__two"><span class="menu__main">${esc(x.label)}</span><span class="menu__sub">${esc(x.sub || '')}</span></span><span class="menu__check">${o.agent === x.id ? '✓' : ''}</span></button>`).join('')}
    </div>`;
}

// The line under the Ask box while talking, or the answer to an instant command.
export function askVoiceStatus(c) {
  const v = c.ui.octo.voice;
  if (v.state === 'listening') return `<div class="ask__status ask__status--voice">${octo('wait', 22)}<span>Listening…</span><span class="ask__heard">${esc(v.text)}</span><button class="ask__open" data-action="voiceStop">end</button></div>`;
  if (v.state === 'working') return `<div class="ask__status ask__status--routing">${octo('dance', 22)}<span>“${esc(v.heard)}”</span><span>· working on it…</span>${v.conversation ? '<button class="ask__open" data-action="voiceStop">end</button>' : ''}</div>`;
  if (v.state === 'speaking') return `<div class="ask__status ask__status--voice">${octo('idle', 22)}<span>Speaking…</span><button class="ask__open" data-action="voiceStop">${v.conversation ? 'end' : 'stop'}</button></div>`;
  if (v.error) return `<div class="ask__status ask__status--error">${esc(v.error)}</div>`;
  return '';
}

// ---------------------------------------------------------------- the panel
// Everything running, each with its details and its Pause / Play (this used to be a separate "Running now" list on Home).
function nowItem(c, t, size) {
  const p = projectById(t.project_id);
  const steps = t.plan_steps || [];
  const cur = steps.find(s => s.status === 'in_progress');
  const agentLane = t.lane !== 'api';
  const agent = t.agent_id ? agentLabel(t.agent_id) : (t.lane === 'agent' ? 'Coding Agent' : '');
  const waiting = c.w.apTask.has(t.id);
  const on = !!t.paused;
  const step = waiting ? 'Waiting for your OK' : cur ? cur.content : t.current_step;
  return `<div class="op-now-item${on ? ' is-paused' : ''}" data-key="r${t.id}"><button class="op-now" data-action="openChat" data-id="${t.id}">
    ${octo(on ? 'sleep' : waiting ? 'wait' : 'work', size)}
    <span class="op-now__body">
      <span class="op-now__title truncate"><span class="task-num">#${t.id}</span>${esc(taskName(t))}</span>
      ${restartTag(t, 'span')}
      <span class="op-now__facts">
        ${p && !p.is_general ? `<span><i>PROJECT</i>${esc(p.name)}</span>` : ''}
        ${agent ? `<span><i>AGENT</i>${esc(agent)}</span>` : ''}
        <span><i>MODEL</i>${chipFor(t)}</span>
        <span><i>STATUS</i>${on ? 'paused' : t.started_at ? startedAgo(t.started_at, c.now) : 'starting'}</span>
      </span>
      ${agentLane ? progressBar(t, c.now, 'op-now__prog') : ''}
      ${agentLane && step ? `<span class="op-now__step truncate">› ${esc(step)}</span>` : ''}
    </span></button>${agentLane && !waiting ? `<button class="op-now__hold" title="${on ? 'Play - carry on' : 'Pause - freeze it here'}" aria-label="${on ? 'Play' : 'Pause'}" data-action="${on ? 'playTask' : 'pauseTask'}" data-id="${t.id}">${icon(on ? 'play' : 'pause', 16)}</button>` : ''}</div>`;
}

function nowBlock(c) {
  const rows = c.w.running, nq = c.w.queue.length;
  if (!rows.length) return `<div class="op-now op-now--idle">${octo(c.w.mood, 44)}<div class="op-now__body"><span class="op-now__title">Idle</span><span class="meta">${nq ? `${plural(nq, 'task')} waiting in the queue` : 'Nothing running, nothing waiting.'}</span></div></div>`;
  return `<div class="op-now-list">${rows.map(t => nowItem(c, t, rows.length > 1 ? 34 : 44)).join('')}</div>
    ${nq ? `<span class="meta">${plural(nq, 'task')} waiting after ${rows.length > 1 ? 'these' : 'this'}</span>` : ''}`;
}

const block = (title, body, key) => `<div class="op-block"${key ? ` data-key="${key}"` : ''}><div class="op-block__head"><span class="op-block__t">${title}</span></div>${body}</div>`;

function providerChips(c) {
  const ov = c.d.octo.providers;
  if (!ov) return '';
  return `<div class="op-provs">${ov.providers.filter(g => g.id !== 'control').map(g => {
    const ok = g.ready && g.available_models > 0;
    const color = ok ? (g.available_models < g.models.length ? 'var(--wait)' : 'var(--ok)') : 'var(--err-soft)';
    return `<button class="op-prov" data-action="go" data-view="providers" title="${esc(g.ready ? g.available_models + ' of ' + g.models.length + ' models usable' : g.not_ready_reason)}"><span class="dot" style="--c:${color};width:7px;height:7px"></span>${esc(g.label)}<span class="meta">${g.ready ? `${g.available_models}/${g.models.length}` : 'off'}</span></button>`;
  }).join('')}</div>`;
}

// One slim row on Home: what Octopus is doing now (or idle) and how much is waiting. The full controls live in Settings / Providers.
export function octopusPanel(c) {
  const rows = c.w.running, nq = c.w.queue.length;
  const waiting = ((c.d.octo.overview && c.d.octo.overview.plans_waiting) || []).length;
  const t = rows[0];
  const title = t ? `<span class="task-num">#${t.id}</span>${esc(taskName(t))}` : 'Idle';
  const facts = [t ? (t.paused ? 'paused' : t.started_at ? startedAgo(t.started_at, c.now) : 'starting') : '',
    rows.length > 1 ? `+${rows.length - 1} more running` : '',
    nq ? `${nq} in queue` : (t ? '' : 'nothing waiting'),
    waiting ? `${plural(waiting, 'plan')} need you` : ''].filter(Boolean).join(' · ');
  return `<section class="section reveal" style="--i:45" data-key="octo-panel">
    <button class="card op-strip" ${t ? `data-action="openChat" data-id="${t.id}"` : 'data-action="go" data-view="activity"'}>
      ${octo(t ? (t.paused ? 'sleep' : 'work') : c.w.mood, 28)}
      <span class="op-strip__title truncate">${title}</span>
      <span class="op-strip__meta meta truncate">${esc(facts)}</span>
    </button>
  </section>`;
}
