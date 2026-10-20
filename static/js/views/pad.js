// Notes pad: write anything (built for the iPad), file it under a project or leave it loose,
// and save it out as a Markdown file into a project folder, the shared notes folder, or any folder at home.
// Actions and autosave live in pad-ui.js; data.js has loadPad / pad.

import { esc, emptyState, icon } from '../ui.js';
import { agoShort } from '../format.js';
import { padState } from '../pad-ui.js';
import { meetingNotes, meetingWhen, events } from './calendar.js';

const projectName = (c, id) => { const p = c.d.projects.find(x => x.id === id); return p ? p.name : ''; };
const eventOf = (c, id) => events(c.d).find(e => e.id === id);
const preview = text => (text || '').replace(/\s+/g, ' ').trim().slice(0, 110);

// Notes written on calendar items show here too, tagged "Meeting notes"; tapping one opens it full-page.
const meetingRow = (c, e) => `<button class="pad-row" data-action="openMeetingNotes" data-id="${e.id}" data-key="pad-mt-${e.id}">
      <span class="pad-row__title"><span class="pad-tag">Meeting notes</span>${esc(e.title)}</span>
      <span class="pad-row__prev">${esc(preview(e.notes))}</span>
      <span class="pad-row__meta">${esc(projectName(c, e.project_id))} · ${esc(meetingWhen(e))}</span>
    </button>`;

function listView(c, p, items) {
  const q = p.q.trim().toLowerCase();
  const meets = meetingNotes(c.d);
  const used = [...new Set([...items, ...meets].map(n => n.project_id).filter(id => id != null))];
  const inProject = id => n => n.project_id === id;
  const chips = [['all', 'All', items.length + meets.length], ['unfiled', 'Unfiled', items.filter(n => n.project_id == null).length],
    ...(meets.length ? [['meeting', 'Meeting notes', meets.length]] : []),
    ...used.map(id => [String(id), projectName(c, id) || 'Project', items.filter(inProject(id)).length + meets.filter(inProject(id)).length])];
  const hit = text => !q || text.toLowerCase().includes(q);
  const pads = p.filter === 'meeting' ? [] : items.filter(n =>
    (p.filter === 'all' || (p.filter === 'unfiled' ? n.project_id == null : n.project_id === p.filter)) && hit(n.title + ' ' + n.body + ' ' + ((eventOf(c, n.event_id) || {}).title || '')))
    .map(n => ({ at: n.updated_at, html: `<button class="pad-row" data-action="padOpen" data-id="${n.id}" data-key="pad-row-${n.id}">
      <span class="pad-row__title">${esc(n.title || 'Untitled note')}</span>
      <span class="pad-row__prev">${esc(preview(n.body)) || '<i>Empty</i>'}</span>
      <span class="pad-row__meta">${n.event_id != null && eventOf(c, n.event_id) ? '👥 ' + esc(eventOf(c, n.event_id).title) + ' · ' : ''}${n.project_id != null ? esc(projectName(c, n.project_id)) + ' · ' : ''}${n.saved_path ? 'saved as file · ' : ''}${agoShort(n.updated_at)}</span>
    </button>` }));
  const mts = p.filter === 'unfiled' ? [] : meets.filter(e =>
    (p.filter === 'all' || p.filter === 'meeting' || e.project_id === p.filter) && hit(e.title + ' ' + e.notes))
    .map(e => ({ at: e.updated_at, html: meetingRow(c, e) }));
  const shown = [...pads, ...mts].sort((a, b) => (b.at || '').localeCompare(a.at || ''));
  const total = items.length + meets.length;
  const rows = shown.map(r => r.html).join('');
  return `<div class="page page--narrow page--notes" data-key="page-notes">
    <div class="pad-top">
      <input class="input pad-search" data-input="pad.q" value="${esc(p.q)}" placeholder="Search notes" autocomplete="off" data-key="pad-q">
      <button class="btn btn--primary" data-action="padNew">+ New note</button>
    </div>
    <div class="chip-row" data-noswipe>${chips.map(([k, l, n]) => `<button class="chip${String(p.filter) === k ? ' is-on' : ''}" data-action="padFilter" data-v="${k}">${esc(l)} <span class="chip__count">${n}</span></button>`).join('')}</div>
    <section class="section" data-key="pad-list">
      ${shown.length ? `<div class="list pad-list">${rows}</div>`
        : emptyState({ title: total ? 'No notes match' : 'No notes yet', body: total ? 'Try another search or filter.' : 'Tap "New note" and write anything.', label: 'New note', attrs: 'data-action="padNew"' })}
    </section>
  </div>`;
}

