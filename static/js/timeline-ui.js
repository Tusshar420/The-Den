// Glue between the project calendar (views/calendar.js) and app.js: its actions (data-action="…") and
// form inputs. app.js passes in the few helpers it owns.

import { store } from './api.js';
import { more, loadTimeline, loadBriefing, projectById, realProjects } from './data.js';
import { kinds, todayOf, events, isoDay, parseDay } from './views/calendar.js';
import * as voice from './voice.js';

const u = () => store.ui;
const defaultRemind = kind => [...((kinds(store.data)[kind] || {}).remind || [1, 0])];

// Everything that shows timeline items re-reads after a change (the briefing too, when it's open).
function reload() {
  const jobs = [loadTimeline()];
  if (u().view === 'briefing') jobs.push(loadBriefing());
  return Promise.all(jobs).catch(() => {});
}

export function timelineActions(h) {
  const { set, toast, fail, navigate, openPalette } = h;
  const setEv = patch => { const e = u().evEdit; if (e) set({ evEdit: { ...e, error: '', ...patch } }); };
  const setCal = patch => set({ cal: { ...u().cal, ...patch } });

  function openEvent(el) {
    const id = el.dataset.id ? Number(el.dataset.id) : null;
    if (id) {
      const e = events(store.data).find(x => x.id === id);
      if (!e) return;
      return set({ sheet: 'event', focus: null, evEdit: { id, pid: e.project_id, kind: e.kind, title: e.title, date: e.date, time: e.time || '', notes: e.notes || '',
        remind: e.remind_days ? [...e.remind_days] : defaultRemind(e.kind), remindTouched: false, status: e.status, saving: false, error: '' } });
    }
    const kind = el.dataset.kind || 'deadline';
    // New: the project it was opened from, else the one on screen / last opened, else the first.
    const ui = u(), real = realProjects();
    const pid = Number(el.dataset.pid) || (ui.view === 'project' && projectById(ui.projectId) && !projectById(ui.projectId).is_general ? ui.projectId : null)
      || (ui.lastProjectId && real.some(p => p.id === ui.lastProjectId) ? ui.lastProjectId : null) || (real[0] || {}).id || null;
    set({ sheet: 'event', focus: 'evTitle', evEdit: { id: null, pid, kind, title: '', date: el.dataset.date || todayOf(store.data), time: '', notes: '',
      remind: defaultRemind(kind), remindTouched: false, status: 'open', saving: false, error: '' } });
  }

  async function saveEvent() {
    const e = u().evEdit;
    if (!e || e.saving || !e.title.trim() || !e.date || !e.pid) return;
    setEv({ saving: true });
    const body = { kind: e.kind, title: e.title.trim(), date: e.date, time: e.time || null, notes: e.notes.trim() || null };
    if (e.remindTouched) body.remind_days = e.remind;
    try {
      if (e.id) {
        const was = events(store.data).find(x => x.id === e.id);
        await more.updateEvent(e.id, { ...body, ...(was && was.project_id !== e.pid ? { project_id: e.pid } : {}) });
      } else {
        await more.addEvent(e.pid, body);
      }
      await reload();
      set({ sheet: null, evEdit: null });
      const p = projectById(e.pid);
      toast(e.id ? 'Saved' : `Added to ${p ? p.name : 'the'} timeline`);
    } catch (err) {
      if (u().evEdit) set({ evEdit: { ...u().evEdit, saving: false, error: err.message || String(err) } });
    }
  }

  // Tick a deadline / milestone / task done (or not) right in the list.
  function setStatus(id, status, msg) {
    const tl = store.data.timeline;
    store.data = { ...store.data, timeline: { ...tl, events: tl.events.map(x => (x.id === id ? { ...x, status, overdue: status === 'open' ? x.overdue : false } : x)) } };
    set({});
    more.updateEvent(id, { status }).then(() => { if (msg) toast(msg); return reload(); }, e => { fail(e); reload(); });
  }

  return {
    openEvent,
    saveEvent,
    evKind: el => { const e = u().evEdit; if (e) setEv({ kind: el.dataset.kind, ...(e.remindTouched ? {} : { remind: defaultRemind(el.dataset.kind) }) }); },
    evProject: el => setEv({ pid: Number(el.dataset.id) }),
    evAllDay: () => setEv({ time: '' }),
    evRemind: el => {
      const e = u().evEdit, d = Number(el.dataset.d);
      if (!e) return;
      setEv({ remind: e.remind.includes(d) ? e.remind.filter(x => x !== d) : [...e.remind, d].sort((a, b) => b - a), remindTouched: true });
    },
    evStatus: el => {
      const e = u().evEdit;
      if (!e) return;
      set({ sheet: null, evEdit: null });
      setStatus(e.id, el.dataset.status, el.dataset.status === 'done' ? 'Marked done' : 'Reopened');
    },
    deleteEvent: () => {
      const e = u().evEdit;
      if (!e || !e.id) return;
      const tl = store.data.timeline;
      store.data = { ...store.data, timeline: { ...tl, events: tl.events.filter(x => x.id !== e.id) } };
      set({ sheet: null, evEdit: null });
      more.deleteEvent(e.id).then(() => { toast('Deleted'); return reload(); }, err => { fail(err); reload(); });
    },
    eventDone: el => {
      const id = Number(el.dataset.id);
      const e = events(store.data).find(x => x.id === id);
      if (e) setStatus(id, e.status === 'done' ? 'open' : 'done', e.status === 'done' ? 'Reopened' : `Done: ${e.title}`);
    },
    calMonth: el => {
      const d = Number(el.dataset.d);
      if (!d) { const t = todayOf(store.data); return setCal({ month: t.slice(0, 7), day: t }); }
      const m = parseDay(u().cal.month + '-01');
      m.setMonth(m.getMonth() + d);
      setCal({ month: isoDay(m).slice(0, 7) });
    },
    calDay: el => {
      const day = el.dataset.date;
      setCal({ day, month: day.slice(0, 7) });
    },
    calProject: el => setCal({ project: el.dataset.id === 'all' ? 'all' : Number(el.dataset.id) }),
    openProjectTimeline: el => navigate('project', Number(el.dataset.id), 'timeline'),
    tlShowDone: () => set({ tlShowDone: !u().tlShowDone }),
    // Claude looks at the code and backlog against the dates (it gets the timeline with every task) - nothing runs until you press Queue.
    checkProgress: el => openPalette({ projectId: Number(el.dataset.pid),
      text: "Check this project's progress against its timeline. For the nearest deadline or milestone, look at the code and the backlog "
        + "and tell me what is done, what is still missing, and whether we're on track. Suggest the next steps in order, and flag anything that puts a date at risk." }),
    briefingRefresh: async () => {
      set({ busy: { ...u().busy, briefing: true } });
      try { await Promise.all([loadBriefing(), loadTimeline()]); } catch (e) { fail(e); }
      set({ busy: { ...u().busy, briefing: false } });
    },
    briefingSpeak: () => { const b = store.data.briefing; if (b) voice.speak(b.spoken); },
    timelineSetting: el => {
      const k = el.dataset.k, s = store.data.settings || {};
      const on = s[k] === false;
      store.data = { ...store.data, settings: { ...s, [k]: on } };
      set({});
      more.setSettings({ [k]: on }).then(v => { store.data = { ...store.data, settings: v }; set({}); }, fail);
    },
  };
}

// data-input="ev.…" (the sheet's fields) and "set.briefing_time".
let timeSave = 0;
export function timelineInput(path, v, { fail }) {
  if (path.startsWith('ev.') && u().evEdit) {
    u().evEdit[path.slice(3)] = v;
    u().evEdit.error = '';
    return true;
  }
  if (path === 'set.briefing_time') {
    clearTimeout(timeSave);
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(v)) return true;
    timeSave = setTimeout(() => more.setSettings({ briefing_time: v }).then(s => { store.data = { ...store.data, settings: s }; }, fail), 600);
    return true;
  }
  return false;
}

// The calendar opens on this month, today selected.
export const calState = () => { const t = isoDay(new Date()); return { month: t.slice(0, 7), day: t, project: 'all' }; };
