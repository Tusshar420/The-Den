// AI Providers: the models Octopus can route to, grouped by provider - what each can do, whether it is
// usable right now (key set, rate-limited, switched off), what it has been used for, and which categories
// prefer it. Most models live in models.json; the switch here turns one on or off. Models can also be
// added here (octopus/models.py keeps them in the settings table) - those can be edited and removed too.

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
      <div class="prov-model__top"><span class="prov-model__name">${esc(m.label)}</span><span class="prov-model__id">${esc(m.model && m.model !== m.id ? m.id + ' · ' + m.model : m.id)}</span><span class="prov-lane">${LANE[m.lane] || m.lane}</span>${m.custom ? '<span class="prov-lane prov-lane--mine">added here</span>' : ''}</div>
      <span class="prov-model__caps">${caps}</span>
      <span class="prov-model__status"><span class="dot" style="--c:${status[1]};width:7px;height:7px"></span><span style="color:${status[1]}">${esc(status[0])}</span><span class="prov-sep">·</span>${esc(usage)}</span>
      ${m.rate_limit ? `<span class="prov-model__note">${esc(m.rate_limit)}</span>` : ''}
      ${m._note ? `<span class="prov-model__note">${esc(m._note)}</span>` : ''}
      ${m.custom ? `<span class="prov-model__acts"><button class="btn--text" data-action="mfEdit" data-id="${esc(m.id)}">Edit</button><button class="btn--text acct-remove" data-action="mfRemove" data-id="${esc(m.id)}">Remove</button></span>` : ''}
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

// ---------------------------------------------------------------- API keys (api_keys.py)
// Several keys per provider (two OpenAI accounts, say): the default is used first, and a request
// moves on to the next key when one is rate-limited, out of credit or rejected.
const PREFIX = { openai: 'sk-…', gemini: 'AIza…' };