function savePanel(c, n, s) {
  const proj = s.project_id ? c.d.projects.find(x => String(x.id) === String(s.project_id)) : null;
  const where = proj ? `Inside ${proj.path}` : 'Shared notes folder (workspace/notes). Or type a folder under your home folder, e.g. ~/Documents.';
  return `<div class="pad-save card card--pad" data-key="pad-save">
    <div class="pad-save__head"><strong>Save as a file</strong><button class="icon-btn" data-action="padSaveClose" aria-label="Close">✕</button></div>
    <label class="pad-lbl">Where
      <select class="input" data-input="pad.save.project_id"><option value="">No project (any folder)</option>
        ${c.d.projects.map(x => `<option value="${x.id}"${String(x.id) === String(s.project_id) ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}</select></label>
    <label class="pad-lbl">Folder (optional)
      <input class="input" data-input="pad.save.folder" value="${esc(s.folder)}" placeholder="${proj ? 'subfolder, e.g. docs' : '~/Documents/notes'}" autocapitalize="off" autocorrect="off"></label>
    <div class="meta">${esc(where)}</div>
    <label class="pad-lbl">File name (optional)
      <input class="input" data-input="pad.save.filename" value="${esc(s.filename)}" placeholder="${esc(n.title || 'note')}.md" autocapitalize="off" autocorrect="off"></label>
    ${n.saved_path ? `<div class="meta">Last saved: ${esc(n.saved_path)}. Saving again updates that file.</div>` : ''}
    ${s.error ? `<div class="pad-err">${esc(s.error)}</div>` : ''}
    <button class="btn btn--primary" data-action="padSaveGo"${s.busy ? ' disabled' : ''}>${s.busy ? 'Saving…' : 'Save file'}</button>
  </div>`;
}

// Which meeting a note is about: the note's project's meetings (or all, when unfiled), plus the one it's on now.
function meetingPick(c, n) {
  const meets = events(c.d).filter(e => e.kind === 'meeting' && (e.id === n.event_id || n.project_id == null || e.project_id === n.project_id))
    .sort((a, b) => (b.start_date || b.date || '').localeCompare(a.start_date || a.date || ''));
  return `<label class="pad-lbl pad-proj">Meeting
      <select class="input" data-input="pad.event"><option value="">No meeting</option>
        ${meets.map(e => `<option value="${e.id}"${e.id === n.event_id ? ' selected' : ''}>${esc(e.title)} · ${esc(meetingWhen(e))}</option>`).join('')}</select></label>`;
}

// A meeting's own notes (the calendar item's notes field), full-page, with the pad notes tied to it below.
function meetingView(c, p, e) {
  const tied = c.d.pad.items.filter(n => n.event_id === e.id);
  return `<div class="page page--narrow page--notes pad-edit" data-key="page-notes-meeting">
    <div class="pad-bar">
      <button class="btn btn--ghost btn--40" data-action="padBack">← Notes</button>
      <span class="pad-status meta">${esc(p.status)}</span>
      <button class="btn btn--40" data-action="openEvent" data-id="${e.id}">Meeting details</button>
    </div>
    <h2 class="pad__title">${esc(e.title)}</h2>
    <div class="meta">${esc(projectName(c, e.project_id))} · ${esc(meetingWhen(e))}</div>
    <textarea class="pad__body" data-input="pad.evnotes" maxlength="4000" placeholder="Agenda, who's coming, what was decided…" data-key="pad-evnotes-${e.id}">${esc(e.notes || '')}</textarea>
    <section class="section">
      <div class="section__head"><span class="section__title">More notes on this meeting <span class="section__count">${tied.length}</span></span>
        <button class="btn btn--40" data-action="padNewForMeeting" data-id="${e.id}">+ New note</button></div>
      ${tied.length ? `<div class="list pad-list">${tied.map(n => `<button class="pad-row" data-action="padOpen" data-id="${n.id}" data-key="pad-tied-${n.id}">
        <span class="pad-row__title">${esc(n.title || 'Untitled note')}</span><span class="pad-row__prev">${esc(preview(n.body)) || '<i>Empty</i>'}</span></button>`).join('')}</div>` : ''}
    </section>
  </div>`;
}

function editorView(c, p, n) {
  return `<div class="page page--narrow page--notes pad-edit" data-key="page-notes-edit">
    <div class="pad-bar">
      <button class="btn btn--ghost btn--40" data-action="padBack">← Notes</button>
      <span class="pad-status meta">${esc(p.status)}</span>
      <button class="btn btn--40" data-action="padSaveOpen">${icon('notes', 16)} Save as file</button>
      <button class="btn btn--danger btn--40" data-action="padDelete" aria-label="Delete note">Delete</button>
    </div>
    <input class="pad__title" data-input="pad.title" value="${esc(n.title)}" placeholder="Title" autocomplete="off" data-key="pad-title-${n.id}">
    <label class="pad-lbl pad-proj">Project
      <select class="input" data-input="pad.project"><option value="">Unfiled</option>
        ${c.d.projects.map(x => `<option value="${x.id}"${x.id === n.project_id ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}</select></label>
    ${meetingPick(c, n)}
    ${p.save ? savePanel(c, n, p.save) : ''}
    <textarea class="pad__body" data-input="pad.body" placeholder="Write anything…" data-key="pad-body-${n.id}">${esc(n.body)}</textarea>
  </div>`;
}

export function notes(c) {
  const p = padState(), items = c.d.pad.items;
  const n = p.sel && items.find(x => x.id === p.sel);
  if (n) return editorView(c, p, n);
  const ev = p.ev && events(c.d).find(x => x.id === p.ev);
  if (ev) return meetingView(c, p, ev);
  if (!c.d.pad.loaded) return `<div class="page page--narrow page--notes" data-key="page-notes"><div class="meta">Loading…</div></div>`;
  return listView(c, p, items);
}
