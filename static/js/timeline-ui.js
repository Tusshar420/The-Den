// Glue between the project calendar (views/calendar.js) and app.js: its actions (data-action="…") and
// form inputs. app.js passes in the few helpers it owns.

import { store } from './api.js';
import { more, loadTimeline, loadBriefing, projectById, realProjects } from './data.js';
import { kinds, todayOf, events, isoDay, parseDay, weekdayOf, WITH_PEOPLE, REPEATABLE, ruleOf, repOf, presetRep, ruleBody, DEFAULT_DURATION, inviteEmails } from './views/calendar.js';
import * as voice from './voice.js';

const u = () => store.ui;
const defaultRemind = kind => [...((kinds(store.data)[kind] || {}).remind || [1, 0])];
const EMAIL_RE = /^[^@\s<>(),;:"]+@[^@\s<>(),;:"]+\.[^@\s<>(),;:"]+$/;
// A meeting's guests and Google Calendar invite, as the sheet edits them (see invites.py).
const inviteState = e => ({ guests: [...((e && e.guests) || [])], guestDraft: '', duration: (e && e.duration) || DEFAULT_DURATION,
  gid: (e && e.google_event_id) || null, invite: !!(e && e.google_event_id), meet: !!(e && e.meet_link), meetLink: (e && e.meet_link) || '',
  inviteLink: (e && e.invite_link) || '', inviteError: (e && e.invite_error) || '' });

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
      // A repeating meeting is edited as a whole, from its first day.
      return set({ sheet: 'event', focus: null, evEdit: { id, pid: e.project_id, kind: e.kind, title: e.title, date: e.start_date || e.date, time: e.time || '', notes: e.notes || '',
        rep: repOf(e), until: e.repeat_until || '', remind: e.remind_days ? [...e.remind_days] : defaultRemind(e.kind), remindTouched: false, people: [...(e.contact_ids || [])], staff: [...(e.employee_ids || [])], status: e.status, saving: false, error: '', ...inviteState(e) } });
    }
    const kind = el.dataset.kind || 'deadline';
    // New: the project it was opened from, else the one on screen / last opened, else the first.
    const ui = u(), real = realProjects();
    const pid = Number(el.dataset.pid) || (ui.view === 'project' && projectById(ui.projectId) && !projectById(ui.projectId).is_general ? ui.projectId : null)
      || (ui.lastProjectId && real.some(p => p.id === ui.lastProjectId) ? ui.lastProjectId : null) || (real[0] || {}).id || null;
    set({ sheet: 'event', focus: 'evTitle', evEdit: { id: null, pid, kind, title: '', date: el.dataset.date || todayOf(store.data), time: '', notes: '',
      rep: null, until: '', remind: defaultRemind(kind), remindTouched: false, people: [], staff: [], status: 'open', saving: false, error: '', ...inviteState(null) } });
  }

  async function saveEvent() {
    const e = u().evEdit;
    if (!e || e.saving || !e.title.trim() || !e.date || !e.pid) return;
    // An email typed in but not added yet still counts.
    const draft = e.guestDraft.trim().toLowerCase();
    if (draft && !EMAIL_RE.test(draft)) return setEv({ error: `${draft} isn't an email address` });
    const guests = draft && !e.guests.includes(draft) ? [...e.guests, draft] : e.guests;
    setEv({ saving: true, guests, guestDraft: '' });
    const body = { kind: e.kind, title: e.title.trim(), date: e.date, time: e.time || null, notes: e.notes.trim() || null };
    if (e.remindTouched) body.remind_days = e.remind;
    const meeting = WITH_PEOPLE.includes(e.kind);
    if (meeting) {
      Object.assign(body, { contact_ids: e.people, employee_ids: e.staff || [], guests, duration: e.time ? e.duration : null });
      // invite: true sends it (or retries a failed one), false cancels a sent one; meet adds a Meet link.
      if (e.gid) {
        if (!e.invite) body.invite = false;
        else if (e.meet && !e.meetLink) body.meet = true;
        else if (e.inviteError) body.invite = true;
      } else if (e.invite || e.meet) Object.assign(body, { invite: true, meet: e.meet });
    }
    if (REPEATABLE.includes(e.kind)) Object.assign(body, { repeat_days: null, repeat_rule: ruleBody(e), repeat_until: (e.rep && e.until) || null });
    try {
      let saved;
      if (e.id) {
        const was = events(store.data).find(x => x.id === e.id);
        saved = await more.updateEvent(e.id, { ...body, ...(was && was.project_id !== e.pid ? { project_id: e.pid } : {}) });
      } else {
        saved = await more.addEvent(e.pid, body);
      }
      await reload();
      set({ sheet: null, evEdit: null });
      const p = projectById(e.pid);
      const done = e.id ? 'Saved' : `Added to ${p ? p.name : 'the'} timeline`;
      if (saved && saved.invite_error) toast(`${done}, but the invite wasn't sent: ${saved.invite_error}`);
      else if (meeting && body.invite === false) toast(`${done} · invite cancelled`);
      else if (saved && saved.google_event_id && !e.gid) {
        const n = inviteEmails({ d: store.data }, { people: e.people, guests }).emails.length;
        toast(`${done} · ${n ? `invite sent to ${n === 1 ? '1 person' : n + ' people'}` : 'on your Google Calendar'}${saved.meet_link ? ' with a Meet link' : ''}`);
      } else toast(done);
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
    evPerson: el => {
      const e = u().evEdit, id = Number(el.dataset.id);
      if (e) setEv({ people: e.people.includes(id) ? e.people.filter(x => x !== id) : [...e.people, id] });
    },
    evStaff: el => {
      const e = u().evEdit, id = Number(el.dataset.id);
      if (e) setEv({ staff: (e.staff || []).includes(id) ? e.staff.filter(x => x !== id) : [...(e.staff || []), id] });
    },
    evStaffPick: el => {
      const e = u().evEdit, id = Number(el.value);
      el.value = '';
      if (e && id && !(e.staff || []).includes(id)) setEv({ staff: [...(e.staff || []), id] });
    },
    evPersonPick: el => {
      const e = u().evEdit, id = Number(el.value);
      el.value = '';
      if (e && id && !e.people.includes(id)) setEv({ people: [...e.people, id] });
    },
    evAllDay: () => setEv({ time: '' }),
    evGuestAdd: () => {
      const e = u().evEdit;
      if (!e) return;
      const g = e.guestDraft.trim().toLowerCase();
      if (!g) return;
      if (!EMAIL_RE.test(g)) return setEv({ error: `${g} isn't an email address` });
      setEv({ guests: e.guests.includes(g) ? e.guests : [...e.guests, g], guestDraft: '' });
    },
    evGuestDel: el => { const e = u().evEdit; if (e) setEv({ guests: e.guests.filter(g => g !== el.dataset.email) }); },
    evDuration: el => setEv({ duration: Number(el.dataset.m) }),
    // Turning the invite off also drops a Meet link not sent yet; turning the Meet link on sends the invite with it.
    evInvite: () => { const e = u().evEdit; if (e) setEv({ invite: !e.invite, meet: e.invite ? false : e.meet }); },
    evMeet: () => { const e = u().evEdit; if (e && !e.meetLink) setEv({ meet: !e.meet, invite: e.meet ? e.invite : true }); },
    evRepeat: el => {
      const e = u().evEdit;
      if (e && el.dataset.k !== 'keep') setEv({ rep: presetRep(el.dataset.k) });
    },
    evRepEvery: el => { const e = u().evEdit; if (e && e.rep) setEv({ rep: { ...e.rep, every: Number(el.value) } }); },
    // Toggle a weekday on a weekly repeat (one always stays on).
    evRepDay: el => {
      const e = u().evEdit, wd = Number(el.dataset.wd);
      if (!e || !e.rep) return;
      const on = e.rep.on || [weekdayOf(e.date)];
      const next = on.includes(wd) ? on.filter(x => x !== wd) : [...on, wd].sort((a, b) => a - b);
      if (next.length) setEv({ rep: { ...e.rep, on: next } });
    },
    evRepMonthly: el => { const e = u().evEdit; if (e && e.rep) setEv({ rep: { ...e.rep, monthly: el.dataset.k } }); },
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
      const tl = store.data.timeline, old = tl.events.find(x => x.id === e.id);
      store.data = { ...store.data, timeline: { ...tl, events: tl.events.filter(x => x.id !== e.id) } };
      set({ sheet: null, evEdit: null });
      const undo = old ? { action: () => { const body = { kind: old.kind, title: old.title, date: old.start_date || old.date, time: old.time || null, notes: old.notes || null }; if (Array.isArray(old.remind_days)) body.remind_days = old.remind_days; if (old.contact_ids && old.contact_ids.length) body.contact_ids = old.contact_ids; if (old.employee_ids && old.employee_ids.length) body.employee_ids = old.employee_ids; if (old.guests && old.guests.length) body.guests = old.guests; if (old.duration) body.duration = old.duration; if (ruleOf(old)) Object.assign(body, { repeat_rule: ruleOf(old), repeat_until: old.repeat_until || null }); return more.addEvent(old.project_id, body).then(reload, fail); }, actionLabel: 'Undo' } : {};
      more.deleteEvent(e.id).then(r => { toast((r && r.warning) || (r && r.invite_cancelled ? 'Deleted · invite cancelled' : 'Deleted'), undo); return reload(); }, err => { fail(err); reload(); });
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