function keysCard(c, g) {
  const f = (c.ui.octo.keys || {})[g.id] || { label: '', key: '', busy: false, error: '' };
  const added = k => k.env ? `${esc(k.env_var)} in the service settings` : k.added_at ? 'added ' + new Date(k.added_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) : '';
  return `<div class="card card--pad prov-keys" data-key="keys-${esc(g.id)}">
    <span class="prov-usage__t">${esc(g.label)} API keys</span>
    <span class="meta">Octopus uses the default key first. If it's rate-limited, out of credit or rejected, the same request goes to the next key.</span>
    ${g.keys.length ? `<div class="list">${g.keys.map(k => `<div class="prov-key" data-key="key-${esc(k.id)}">
      <div class="prov-key__b"><span class="prov-key__name"><b>${esc(k.label)}</b><span class="prov-model__id">${esc(k.hint)}</span>${k.default ? '<span class="risk-chip risk-chip--ok">default</span>' : ''}</span>
        <span class="meta">${added(k)}</span>${k.error ? `<span class="acct-err">${esc(k.error)}</span>` : ''}</div>
      <span class="acct-actions">${k.default || g.keys.length < 2 ? '' : `<button class="btn btn--sm" data-action="keyDefault" data-id="${esc(k.id)}">Make default</button>`}
        ${k.env ? '' : `<button class="btn--text acct-remove" data-action="keyRemove" data-id="${esc(k.id)}" data-name="${esc(k.label)}">Remove</button>`}</span>
    </div>`).join('')}</div>` : ''}
    <div class="acct-add">
      <span class="set-group__t">Add a key</span>
      <input class="input" placeholder="Name, e.g. Work account" maxlength="60" value="${esc(f.label)}" data-input="octo.keys.${esc(g.id)}.label" aria-label="Key name" autocomplete="off">
      <input class="input input--mono" type="password" placeholder="${PREFIX[g.id] || 'API key'}" value="${esc(f.key)}" data-input="octo.keys.${esc(g.id)}.key" aria-label="API key" autocomplete="off" autocapitalize="off" spellcheck="false">
      ${f.error ? `<span class="acct-err">${esc(f.error)}</span>` : ''}
      <button class="btn btn--sm" data-action="keyAdd" data-provider="${esc(g.id)}"${f.busy ? ' disabled' : ''}>${f.busy ? 'Checking the key…' : 'Add key'}</button>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- Add / edit a model
const PLACEHOLDER = { openai: 'gpt-6-nova', gemini: 'gemini-3.8-pro', claude_code: 'sonnet (blank = Claude Code\'s default)' };
const CAT_LABEL = { coding: 'Coding', salesforce: 'Salesforce', system: 'This Mac', writing: 'Writing', research: 'Research',
  image: 'Pictures', general: 'General questions', simple: 'Quick questions' };

const seg = (k, cur, opts) => `<div class="segmented" data-noswipe>${opts.map(([v, l]) =>
  `<button class="segmented__opt${String(cur) === String(v) ? ' is-on' : ''}" data-action="mfSet" data-k="${k}" data-v="${esc(v)}">${l}</button>`).join('')}</div>`;
const field = (label, body, hint = '') => `<label class="mf-field"><span class="mf-field__l">${label}</span>${body}${hint ? `<span class="meta">${hint}</span>` : ''}</label>`;
const input = (k, f, attrs = '', cls = 'input') => `<input class="${cls}" value="${esc(f[k] ?? '')}" data-input="octo.mf.${k}" autocomplete="off" autocapitalize="off" spellcheck="false" ${attrs}>`;
const rating = (k, f, low = 1) => seg(k, f[k], Array.from({ length: 6 - low }, (_, i) => [i + low, String(i + low)]));

function modelForm(c, pv) {
  const f = c.ui.octo.mf;
  const prov = ((pv.add || {}).providers || []).find(p => p.id === f.provider) || { modalities: ['text'], categories: {} };
  const image = f.modality === 'image', cc = f.provider === 'claude_code';
  const cats = (prov.categories || {})[image ? 'image' : 'text'] || [];
  return `<div class="card card--pad mf" data-key="mf">
    <span class="prov-usage__t">${f.id ? 'Edit ' + esc(f.label || 'model') : 'Add a model'}</span>
    ${field('Name', input('label', f, 'placeholder="e.g. GPT-6 Nova" maxlength="60" aria-label="Name"'))}
    ${field('Provider', seg('provider', f.provider, ((pv.add || {}).providers || []).map(p => [p.id, esc(p.label)])),
      cc ? 'Runs on your Claude subscription with a different model - it shares Claude Code\'s limits.' : 'Uses this provider\'s API keys - its default key first (see the keys under it below).')}
    ${prov.modalities.length > 1 ? field('What it makes', seg('modality', f.modality, [['text', 'Text'], ['image', 'Pictures']])) : ''}
    ${field('Model name', input('model', f, `placeholder="${esc(image ? (f.provider === 'openai' ? 'gpt-image-2' : 'imagen-5') : PLACEHOLDER[f.provider] || '')}" aria-label="Model name"`, 'input input--mono'),
      cc ? 'What you\'d pass to <code>claude --model</code>, e.g. opus or sonnet.' : 'Exactly as the provider\'s API names it.')}
    ${field('Use it for', `<div class="mf-chips">${cats.map(k => `<button class="chip${f.use_for.includes(k) ? ' is-on' : ''}" data-action="mfCat" data-v="${esc(k)}">${esc(CAT_LABEL[k] || k)}</button>`).join('')}</div>`,
      image ? 'Octopus adds it to the picture models it chooses from. Leave it off to only use it when you pick it by hand.'
        : 'Octopus adds it to these kinds of request, ahead of Claude Code. Leave all off to only use it when you pick it by hand.')}
    ${image ? '' : `<div class="mf-grid">${field('Coding', rating('coding', f, 0))}${field('Reasoning', rating('reasoning', f, 0))}</div>`}
    <div class="mf-grid">${field('Cost (5 = priciest)', rating('cost', f))}${field('Speed (5 = fastest)', rating('speed', f))}</div>
    ${image || cc ? '' : `<div class="mf-flags">
      <span class="mf-flag">${toggle(f.vision, 'mfFlag', 'data-k="vision" aria-label="Can read pictures"')}Can read pictures</span>
      <span class="mf-flag">${toggle(f.web_search, 'mfFlag', 'data-k="web_search" aria-label="Can search the web"')}Can search the web</span></div>`}
    ${image ? field('Price per picture (USD, optional)', input('price_image', f, 'inputmode="decimal" placeholder="0.04" aria-label="Price per picture"'))
      : `${cc ? '' : `<div class="mf-grid">${field('Input $ / million tokens', input('price_in', f, 'inputmode="decimal" placeholder="optional" aria-label="Input price"'))}${field('Output $ / million tokens', input('price_out', f, 'inputmode="decimal" placeholder="optional" aria-label="Output price"'))}</div>`}
        ${field('Context size (tokens)', input('context_tokens', f, `inputmode="numeric" placeholder="${cc ? 200000 : 128000}" aria-label="Context size"`))}`}
    ${field('Note (optional)', input('note', f, 'placeholder="e.g. trial key, free until March" maxlength="200" aria-label="Note"'))}
    ${f.error ? `<span class="acct-err">${esc(f.error)}</span>` : ''}
    <div class="acct-actions"><button class="btn btn--sm" data-action="mfSave"${f.busy ? ' disabled' : ''}>${f.busy ? 'Saving…' : f.id ? 'Save changes' : 'Add model'}</button>
      <button class="btn--text btn--muted" data-action="mfCancel">Cancel</button></div>
  </div>`;
}

export function providers(c) {
  const pv = c.d.octo.providers;
  if (!pv) return `<div class="page page--narrow" data-key="page-providers"><div class="empty">Loading…</div></div>`;
  const labelOf = new Map(pv.providers.flatMap(g => g.models.map(m => [m.id, m.label])));
  const cats = Object.entries(pv.categories || {});
  return `<div class="page page--narrow page--providers" data-key="page-providers">
    <div class="rt-intro"><span class="rt-intro__t">AI providers</span><span class="meta">The models Octopus can route to. Switch one off to take it out of rotation, or add your own - no restart, no code. The built-in ones are set in <code>models.json</code>.</span>
      ${c.ui.octo.mf ? '' : '<button class="btn btn--sm mf-open" data-action="mfNew">+ Add a model</button>'}</div>
    ${c.ui.octo.mf ? modelForm(c, pv) : ''}
    ${claudeWindow(c)}
    ${pv.providers.map(g => `<section class="section" data-key="prov-${esc(g.id)}">
      <div class="section__head"><span class="section__title">${esc(g.label)}</span><span class="section__note" style="color:${g.ready ? 'var(--ok)' : 'var(--err-soft)'}">${g.ready ? `${g.available_models} of ${g.models.length} usable` : esc(g.not_ready_reason)}</span></div>
      ${g.note ? `<span class="meta prov-note">${esc(g.note)}</span>` : ''}
      <div class="list">${g.models.map(modelRow).join('')}</div>
      ${g.keys ? keysCard(c, g) : ''}
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
