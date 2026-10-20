// Glue between the CRM Activity card / follow-up rows (views/crm-activity.js) and app.js: their actions and
// form inputs. The server checks every value (activities.py) and says what's wrong; its message is shown
// next to the form. UI state: actNew, fuNew, actMore (see views/crm-activity.js).

import { store } from './api.js';
import { more, loadFeed, loadFollowups, feedKeyFor } from './data.js';
import { localStamp, isoDay } from './views/crm-activity.js';

const u = () => store.ui;
const message = e => (e && (e.detail || e.message)) || 'Could not save';
const SUBJECT_KEY = { project: 'project_id', client: 'client_id', contact: 'contact_id' };

// The open page's timeline and the follow-up list, after a change.
async function reload(at, id) {
  const jobs = [loadFollowups()];
  const key = at ? `${at}:${id}` : feedKeyFor(u());
  if (key) { const [a, i] = key.split(':'); jobs.push(loadFeed(a, Number(i))); }
  await Promise.all(jobs.map(j => j.catch(() => {})));
}

// A follow-up wherever the page has it: the open timeline or the full list.
function findFollowup(id) {
  const key = feedKeyFor(u()), feed = key && store.data.feeds[key];
  return ((feed && feed.open) || []).find(x => x.id === id) || ((store.data.followups && store.data.followups.items) || []).find(x => x.id === id);
}
function findActivity(id) {
  const key = feedKeyFor(u()), feed = key && store.data.feeds[key];
  return ((feed && feed.items) || []).find(x => x.type === 'activity' && x.id === id);
}

