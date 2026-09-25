// Activity: two sections - the queue (same rows and controls as Home's Up next), then history:
// every finished chat, grouped Today / Yesterday / Earlier.

import { threadSummaries, finishedAt } from '../data.js';
import { histRow } from '../ui.js';
import { upNext } from './home.js';

export const ACTIVITY_FILTERS = [['all', 'All'], ['done', 'Done'], ['error', 'Failed'], ['needs_review', 'Review'], ['cancelled', 'Cancelled']];

export function activity(c) {
  const f = c.ui.activityFilter;
  const hist = threadSummaries(c.d).filter(t => ['done', 'cancelled', 'error', 'needs_review'].includes(t.status))
    .filter(t => f === 'all' || t.status === f)
    .sort((a, b) => finishedAt(b).localeCompare(finishedAt(a)));
  const now = new Date();
  const today0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const groups = [
    ['TODAY', t => new Date(finishedAt(t)).getTime() >= today0],
    ['YESTERDAY', t => { const x = new Date(finishedAt(t)).getTime(); return x < today0 && x >= today0 - 86400000; }],
    ['EARLIER', t => new Date(finishedAt(t)).getTime() < today0 - 86400000],
  ].map(([label, test]) => ({ label, rows: hist.filter(test) })).filter(g => g.rows.length);
  return `<div class="page page--narrow page--activity" data-key="page-activity">
    ${upNext(c)}
    <section class="act-history" data-key="act-history">
      <div class="section__head"><span class="section__title">History <span class="section__count">${hist.length || ''}</span></span></div>
      <div class="act-filters" data-noswipe>${ACTIVITY_FILTERS.map(([k, l]) => `<button class="act-chip${f === k ? ' is-on' : ''}" data-action="activityFilter" data-f="${k}">${l}</button>`).join('')}</div>
      ${groups.length ? groups.map((g, i) => `<div class="section act-group" style="animation-delay:${i * 80}ms" data-key="ag-${g.label}">
        <span class="act-group__label">${g.label}</span>
        <div class="list">${g.rows.map(t => histRow(t)).join('')}</div>
      </div>`).join('') : `<div class="empty">${f === 'all' ? 'Nothing has finished yet.' : 'Nothing here.'}</div>`}
    </section>
  </div>`;
}
