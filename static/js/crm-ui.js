// Glue between the project Overview tab (views/overview.js) and app.js: its actions (data-action="ov…")
// and form inputs. Every edit is a PATCH /projects/{id}; the server checks it (crm.py) and sends the
// whole record back, which replaces ours.
// UI state: ovEdit { pid, k, v, v2, saving, error } (the field open for editing), ovTag (the tag box),
// ovLink { label, url, error } (the add-link form), ovDel { pid, text, busy, error } (typed delete confirmation).
// Also the New project sheet (views/new-project.js): ui.onboard holds the form, newProjectBody turns it
// into the POST /projects body, and the np… actions are its chips and link list.

import { store, endpoints, uploadProjectImage } from './api.js';
import { more } from './data.js';
import { cropImage } from './crop.js';

// The New project form, empty. category is set from the Work / Personal switch when it opens.
export const NEW_PROJECT = { name: '', path: '', category: 'work', status: 'not_started', priority: 'medium', health: 'on_track',
  start_date: '', due_date: '', contract_value: '', currency: 'USD', billing_type: '', owner: '', description: '', tags: '',
  links: [], linkLabel: 'Repo', linkUrl: '', error: '' };

// The POST /projects body for the form, or a string saying what to fix. Empty fields are left out, so the
// server's defaults apply; the server checks every value again (crm.clean) and says what's wrong.
export function newProjectBody(ob) {
  const f = { ...NEW_PROJECT, ...ob };
  const name = (f.name || '').trim();
  if (!name) return 'Give the project a name.';
  const body = { name, category: f.category || 'work', status: f.status || 'not_started', priority: f.priority, health: f.health };
  const path = (f.path || '').trim();
  if (path) body.path = path;
  for (const k of ['start_date', 'due_date', 'owner', 'description', 'billing_type']) {
    const v = (f[k] || '').trim();
    if (v) body[k] = v;
  }
  const raw = String(f.contract_value ?? '').trim();
  if (raw) {
    const n = Number(raw);
    if (!Number.isFinite(n)) return 'Contract value must be a number.';
    body.contract_value = n;
  }
  if (raw || (f.currency || '').trim()) body.currency = (f.currency || 'USD').trim().toUpperCase();
  const tags = (f.tags || '').split(',').map(t => t.trim()).filter(Boolean);
  if (tags.length) body.tags = tags;
  const links = [...(f.links || [])];
  if ((f.linkUrl || '').trim()) links.push({ label: (f.linkLabel || '').trim() || null, url: f.linkUrl.trim() });   // typed but not added yet
  if (links.length) body.links = links;
  return body;
}

// The picture cropped to a centred 256px square (PNG), so what's stored and fetched stays small.
// A GIF or anything the browser can't draw is sent as it is. Client logos (clients-ui.js) use it too.
export async function squareImage(file) {
  let bmp;
  try { bmp = await createImageBitmap(file); } catch { return file; }
  const n = Math.min(bmp.width, bmp.height), size = Math.min(256, n);
  const cv = Object.assign(document.createElement('canvas'), { width: size, height: size });
  cv.getContext('2d').drawImage(bmp, (bmp.width - n) / 2, (bmp.height - n) / 2, n, n, 0, 0, size, size);
  const blob = await new Promise(res => cv.toBlob(res, 'image/png'));
  return blob || file;
}

const u = () => store.ui;
const pid = () => u().projectId;
const rec = () => ({ ...(store.data.projects.find(p => p.id === pid()) || {}), ...(store.data.records[pid()] || {}) });
const NEW_LINK = { label: 'Repo', url: '', error: '' };
const message = e => (e && (e.detail || e.message)) || 'Could not save';

// Keep what the server sent back: the full record, and the list copy (badges, sidebar) up to date too.
export function keepRecord(id, r) {
  const { progress, ...fields } = r;
  store.data = {
    ...store.data,
    records: { ...store.data.records, [id]: r },
    projects: store.data.projects.map(p => (p.id === id ? { ...p, ...fields } : p)),
  };
}