export function crmActivityActions(h) {
  const { set, toast, fail, navigate, confirmNear } = h;

  async function setDone(id, status) {
    try {
      await more.updateFollowup(id, { status });
      await reload();
      if (status === 'done') {
        toast('Follow-up done', { actionLabel: 'Undo', action: async () => {
          try { await more.updateFollowup(id, { status: 'open' }); await reload(); } catch (e) { fail(e); }
        } });
      }
    } catch (e) { fail(e); }
  }

  return {
    openContact: el => navigate('contact', Number(el.dataset.id)),

    // ------------------------------------------------ logging an activity
    actOpen: el => set({ actNew: { at: el.dataset.at, id: Number(el.dataset.id), editId: null, kind: 'call', when: localStamp(), summary: '', outcome: '',
      people: [], saving: false, error: '' }, fuNew: null, focus: 'act-summary' }),
    actSet: el => { if (u().actNew) set({ actNew: { ...u().actNew, kind: el.dataset.v, error: '' } }); },
    actPerson: el => {
      const n = u().actNew, id = Number(el.dataset.id);
      if (n) set({ actNew: { ...n, people: n.people.includes(id) ? n.people.filter(x => x !== id) : [...n.people, id] } });
    },
    actPersonPick: el => {
      const n = u().actNew, id = Number(el.value);
      el.value = '';
      if (n && id && !n.people.includes(id)) set({ actNew: { ...n, people: [...n.people, id] } });
    },
    actCancel: () => set({ actNew: null }),
    actEdit: el => {
      const a = findActivity(Number(el.dataset.id)), key = feedKeyFor(u());
      if (!a || !key) return;
      const [at, id] = key.split(':');
      set({ actNew: { at, id: Number(id), editId: a.id, kind: a.kind, when: localStamp(new Date(a.occurred_at)), summary: a.summary || '', outcome: a.outcome || '',
        people: a.contact_ids || [], saving: false, error: '' }, fuNew: null, focus: 'act-summary' });
    },
    actSave: async () => {
      const n = u().actNew;
      if (!n || n.saving) return;
      if (!n.summary.trim()) return set({ actNew: { ...n, error: 'Write a short summary' }, focus: 'act-summary' });
      const when = new Date(n.when);
      if (!n.when || Number.isNaN(when.getTime())) return set({ actNew: { ...n, error: 'Say when it happened' }, focus: 'act-when' });
      const body = { kind: n.kind, occurred_at: when.toISOString(), summary: n.summary.trim(), outcome: n.outcome.trim() || null, contact_ids: n.people };
      set({ actNew: { ...n, saving: true, error: '' } });
      try {
        if (n.editId) await more.updateActivity(n.editId, body);
        else await more.createActivity({ ...body, [SUBJECT_KEY[n.at]]: n.id });
        set({ actNew: null });
        if (!n.editId) toast('Logged');
        await reload(n.at, n.id);
      } catch (e) { set({ actNew: { ...u().actNew, saving: false, error: message(e) } }); }
    },
    actDelete: async el => {
      const id = Number(el.dataset.id);
      if (!(await confirmNear(el, 'Delete this from the timeline?', 'Delete'))) return;
      try { await more.deleteActivity(id); await reload(); } catch (e) { fail(e); }
    },
    actMore: el => { const k = el.dataset.k; set({ actMore: { ...(u().actMore || {}), [k]: ((u().actMore || {})[k] || 12) + 24 } }); },

    // ------------------------------------------------ follow-ups
    fuNewOpen: el => {
      const tomorrow = new Date(Date.now() + 86400000);
      set({ fuNew: { at: el.dataset.at, id: Number(el.dataset.id), editId: null, title: '', date: isoDay(tomorrow), time: '', notes: '', saving: false, error: '' }, actNew: null, focus: 'fu-title' });
    },
    fuEdit: el => {
      const f = findFollowup(Number(el.dataset.id)), key = feedKeyFor(u());
      if (!f || !key) return;
      const [at, id] = key.split(':');
      set({ fuNew: { at, id: Number(id), editId: f.id, title: f.title, date: f.due_date, time: f.due_time || '', notes: f.notes || '', saving: false, error: '' }, actNew: null, focus: 'fu-title' });
    },
    fuCancel: () => set({ fuNew: null }),
    fuSave: async () => {
      const n = u().fuNew;
      if (!n || n.saving) return;
      if (!n.title.trim()) return set({ fuNew: { ...n, error: 'Say what needs doing' }, focus: 'fu-title' });
      if (!n.date) return set({ fuNew: { ...n, error: 'Pick a due date' } });
      const body = { title: n.title.trim(), due_date: n.date, due_time: n.time || null, notes: n.notes.trim() || null };
      set({ fuNew: { ...n, saving: true, error: '' } });
      try {
        if (n.editId) await more.updateFollowup(n.editId, body);
        else await more.createFollowup({ ...body, [SUBJECT_KEY[n.at]]: n.id });
        set({ fuNew: null });
        if (!n.editId) toast('Follow-up added');
        await reload(n.at, n.id);
      } catch (e) { set({ fuNew: { ...u().fuNew, saving: false, error: message(e) } }); }
    },
    fuDelete: async el => {
      const id = Number(el.dataset.id);
      if (!(await confirmNear(el, 'Delete this follow-up?', 'Delete'))) return;
      try { await more.deleteFollowup(id); set({ fuNew: null }); await reload(); } catch (e) { fail(e); }
    },
    fuDone: el => setDone(Number(el.dataset.id), el.dataset.v === 'open' ? 'open' : 'done'),
    // Home / Calendar: open what the follow-up is on.
    fuOpen: el => {
      const f = findFollowup(Number(el.dataset.id));
      if (!f) return;
      if (el.closest('.sheet')) store.ui.sheet = null;
      if (f.subject === 'project') navigate('project', f.subject_id, 'overview');
      else if (f.subject === 'client') navigate('client', f.subject_id);
      else if (f.subject === 'contact') navigate('contact', f.subject_id);
    },
  };
}

// data-input "act.<when|summary|outcome>" (the log form), "fu.<title|date|time|notes>" (the follow-up form).
export function crmActivityInput(path, v) {
  const ui = u(), [group, k] = path.split('.');
  const key = group === 'act' ? 'actNew' : 'fuNew';
  if (ui[key]) ui[key] = { ...ui[key], [k]: v, error: '' };
}
