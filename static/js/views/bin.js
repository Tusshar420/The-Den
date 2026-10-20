// Recycle bin: queue items that were deleted, newest first. Restore puts one back exactly where it was
// (a queued task keeps its place in line); Erase / Empty bin removes them for good. The server erases
// anything left longer than keep_days on its own.

import { ago, taskName } from '../format.js';
import { projectById, projectColor } from '../data.js';
import { esc, icon, emptyState } from '../ui.js';

const FROM = { queued: 'was queued', cancelled: 'was cancelled', done: 'was done', error: 'had failed', needs_review: 'needed review' };

function binRow(t, busy) {
  const p = projectById(t.project_id);
  const rest = [FROM[t.deleted_from] || '', t.replies ? '+ ' + (t.replies === 1 ? '1 reply' : t.replies + ' replies') : '', 'deleted ' + ago(t.deleted_at)]
    .filter(Boolean).join(' · ');
  const wait = busy['bin' + t.id];
  return `<div class="qrow binrow" data-key="bin${t.id}">
    <span class="binrow__ic">${icon('trash', 16)}</span>
    <span class="qrow__swatch" style="background:${projectColor(p)}"></span>
    <div class="qrow__body">
      <span class="qrow__title truncate">${esc(taskName(t))}</span>
      <div class="meta-row"><span class="nowrap">${esc(p ? p.name : t.project_name)}</span><span class="truncate">· ${esc(rest)}</span></div>
    </div>
    <div class="binrow__acts">
      <button class="btn btn--sm" data-action="restoreBin" data-id="${t.id}"${wait ? ' disabled' : ''}>Restore</button>
      <button class="row-icon" title="Erase for good" aria-label="Erase for good" data-action="eraseBin" data-id="${t.id}"${wait ? ' disabled' : ''}>${icon('close', 15)}</button>
    </div>
  </div>`;
}

export function bin(c) {
  const b = c.d.bin, items = b.items || [];
  return `<div class="page page--narrow page--bin" data-key="page-bin">
    <section class="section" data-key="bin-list">
      <div class="section__head"><span class="section__title">Recycle bin <span class="section__count">${items.length || ''}</span></span>
        ${items.length ? `<button class="btn--text bin__empty" data-action="emptyBin">Empty bin</button>` : ''}</div>
      <span class="bin__note">Deleted queue items wait here for ${b.keep_days || 30} days, then they're erased. Restore puts one back where it was.</span>
      ${!b.loaded ? `<div class="empty">Loading…</div>`
        : items.length ? `<div class="list">${items.map(t => binRow(t, c.ui.busy)).join('')}</div>`
        : emptyState({ title: 'The bin is empty', body: 'Deleted queue items wait here so you can restore them before they are erased.', label: 'Back to activity', attrs: 'data-action="go" data-view="activity"' })}
    </section>
  </div>`;
}
