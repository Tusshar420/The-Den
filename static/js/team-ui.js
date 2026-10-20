// Glue between the Team views (views/team.js) and app.js: their actions and form inputs. The server checks every
// value (team.py) and says what's wrong; its message is shown next to the form.
// UI state: tmList, tmNew, tmEdit, tmTask, tmLeave, tmDel (see views/team.js).

import { store } from './api.js';
import { more, loadTeam } from './data.js';
import { LIST_DEFAULT, FIELDS } from './views/team.js';

const u = () => store.ui;
const message = e => (e && (e.detail || e.message)) || 'Could not save';
const NEW_EMPLOYEE = { name: '', job_title: '', department: '', email: '', phone: '', location: '', start_date: '', skills: '', notes: '', status: 'active', employment_type: 'full_time', manager_id: '', saving: false, error: '' };
const STATE = { new: 'tmNew', edit: 'tmEdit', task: 'tmTask', leave: 'tmLeave' };

// A form's values as a request body: empty text goes as null (clears it); manager_id as a number.
const body = f => Object.fromEntries(FIELDS.map(k => {
  const v = typeof f[k] === 'string' ? f[k].trim() : f[k];
  return [k, k === 'manager_id' ? (v ? Number(v) : null) : k === 'notes' ? (v || '') : (v || null)];
}));

export function teamActions(h) {
  const { set, toast, fail, navigate, confirmNear } = h;
  const eid = () => u().employeeId;
  const page = () => store.data.employeePages[eid()] || {};
  const patchForm = (group, patch) => { const key = STATE[group]; if (u()[key]) set({ [key]: { ...u()[key], ...patch, error: '' } }); };
  const err = (key, e) => set({ [key]: { ...u()[key], saving: false, error: message(e) } });

  return {
    openEmployee: el => navigate('employee', Number(el.dataset.id)),
    tmFilter: el => set({ tmList: { ...LIST_DEFAULT, ...u().tmList, status: el.dataset.v } }),
    tmSet: el => { const { f, k, v } = el.dataset; patchForm(f, { [k]: v }); },
    tmPick: el => patchForm(el.dataset.f, { [el.dataset.k]: el.value }),
    tmCancel: el => set({ [STATE[el.dataset.f]]: null }),

    // ------------------------------------------------ add an employee
    tmNewOpen: () => { if (u().view !== 'team') navigate('team'); set({ tmNew: { ...NEW_EMPLOYEE }, focus: 'tm-new-name' }); },
    tmNewSave: async () => {
      const n = u().tmNew;
      if (!n || n.saving) return;
      if (!n.name.trim()) return set({ tmNew: { ...n, error: 'Give the employee a name' }, focus: 'tm-new-name' });
      set({ tmNew: { ...n, saving: true, error: '' } });
      try {
        const e = await more.createEmployee(body(n));
        set({ tmNew: null });
        await loadTeam().catch(() => {});
        toast(`Added ${e.name}`);
        navigate('employee', e.id);
      } catch (e) { err('tmNew', e); }
    },

    // ------------------------------------------------ one employee's details
    tmEdit: () => {
      const e = page();
      set({ tmEdit: { id: e.id, ...Object.fromEntries(FIELDS.map(k => [k, e[k] == null ? '' : String(e[k])])), saving: false, error: '' }, focus: 'tm-edit-name' });
    },
    tmUpdate: async () => {
      const f = u().tmEdit;
      if (!f || f.saving) return;
      if (!f.name.trim()) return set({ tmEdit: { ...f, error: 'An employee needs a name' } });
      set({ tmEdit: { ...f, saving: true, error: '' } });
      try {
        await more.updateEmployee(f.id, body(f));
        set({ tmEdit: null });
        await loadTeam();
      } catch (e) { err('tmEdit', e); }
    },
    tmDelOpen: () => set({ tmDel: { id: eid(), busy: false } }),
    tmDelCancel: () => set({ tmDel: null }),
    tmDelete: async () => {
      const d = u().tmDel, name = page().name;
      if (!d || d.busy) return;
      set({ tmDel: { ...d, busy: true } });
      try {
        await more.deleteEmployee(d.id);
        store.data = { ...store.data, employees: (store.data.employees || []).filter(x => x.id !== d.id) };
        set({ tmDel: null });
        navigate('team');
        toast(`Deleted ${name}`);
      } catch (e) { set({ tmDel: null }); fail(e); }
    },

    // ------------------------------------------------ tasks
    tmTaskOpen: () => set({ tmTask: { title: '', due_date: '', priority: 'normal', project_id: '', notes: '', saving: false, error: '' }, focus: 'tm-task-title' }),
    tmTaskCancel: () => set({ tmTask: null }),
    tmTaskSave: async () => {
      const t = u().tmTask;
      if (!t || t.saving) return;
      if (!t.title.trim()) return set({ tmTask: { ...t, error: 'Say what the task is' }, focus: 'tm-task-title' });
      set({ tmTask: { ...t, saving: true, error: '' } });
      try {
        await more.addEmployeeTask(eid(), { title: t.title.trim(), due_date: t.due_date || null, priority: t.priority, project_id: t.project_id ? Number(t.project_id) : null, notes: t.notes.trim() });
        set({ tmTask: null });
        await loadTeam();
      } catch (e) { err('tmTask', e); }
    },
    tmTaskDone: async el => {
      const id = Number(el.dataset.id), t = (page().tasks || []).find(x => x.id === id);
      if (!t) return;
      try { await more.updateEmployeeTask(id, { status: t.status === 'done' ? 'open' : 'done' }); await loadTeam(); } catch (e) { fail(e); }
    },
    tmTaskStart: async el => { try { await more.updateEmployeeTask(Number(el.dataset.id), { status: el.dataset.v }); await loadTeam(); } catch (e) { fail(e); } },
    tmTaskDel: async el => {
      const id = Number(el.dataset.id), t = (page().tasks || []).find(x => x.id === id);
      if (!t || !(await confirmNear(el, `Delete “${t.title}”?`, 'Delete'))) return;
      try { await more.deleteEmployeeTask(id); await loadTeam(); } catch (e) { fail(e); }
    },

    // ------------------------------------------------ time off
    tmLeaveOpen: () => set({ tmLeave: { kind: 'vacation', start_date: '', end_date: '', note: '', saving: false, error: '' } }),
    tmLeaveCancel: () => set({ tmLeave: null }),
    tmLeaveSave: async () => {
      const l = u().tmLeave;
      if (!l || l.saving) return;
      if (!l.start_date) return set({ tmLeave: { ...l, error: 'Pick the first day' } });
      set({ tmLeave: { ...l, saving: true, error: '' } });
      try {
        await more.addEmployeeLeave(eid(), { kind: l.kind, start_date: l.start_date, end_date: l.end_date || l.start_date, note: l.note.trim() || null });
        set({ tmLeave: null });
        await loadTeam();
      } catch (e) { err('tmLeave', e); }
    },
    tmLeaveDel: async el => { try { await more.deleteEmployeeLeave(Number(el.dataset.id)); await loadTeam(); } catch (e) { fail(e); } },
  };
}

// data-input "tm.q" (Team search) and "tm.<new|edit|task|leave>.<field>" (the forms).
export function teamInput(path, v) {
  const ui = u();
  if (path === 'tm.q') { ui.tmList = { ...LIST_DEFAULT, ...ui.tmList, q: v }; return; }
  const [, group, k] = path.split('.'), key = STATE[group];
  if (key && ui[key]) ui[key] = { ...ui[key], [k]: v, error: '' };
}
