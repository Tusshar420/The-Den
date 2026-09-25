// AI Providers: the models Octopus can route to, grouped by provider - what each can do, whether it is
// usable right now (key set, rate-limited, switched off), what it has been used for, and which categories
// prefer it. Models themselves live in models.json; the switch here only turns one on or off.

import { plural, fmtCost, secs, mmss } from '../format.js';
import { esc, toggle, clock } from '../ui.js';
import { sessionColor } from './shell.js';

const LANE = { agent: 'Claude Code', api: 'API', assistant: 'tools', control: 'instant' };

function stars(n) { return n ? '●'.repeat(n) + '○'.repeat(5 - n) : '–'; }

function modelRow(m) {
  const u = m.usage || {};
  const status = m.available ? ['Available', 'var(--ok)']
    : m.rate_limited_until ? ['Rate-limited until ' + clock(m.rate_limited_until), 'var(--sleep)']
    : !m.enabled ? ['Switched off', 'var(--text-meta)'] : [m.unavailable_reason || 'Unavailable', 'var(--err-soft)'];
  const caps = [
    m.coding ? `coding <b>${stars(m.coding)}</b>` : '',
    m.reasoning ? `reasoning <b>${stars(m.reasoning)}</b>` : '',
    m.speed ? `speed <b>${stars(m.speed)}</b>` : '',
    m.cost ? `cost <b>${'$'.repeat(m.cost)}</b>` : '',
    m.vision ? 'vision' : '', m.web_search ? 'web search' : '', m.modality === 'image' ? 'makes images' : '',
    m.context_tokens ? Math.round(m.context_tokens / 1000) + 'k context' : '',
    m.scarce ? '<i class="prov-warn">limited resource</i>' : '',
  ].filter(Boolean).join('<span class="prov-sep">·</span>');
  const usage = u.tasks ? `${plural(u.tasks, 'task')} · ${Math.round((u.done || 0) / u.tasks * 100)}% ok${u.avg_duration_ms ? ' · avg ' + (m.lane === 'agent' ? mmss(u.avg_duration_ms / 1000) : secs(u.avg_duration_ms)) : ''}${u.cost_usd ? ' · ' + fmtCost(u.cost_usd) : ''}` : 'not used in the last 30 days';
  return `<div class="prov-model${m.available ? '' : ' is-off'}" data-key="pm${esc(m.id)}">
    <div class="prov-model__main">
      <div class="prov-model__top"><span class="prov-model__name">${esc(m.label)}</span><span class="prov-model__id">${esc(m.id)}</span><span class="prov-lane">${LANE[m.lane] || m.lane}</span></div>
      <span class="prov-model__caps">${caps}</span>
      <span class="prov-model__status"><span class="dot" style="--c:${status[1]};width:7px;height:7px"></span><span style="color:${status[1]}">${esc(status[0])}</span><span class="prov-sep">·</span>${esc(usage)}</span>
      ${m.rate_limit ? `<span class="prov-model__note">${esc(m.rate_limit)}</span>` : ''}
      ${m._note ? `<span class="prov-model__note">${esc(m._note)}</span>` : ''}
    </div>
    ${toggle(m.enabled !== false, 'toggleModel', `data-id="${esc(m.id)}" aria-label="Use ${esc(m.label)}"`)}
  </div>`;
}

function claudeWindow(c) {
  const { w } = c;
  const bar = (label, u, color) => u ? `<div class="usage-row"><div class="usage-row__head"><span>${label} <span class="mono" style="color:${color};font-weight:600">${u.pct}%</span></span><span class="meta">${u.resets ? 'resets ' + clock(u.resets.toISOString()) : ''}</span></div><div class="bar6"><div style="width:${u.pct}%;background:${color}"></div></div></div>` : '';
  if (!w.session && !w.week) return '';
  return `<div class="card card--pad prov-usage"><span class="prov-usage__t">Claude Code allowance</span>
    ${bar('5-hour window', w.session, w.session ? sessionColor(w.session.pct) : '')}${bar('This week', w.week, 'var(--text-meta)')}
    <span class="meta">Octopus keeps this for coding and system work. Writing, questions and images go to the other providers first${w.session && w.session.pct >= 85 ? ' - and the window is nearly used up, so it avoids Claude Code for anything else right now' : ''}.</span></div>`;
}

export function providers(c) {
  const pv = c.d.octo.providers;
  if (!pv) return `<div class="page page--narrow" data-key="page-providers"><div class="empty">Loading…</div></div>`;
  const labelOf = new Map(pv.providers.flatMap(g => g.models.map(m => [m.id, m.label])));
  const cats = Object.entries(pv.categories || {});
  return `<div class="page page--narrow page--providers" data-key="page-providers">
    <div class="rt-intro"><span class="rt-intro__t">AI providers</span><span class="meta">The models Octopus can route to. Add or change one in <code>models.json</code> - no restart, no code. Switch a model off here to take it out of rotation.</span></div>
    ${claudeWindow(c)}
    ${pv.providers.map(g => `<section class="section" data-key="prov-${esc(g.id)}">
      <div class="section__head"><span class="section__title">${esc(g.label)}</span><span class="section__note" style="color:${g.ready ? 'var(--ok)' : 'var(--err-soft)'}">${g.ready ? `${g.available_models} of ${g.models.length} usable` : esc(g.not_ready_reason)}</span></div>
      ${g.note ? `<span class="meta prov-note">${esc(g.note)}</span>` : ''}
      <div class="list">${g.models.map(modelRow).join('')}</div>
    </section>`).join('')}
    <section class="section" data-key="prov-rules">
      <div class="section__head"><span class="section__title">Which model for what</span></div>
      <div class="list">${cats.map(([name, cat]) => `<div class="prov-rule" data-key="rule-${esc(name)}">
        <span class="prov-rule__cat">${esc(name)}</span>
        <span class="prov-rule__models">${(cat.candidates || []).map((id, i) => `<span class="prov-rule__m">${i ? '<i>then</i> ' : ''}${esc(labelOf.get(id) || id)}</span>`).join('')}</span>
        <span class="meta">${cat.transferable === false ? 'waits for its first choice instead of switching' : esc(cat.optimize || 'balanced') + ' weighting'}</span>
      </div>`).join('')}</div>
    </section>
  </div>`;
}