export function overviewActions(h) {
  const { set, toast, fail, navigate, again, submitNewProject } = h;

  async function save(body, { onError } = {}) {
    const id = pid();
    try {
      keepRecord(id, await more.updateProject(id, body));
      set({});
      return true;
    } catch (e) {
      if (onError) onError(e); else fail(e);
      return false;
    }
  }

  // The value an editor opens with, for each field that has one.
  const start = k => {
    const r = rec();
    if (k === 'contract') return { v: r.contract_value == null ? '' : String(r.contract_value), v2: r.currency || 'USD' };
    if (k === 'folder') return { v: '', v2: '' };
    return { v: r[k] == null ? '' : String(r[k]), v2: '' };
  };

  async function saveEdit(clear = false) {
    const e = u().ovEdit;
    if (!e || e.saving) return;
    let body;
    if (e.k === 'contract') {
      const raw = clear ? '' : e.v.trim();
      const n = raw === '' ? null : Number(raw);
      if (n !== null && !Number.isFinite(n)) return set({ ovEdit: { ...e, error: 'Enter a number' } });
      body = { contract_value: n };
      if (!clear) body.currency = (e.v2 || 'USD').trim().toUpperCase();
    } else if (e.k === 'name') {
      if (!e.v.trim()) return set({ ovEdit: { ...e, error: 'Give the project a name' } });
      body = { name: e.v.trim() };
    } else if (e.k === 'folder') {
      // "Link a folder": the server checks it exists, then the project can run tasks.
      if (!e.v.trim()) return set({ ovEdit: { ...e, error: 'Type the folder path, e.g. ~/code/shop' } });
      set({ ovEdit: { ...e, saving: true, error: '' } });
      try {
        keepRecord(e.pid, await endpoints.linkFolder(e.pid, e.v.trim()));
        set({ ovEdit: null });
        toast('Folder saved');
      } catch (err) { set({ ovEdit: { ...u().ovEdit, saving: false, error: message(err) } }); }
      return;
    } else {
      const v = clear ? '' : e.v.trim();
      body = { [e.k]: v === '' ? null : v };
    }
    set({ ovEdit: { ...e, saving: true, error: '' } });
    const ok = await save(body, { onError: err => set({ ovEdit: { ...u().ovEdit, saving: false, error: message(err) } }) });
    if (ok) set({ ovEdit: null });
  }

  return {
    ovEdit: el => { const k = el.dataset.k; set({ ovEdit: { pid: pid(), k, ...start(k), saving: false, error: '' }, focus: `ov-${k}` }); },
    ovCancel: () => set({ ovEdit: null }),
    // Draft the description from the project itself; it lands in the editor, unsaved, for review.
    ovGenDesc: async () => {
      const id = pid();
      const cur = u().ovEdit;
      const base = cur && cur.pid === id && cur.k === 'description' ? cur : { pid: id, k: 'description', ...start('description'), saving: false, error: '' };
      if (base.generating) return;
      set({ ovEdit: { ...base, generating: true, error: '' }, focus: 'ov-description' });
      try {
        const text = await endpoints.describeProject(id);
        const e = u().ovEdit;
        if (e && e.pid === id && e.k === 'description') set({ ovEdit: { ...e, v: text, generating: false, error: '' } });
      } catch (err) {
        const e = u().ovEdit;
        if (e && e.pid === id && e.k === 'description') set({ ovEdit: { ...e, generating: false, error: message(err) } });
      }
    },
    ovSave: () => saveEdit(false),
    ovClear: () => saveEdit(true),
    ovColor: el => { const v = el.dataset.v || null; if ((rec().color || null) !== v) save({ color: v }); },
    ovLogoBg: el => { const v = el.dataset.v || null; if ((rec().logo_bg || null) !== v) save({ logo_bg: v }); },
    ovImagePick: () => { const f = document.querySelector('input[data-ov-image]'); if (f) f.click(); },
    ovImageDel: async () => {
      const id = pid();
      try { keepRecord(id, await endpoints.deleteProjectImage(id)); set({}); toast('Picture removed'); } catch (e) { fail(e); }
    },
    ovImagePicked: async file => {
      const id = pid();
      if (!file) return;
      try {
        const pic = await cropImage(file, 'Adjust project picture');
        if (!pic) return;
        keepRecord(id, await uploadProjectImage(id, pic));
        set({});
        toast('Picture saved');
      } catch (e) { fail(e); }
    },
    ovSet: el => { const { k, v } = el.dataset; if (rec()[k] !== (v || null)) save({ [k]: v || null }); },
    ovTagAdd: () => {
      const add = (u().ovTag || '').split(',').map(s => s.trim()).filter(Boolean);
      if (!add.length) return;
      set({ ovTag: '' });
      save({ tags: [...(rec().tags || []), ...add] }, { onError: e => { set({ ovTag: add.join(', ') }); fail(e); } });
    },
    ovTagDel: el => { const i = Number(el.dataset.i); save({ tags: (rec().tags || []).filter((_, j) => j !== i) }); },
    ovLinkKind: el => set({ ovLink: { ...(u().ovLink || NEW_LINK), label: el.dataset.v, error: '' }, focus: 'ov-link-url' }),
    ovLinkAdd: async () => {
      const nl = u().ovLink || NEW_LINK;
      if (!nl.url.trim()) return;
      const links = [...(rec().links || []), { label: nl.label.trim() || null, url: nl.url.trim() }];
      const ok = await save({ links }, { onError: e => set({ ovLink: { ...nl, error: message(e) } }) });
      if (ok) { set({ ovLink: { ...NEW_LINK } }); toast('Link added'); }
    },
    ovLinkDel: el => { const i = Number(el.dataset.i); save({ links: (rec().links || []).filter((_, j) => j !== i) }); },
    // From the project header or a "Link a folder" hint: open the Overview with the folder editor ready.
    ovLinkStart: () => {
      if (u().projectTab !== 'overview') navigate('project', pid(), 'overview');
      set({ ovEdit: { pid: pid(), k: 'folder', v: '', v2: '', saving: false, error: '' }, focus: 'ov-folder' });
    },
    ovFolderChange: () => set({ ovEdit: { pid: pid(), k: 'folder', v: rec().path || '', v2: '', saving: false, error: '' }, focus: 'ov-folder' }),
    ovArchive: async () => {
      const r = rec(), was = r.status, archived = was === 'archived';
      const ok = await save({ status: archived ? 'not_started' : 'archived' });
      if (ok) toast(archived ? `${r.name} is back in the list` : `Archived ${r.name}`, archived ? {} : { action: () => save({ status: was }), actionLabel: 'Undo' });
    },
    ovDelOpen: () => set({ ovDel: { pid: pid(), text: '', busy: false, error: '' }, focus: 'ov-del' }),
    ovDelCancel: () => set({ ovDel: null }),
    ovDelete: async () => {
      const d = u().ovDel, r = rec();
      if (!d || d.busy || d.text !== r.name) return;
      set({ ovDel: { ...d, busy: true, error: '' } });
      try {
        await endpoints.deleteProject(d.pid, d.text);
        set({ ovDel: null, ovEdit: null });
        store.data = { ...store.data, projects: store.data.projects.filter(p => p.id !== d.pid) };
        navigate('projects');
        toast(`Deleted ${r.name}`);
        again();
      } catch (e) { set({ ovDel: { ...u().ovDel, busy: false, error: message(e) } }); }
    },
    // The New project sheet. The button runs it directly: a click inside a sheet doesn't reach the form's submit.
    npSubmit: () => submitNewProject(),
    npSet: el => { const { k, v } = el.dataset; set({ onboard: { ...u().onboard, [k]: v, error: '' } }); },
    npLinkKind: el => set({ onboard: { ...u().onboard, linkLabel: el.dataset.v }, focus: 'np-link-url' }),
    npLinkAdd: () => {
      const ob = u().onboard;
      if (!(ob.linkUrl || '').trim()) return;
      set({ onboard: { ...ob, links: [...(ob.links || []), { label: (ob.linkLabel || '').trim() || null, url: ob.linkUrl.trim() }], linkUrl: '', error: '' }, focus: 'np-link-url' });
    },
    npLinkDel: el => { const ob = u().onboard, i = Number(el.dataset.i); set({ onboard: { ...ob, links: (ob.links || []).filter((_, j) => j !== i) } }); },
  };
}

// data-input="ov.v" / "ov.v2" (the open editor), "ov.tag", "ovLink.label" / "ovLink.url", "ov.del",
// and "onboard.<field>" (the New project sheet).
export function overviewInput(path, v) {
  const ui = u();
  if (path.startsWith('onboard.')) ui.onboard = { ...NEW_PROJECT, ...ui.onboard, [path.slice(8)]: v, error: '' };
  else if (path === 'ov.del') { if (ui.ovDel) ui.ovDel = { ...ui.ovDel, text: v, error: '' }; }
  else if (path === 'ov.tag') ui.ovTag = v;
  else if (path.startsWith('ovLink.')) ui.ovLink = { ...(ui.ovLink || NEW_LINK), [path.slice(7)]: v, error: '' };
  else if (ui.ovEdit && (path === 'ov.v' || path === 'ov.v2')) ui.ovEdit = { ...ui.ovEdit, [path.slice(3)]: v, error: '' };
}
