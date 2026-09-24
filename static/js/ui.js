// Small markup helpers shared by the views. Pure functions: data in, HTML string out.
// Every user- or model-supplied string goes through esc().

import { store } from './api.js';
import { escapeHTML, modelChip, modelChipHTML, statusOf, agoShort, ago, mmss, secs, fmtCost } from './format.js';
import { octopusSVG } from './octopus.js';
import { icon } from './icons.js';
import { projectById, projectColor } from './data.js';

export const esc = escapeHTML;
export { icon };

// Octopus at a fixed pixel size (the SVG fills its box).
export const octo = (mood, size, extra = '') =>
  `<span class="octo-box" style="width:${size}px;height:${size}px${extra}">${octopusSVG(mood)}</span>`;

export const spinner = (cls = '') => `<span class="spinner ${cls}"></span>`;
export const dot = color => `<span class="dot" style="--c:${color}"></span>`;
export const swatch = (color, cls = 'swatch-sq') => `<span class="${cls}" style="background:${color}"></span>`;

export const chipFor = t => modelChipHTML(modelChip(t, store.data.models));
export const modelLabel = id => (store.data.models.find(m => m.id === id) || {}).label || (id === 'claude-code' || !id ? 'Claude Code' : id);
export const laneColor = lane => (lane === 'api' ? 'var(--lane-api-fg)' : 'var(--lane-agent-fg)');

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
export const took = t => (t.duration_ms == null ? '' : t.lane === 'api' ? secs(t.duration_ms) : mmss(t.duration_ms / 1000));

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

// ---------------------------------------------------------------- history row (Recent, Activity, Done recently)
// t is a thread summary (data.threadSummaries) or a plain task.
export function histRow(t, { project = true, key = 'h' } = {}) {
  const p = projectById(t.project_id);
  const [g, gc] = glyph(t);
  const rest = [agoShort(t.finished_at || t.created_at), took(t), fmtCost(t.cost_usd)].filter(Boolean).join(' · ');
  return `<div class="row hrow" data-key="${key}${t.id}">
    <span class="row__glyph" style="color:${gc}">${g}</span>
    <div class="row__body" data-action="openChat" data-id="${t.id}">
      <span class="row__title truncate">${esc(t.prompt)}</span>
      <div class="meta-row">${project ? `<span class="nowrap">${esc(p ? p.name : t.project_name)}</span>` : ''}${chipFor(t)}<span class="truncate">${project ? '· ' : ''}${esc(rest)}</span></div>
    </div>
    <button class="row-icon" title="Run again" aria-label="Run again" data-action="runAgain" data-id="${t.id}">↻</button>
  </div>`;
}

export const projectSwatch = p => swatch(projectColor(p));
export { ago, agoShort, statusOf, fmtCost, mmss, secs };
