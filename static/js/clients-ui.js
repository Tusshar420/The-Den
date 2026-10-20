// Glue between the CRM client views (views/clients.js, views/contacts.js, the Client field on a project's
// Overview tab) and app.js: their actions and form inputs. The server checks every value (clients.py) and
// says what's wrong; its message is shown next to the form.
// UI state: clList, clNew, clEdit, clDel (see views/clients.js), cnNew, cnEdit (views/contacts.js), and
// ovClient { pid, v, name, saving, error } - the project Overview's client picker (v: a client id, '' for none,
// 'new' to create one called name).

import { store, apiKey } from './api.js';
import { more, loadClient, loadClients, clientById } from './data.js';
import { keepRecord } from './crm-ui.js';
import { cropImage } from './crop.js';
import { LIST_DEFAULT } from './views/clients.js';

const u = () => store.ui;
const message = e => (e && (e.detail || e.message)) || 'Could not save';
const NEW_CONTACT = { name: '', role: '', email: '', phone: '', notes: '', preferred_method: '', project_role: 'stakeholder', saving: false, error: '' };
const CONTACT_KEYS = ['name', 'role', 'email', 'phone', 'notes', 'preferred_method'];

async function uploadLogo(cid, blob) {
  const res = await fetch(`/clients/${cid}/logo`, { method: 'POST', headers: { 'X-API-Key': apiKey(), 'Content-Type': blob.type || 'application/octet-stream' }, body: blob });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// The client page as the server sent it back, kept (and the list copy brought up to date).
function keepClient(page) {
  const { projects, contacts, ...row } = page;
  store.data = { ...store.data, clientPages: { ...store.data.clientPages, [page.id]: page },
    clients: (store.data.clients || []).some(x => x.id === page.id) ? store.data.clients.map(x => (x.id === page.id ? { ...x, ...row } : x)) : [...(store.data.clients || []), row] };
}

// After a change that touches more than one place: the lists, the open client page, the open project's record.
async function reload({ pid = null } = {}) {
  const jobs = [loadClients()];
  if (u().view === 'client' && u().clientId) jobs.push(loadClient(u().clientId));
  if (pid != null) jobs.push(more.projectRecord(pid).then(r => keepRecord(pid, r)));
  await Promise.all(jobs.map(j => j.catch(() => {})));
  store.set({});
}

export function clientActions(h) {
  const { set, toast, fail, navigate, confirmNear } = h;
  const cid = () => u().clientId;
  const page = () => store.data.clientPages[cid()] || {};
  const pid = () => u().projectId;

  async function saveClient(body, { onError } = {}) {
    try {
      keepClient(await more.updateClient(cid(), body));
      set({});
      return true;
    } catch (e) {
      if (onError) onError(e); else fail(e);
      return false;
    }
  }

  async function saveEdit(clear) {
    const e = u().clEdit;
    if (!e || e.saving) return;
    const v = clear ? '' : e.v.trim();
    if (e.k === 'name' && !v) return set({ clEdit: { ...e, error: 'Give the client a name' } });
    set({ clEdit: { ...e, saving: true, error: '' } });
    const ok = await saveClient({ [e.k]: v === '' ? null : v }, { onError: err => set({ clEdit: { ...u().clEdit, saving: false, error: message(err) } }) });
    if (ok) { set({ clEdit: null }); if (e.k === 'name') reload(); }
  }

  // The contact being added or edited, as a request body. Empty fields are sent as null (clears them).
  const contactBody = f => Object.fromEntries(CONTACT_KEYS.map(k => [k, (f[k] || '').trim() || null]));
  const findContact = id => (store.data.contacts || []).find(x => x.id === id)
    || (page().contacts || []).find(x => x.id === id)
    || ((store.data.records[pid()] || {}).contacts || []).find(x => x.id === id);

  return {
    openClient: el => navigate('client', Number(el.dataset.id)),
    clOpenProject: el => navigate('project', Number(el.dataset.id), 'overview'),
    clFilter: el => set({ clList: { ...LIST_DEFAULT, ...u().clList, status: el.dataset.v } }),

    // ------------------------------------------------ new client (Clients page)
    clNewOpen: () => {
      if (u().view !== 'clients') navigate('clients');
      set({ clNew: { name: '', status: 'prospect', saving: false, error: '' }, focus: 'cl-new-name' });
    },
    clNewStatus: el => set({ clNew: { ...u().clNew, status: el.dataset.v, error: '' } }),
    clNewCancel: () => set({ clNew: null }),
    clNewSave: async () => {
      const n = u().clNew;
      if (!n || n.saving || !n.name.trim()) return;
      set({ clNew: { ...n, saving: true, error: '' } });
      try {
        const cl = await more.createClient({ name: n.name.trim(), status: n.status });
        keepClient(cl);
        set({ clNew: null });
        navigate('client', cl.id);
        toast(`Added ${cl.name}`);
        reload();
      } catch (e) { set({ clNew: { ...u().clNew, saving: false, error: message(e) } }); }
    },

    // ------------------------------------------------ the client's details
    clEdit: el => { const k = el.dataset.k, v = page()[k]; set({ clEdit: { cid: cid(), k, v: v == null ? '' : String(v), saving: false, error: '' }, focus: `cl-${k}` }); },
    clCancel: () => set({ clEdit: null }),
    clSave: () => saveEdit(false),
    clClear: () => saveEdit(true),
    clSet: el => { const { k, v } = el.dataset; if (page()[k] !== v) saveClient({ [k]: v }).then(ok => ok && reload()); },
    clColor: el => { const v = el.dataset.v !== undefined ? (el.dataset.v || null) : el.value; if ((page().color || null) !== v) saveClient({ color: v }); },
    clLogoPick: () => { const f = document.querySelector('input[data-cl-logo]'); if (f) f.click(); },
    clLogoPicked: async el => {
      const file = el.files && el.files[0];
      el.value = '';
      if (!file) return;
      try { const pic = await cropImage(file, 'Adjust logo'); if (!pic) return; keepClient(await uploadLogo(cid(), pic)); set({}); toast('Logo saved'); } catch (e) { fail(e); }
    },
    clLogoDel: async () => { try { keepClient(await more.deleteClientLogo(cid())); set({}); toast('Logo removed'); } catch (e) { fail(e); } },
    clDelOpen: () => set({ clDel: { cid: cid(), busy: false } }),
    clDelCancel: () => set({ clDel: null }),
    clDelete: async () => {
      const d = u().clDel, name = page().name;
      if (!d || d.busy) return;
      set({ clDel: { ...d, busy: true } });
      try {
        await more.deleteClient(d.cid);
        store.data = { ...store.data, clients: (store.data.clients || []).filter(x => x.id !== d.cid) };
        set({ clDel: null });
        navigate('clients');
        toast(`Deleted ${name}`);
        reload();
      } catch (e) { set({ clDel: null }); fail(e); }
    },
    // "Add a project to <client>" on the client page.
    clLinkProject: async el => {
      const id = Number(el.value);
      el.value = '';
      if (!id) return;
      try {
        await more.updateProject(id, { client_id: cid() });
        toast(`${(store.data.projects.find(p => p.id === id) || {}).name || 'Project'} is now ${page().name}'s`);
        await reload({ pid: id });
      } catch (e) { fail(e); }
    },

    // ------------------------------------------------ contacts (client page and project Overview)
    cnNew: el => set({ cnNew: { ...NEW_CONTACT, at: el.dataset.at, id: Number(el.dataset.id) }, cnEdit: null, focus: 'cn-new-name' }),
    cnSet: el => { const { f, k, v } = el.dataset, key = f === 'new' ? 'cnNew' : 'cnEdit'; if (u()[key]) set({ [key]: { ...u()[key], [k]: v, error: '' } }); },
    cnCancel: el => set({ [el.dataset.f === 'new' ? 'cnNew' : 'cnEdit']: null }),
    cnSave: async () => {
      const n = u().cnNew;
      if (!n || n.saving) return;
      if (!n.name.trim()) return set({ cnNew: { ...n, error: 'Give the contact a name' }, focus: 'cn-new-name' });
      const body = contactBody(n);
      if (n.at === 'client') body.client_id = n.id;
      else { body.project_id = n.id; body.project_role = n.project_role || 'stakeholder'; }
      set({ cnNew: { ...n, saving: true, error: '' } });
      try {
        const ct = await more.createContact(body);
        set({ cnNew: null });
        toast(`Added ${ct.name}`);
        await reload({ pid: n.at === 'project' ? n.id : null });
      } catch (e) { set({ cnNew: { ...u().cnNew, saving: false, error: message(e) } }); }
    },
    cnEdit: el => {
      const ct = findContact(Number(el.dataset.id));
      if (!ct) return;
      set({ cnEdit: { id: ct.id, ...Object.fromEntries(CONTACT_KEYS.map(k => [k, ct[k] || ''])), saving: false, error: '' }, cnNew: null, focus: 'cn-edit-name' });
    },
    cnUpdate: async () => {
      const e = u().cnEdit;
      if (!e || e.saving) return;
      if (!e.name.trim()) return set({ cnEdit: { ...e, error: 'A contact needs a name' } });
      set({ cnEdit: { ...e, saving: true, error: '' } });
      try {
        await more.updateContact(e.id, contactBody(e));
        set({ cnEdit: null });
        await reload({ pid: u().view === 'project' ? pid() : null });
      } catch (err) { set({ cnEdit: { ...u().cnEdit, saving: false, error: message(err) } }); }
    },
    cnDelete: async el => {
      const ct = findContact(Number(el.dataset.id));
      if (!ct) return;
      const n = (ct.project_ids || []).length;
      if (!(await confirmNear(el, `Delete ${ct.name}?${n ? ` They're taken off ${n === 1 ? 'the project' : `all ${n} projects`} they're on.` : ''}`, 'Delete'))) return;
      try { await more.deleteContact(ct.id); toast(`Deleted ${ct.name}`); await reload(); } catch (e) { fail(e); }
    },
    cnKey: async el => {
      const id = pid();
      try { keepRecord(id, await more.linkContact(id, Number(el.dataset.id), el.dataset.v)); set({}); } catch (e) { fail(e); }
    },
    cnUnlink: async el => {
      const id = pid(), ct = findContact(Number(el.dataset.id));
      if (!ct) return;
      const role = ct.project_role || 'stakeholder';
      try {
        keepRecord(id, await more.unlinkContact(id, ct.id));
        set({});
        loadClients().catch(() => {});
        toast(`Took ${ct.name} off this project`, { actionLabel: 'Undo', action: async () => {
          try { keepRecord(id, await more.linkContact(id, ct.id, role)); set({}); loadClients().catch(() => {}); } catch (e) { fail(e); }
        } });
      } catch (e) { fail(e); }
    },
    cnLinkPick: async el => {
      const id = pid(), contact = Number(el.value);
      el.value = '';
      if (!contact) return;
      try { keepRecord(id, await more.linkContact(id, contact, 'stakeholder')); set({}); loadClients().catch(() => {}); } catch (e) { fail(e); }
    },

    // ------------------------------------------------ the client of a project (Overview tab)
    ovClientEdit: () => {
      const r = { ...(store.data.projects.find(p => p.id === pid()) || {}), ...(store.data.records[pid()] || {}) };
      set({ ovClient: { pid: pid(), v: r.client_id ? String(r.client_id) : '', name: '', saving: false, error: '' }, focus: 'ov-client' });
    },
    ovClientCancel: () => set({ ovClient: null }),
    ovClientSave: async () => {
      const o = u().ovClient;
      if (!o || o.saving) return;
      if (o.v === 'new' && !o.name.trim()) return set({ ovClient: { ...o, error: 'Type the new client\'s name' }, focus: 'ov-client-name' });
      set({ ovClient: { ...o, saving: true, error: '' } });
      try {
        let id = o.v ? Number(o.v) : null;
        if (o.v === 'new') {
          const cl = await more.createClient({ name: o.name.trim(), status: 'active' });
          keepClient(cl);
          id = cl.id;
        }
        keepRecord(o.pid, await more.updateProject(o.pid, { client_id: id }));
        set({ ovClient: null });
        if (o.v === 'new') toast(`Created ${clientById(id) ? clientById(id).name : 'the client'} · open it to add details and contacts`);
        reload();
      } catch (e) { set({ ovClient: { ...u().ovClient, saving: false, error: message(e) } }); }
    },
  };
}

// data-input "cl.q" (Clients page search), "cl.new.name", "cl.v" (the detail being edited), "cl.ovClient" /
// "cl.ovClientName" (the Overview's client picker), "cn.new.<field>" / "cn.edit.<field>" (contact forms).
export function clientInput(path, v) {
  const ui = u();
  if (path === 'cl.q') ui.clList = { ...LIST_DEFAULT, ...ui.clList, q: v };
  else if (path === 'cl.new.name') { if (ui.clNew) ui.clNew = { ...ui.clNew, name: v, error: '' }; }
  else if (path === 'cl.v') { if (ui.clEdit) ui.clEdit = { ...ui.clEdit, v, error: '' }; }
  else if (path === 'cl.ovClient') { if (ui.ovClient) ui.ovClient = { ...ui.ovClient, v, error: '' }; if (v === 'new') ui.focus = 'ov-client-name'; }
  else if (path === 'cl.ovClientName') { if (ui.ovClient) ui.ovClient = { ...ui.ovClient, name: v, error: '' }; }
  else if (path.startsWith('cn.')) {
    const [, group, k] = path.split('.'), key = group === 'new' ? 'cnNew' : 'cnEdit';
    if (ui[key]) ui[key] = { ...ui[key], [k]: v, error: '' };
  }
}
