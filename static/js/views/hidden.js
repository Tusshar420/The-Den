// Hidden: projects and queue items you've tucked away. They're out of every other list and count;
// they only show here, with Unhide to put one back. Hiding is display only - a hidden project's
// queued tasks still run.

import { ago, taskName } from '../format.js';
import { projectColor } from '../data.js';
import { esc } from '../ui.js';

function projectRow(p) {
  return `<div class="qrow binrow" data-key="hp${p.id}">
    <span class="qrow__swatch" style="background:${projectColor(p)}"></span>
    <div class="qrow__body">
      <span class="qrow__title truncate">${esc(p.name)}</span>
      <div class="meta-row"><span class="truncate">${esc(p.path || '')}</span></div>
    </div>
    <div class="binrow__acts"><button class="btn btn--sm" data-action="unhideProject" data-id="${p.id}">Unhide</button></div>
  </div>`;
}

function taskRow(t) {
  return `<div class="qrow binrow" data-key="ht${t.id}">
    <span class="qrow__swatch" style="background:${projectColor({ id: t.project_id, name: t.project_name })}"></span>
    <div class="qrow__body">
      <span class="qrow__title truncate">${esc(taskName(t))}</span>
      <div class="meta-row"><span class="nowrap">${esc(t.project_name || '')}</span><span class="truncate">· ${esc(t.status)} · ${esc(ago(t.created_at))}</span></div>
    </div>
    <div class="binrow__acts"><button class="btn btn--sm" data-action="unhideTask" data-id="${t.id}">Unhide</button></div>
  </div>`;
}

export function hidden(c) {
  const h = c.d.hidden, projects = c.d.hiddenProjects || [], tasks = (h.tasks || []).filter(t => !t.thread_id);
  const section = (title, n, body) => `<section class="section"><div class="section__head"><span class="section__title">${title} <span class="section__count">${n || ''}</span></span></div>${body}</section>`;
  return `<div class="page page--narrow page--bin" data-key="page-hidden">
    <span class="bin__note">Hidden projects and queue items only show up here. Hiding doesn't stop anything - queued tasks still run.</span>
    ${section('Projects', projects.length, projects.length ? `<div class="list">${projects.map(projectRow).join('')}</div>` : '<div class="empty">No hidden projects.</div>')}
    ${section('Queue items', tasks.length, !h.loaded ? '<div class="empty">Loading…</div>' : tasks.length ? `<div class="list">${tasks.map(taskRow).join('')}</div>` : '<div class="empty">No hidden queue items.</div>')}
  </div>`;
}
