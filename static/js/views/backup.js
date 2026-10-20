// Settings > Backup: download tasks / history / settings as JSON or CSV, download a copy of the whole
// database, and restore one - with a plain warning (and the file's contents) before anything is replaced.
// State lives in ui.backup: { file, name, info, busy, error, done }.

import { store, apiKey } from '../api.js';
import { esc } from '../ui.js';
import { plural } from '../format.js';

const EXPORTS = [
  ['tasks', 'Tasks', 'Every task and chat message: prompt, status, result, cost, dates.'],
  ['history', 'History', 'What each task did step by step, plus the usage log.'],
  ['settings', 'Settings', 'Your preferences, projects, recipes and schedules. Passwords and tokens are never included.'],
];

export function backupSection(c) {
  const b = c.ui.backup || {};
  const exports = EXPORTS.map(([k, name, help]) => `<div class="set-line" data-key="exp-${k}">
    <span class="set-line__txt"><b>${name}</b><span class="meta">${esc(help)}</span></span>
    <span class="set-line__btns"><button class="btn btn--sm" data-action="exportData" data-kind="${k}" data-fmt="json">JSON</button>
      <button class="btn btn--sm" data-action="exportData" data-kind="${k}" data-fmt="csv">CSV</button></span></div>`).join('');
  const pending = b.info ? `<div class="bk-warn" role="alert" data-key="bk-warn">
      <b>Restore replaces everything</b>
      <span>Restoring <i>${esc(b.name)}</i> deletes all the tasks, chats, projects, recipes and settings you have now and puts back the ${plural(b.info.tasks, 'task')} and ${plural(b.info.projects, 'project')} in this file. Anything made since that backup is lost.
      Your current data is saved first as <code>queue.db.bak-pre-restore</code> on this Mac, so a mistake can be undone by restoring that file.</span>
      <span class="set-line__btns"><button class="btn btn--sm btn--ghost" data-action="restoreCancel"${b.busy ? ' disabled' : ''}>Cancel</button>
      <button class="btn btn--sm btn--danger" data-action="restoreGo"${b.busy ? ' disabled' : ''}>${b.busy ? 'Restoring…' : 'Replace everything with this backup'}</button></span></div>` : '';
  return `<span class="meta prov-note">Download your data to keep it or move it somewhere else.</span>
    <div class="list">${exports}</div>
    <div class="set-group"><span class="set-group__t">Database backup and restore</span>
      <div class="set-line"><span class="set-line__txt"><b>Back up now</b><span class="meta">One file with everything. Keep it private - it holds all your tasks and settings.</span></span>
        <button class="btn btn--sm btn--primary" data-action="backupDownload">Download backup</button></div>
      <div class="set-line"><span class="set-line__txt"><b>Restore from a backup</b><span class="meta">Pick a .db file you downloaded here. Nothing changes until you confirm.</span></span>
        <label class="btn btn--sm">Choose file…<input type="file" accept=".db,.sqlite,application/octet-stream" hidden data-restore-file></label></div>
      ${pending}
      ${b.error ? `<span class="meta bk-err" role="alert">${esc(b.error)}</span>` : ''}
      ${b.done ? `<span class="meta bk-ok">${esc(b.done)}</span>` : ''}</div>`;
}

async function call(path, init = {}) {
  const res = await fetch(path, { ...init, headers: { 'X-API-Key': apiKey(), ...(init.headers || {}) } });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res;
}

function save(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

const nameOf = res => ((res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/) || [])[1] || 'download';

let picked = null;      // the File chosen for restore - kept out of the store so a poll can't serialise it

let H = null;      // app.js helpers, for the file picker's change event

export function backupActions(h) {
  H = h;
  const { set, toast, fail } = h;
  const put = patch => set({ backup: { ...(store.ui.backup || {}), ...patch } });

  async function download(path, label) {
    try {
      const res = await call(path);
      save(await res.blob(), nameOf(res));
      toast(`${label} downloaded`);
    } catch (e) { fail(e); }
  }

  return {
    exportData: el => download(`/export/${el.dataset.kind}?format=${el.dataset.fmt}`, 'Export'),
    backupDownload: () => download('/backup', 'Backup'),
    restoreCancel: () => { picked = null; set({ backup: {} }); },
    restoreGo: async () => {
      const b = store.ui.backup || {};
      if (!picked || b.busy) return;
      put({ busy: true, error: '' });
      try {
        const r = await (await call('/restore?confirm=replace-everything', { method: 'POST', body: picked })).json();
        picked = null;
        set({ backup: { done: `Restored ${plural(r.tasks, 'task')} and ${plural(r.projects, 'project')}. Reloading…` } });
        setTimeout(() => location.reload(), 1200);
      } catch (e) { put({ busy: false, error: e.message }); }
    },
  };
}

// The file picker's change event: look inside the file (read-only) so the warning can say what it holds.
export async function backupPicked(file) {
  if (!file || !H) return;
  picked = null;
  H.set({ backup: { busy: false, error: '', done: '' } });
  try {
    const info = await (await call('/restore/check', { method: 'POST', body: file })).json();
    picked = file;
    H.set({ backup: { file: true, name: file.name, info } });
  } catch (e) { H.set({ backup: { error: e.message } }); }
}
