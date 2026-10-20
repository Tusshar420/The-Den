// Glue between the project Phases tab (views/phases.js) and app.js: its actions (data-action="…") and
// form inputs. app.js passes in the few helpers it owns.

import { store, endpoints } from './api.js';
import { more, loadPhases } from './data.js';

const u = () => store.ui;
const pid = () => u().projectId;

export function phaseActions(h) {
  const { set, toast, fail } = h;
  const setEdit = patch => { const e = u().phEdit; if (e) set({ phEdit: { ...e, error: '', ...patch } }); };
  const phaseOf = id => ((store.data.phases[pid()] || {}).phases || []).find(x => x.id === id);
  const busy = (id, on) => { const b = { ...u().busy }; if (on) b[`ph${id}`] = true; else delete b[`ph${id}`]; set({ busy: b }); };
  const refresh = () => loadPhases(pid()).catch(() => {});

  async function patchSettings(patch) {
    const p = pid(), cur = store.data.projectSettings[p];
    try {
      await more.setProjectSettings(p, patch);
      await refresh();
    } catch (e) { if (cur) store.data = { ...store.data, projectSettings: { ...store.data.projectSettings, [p]: cur } }; fail(e); refresh(); }
  }

  async function savePhase() {
    const e = u().phEdit;
    if (!e || e.saving || !e.name.trim()) return;
    setEdit({ saving: true });
    const items = e.todos.split('\n').map(s => s.trim()).filter(Boolean);
    const body = { name: e.name.trim(), goal: e.goal.trim() || null, description: (e.description || '').trim() || null, start_date: e.start_date || null, end_date: e.end_date || null };
    try {
      if (e.id) {
        await more.updatePhase(e.id, body);
        for (const text of items) {
          const t = await endpoints.addTodo(e.pid, text);
          await more.assignTodo(t.id, e.id);
        }
      } else {
        await more.addPhase(e.pid, { ...body, todos: items });
      }
      await refresh();
      set({ sheet: null, phEdit: null });
      toast(e.id ? 'Saved' : 'Phase added');
    } catch (err) {
      if (u().phEdit) set({ phEdit: { ...u().phEdit, saving: false, error: err.message || String(err) } });
    }
  }

  // The report sheet (views/phases.js reportSheet). A change of what it covers drops the old report.
  const setReport = patch => { const r = u().phReport; if (r) set({ phReport: { ...r, ...patch } }); };
  async function makeReport() {
    const r = u().phReport;
    if (!r || r.loading) return;
    const ask = { pid: r.pid, ids: r.ids.join(), overview: r.overview };
    setReport({ loading: true, error: '', result: null });
    let patch;
    try { patch = { result: await more.report(r.pid, { phase_ids: r.ids.length ? r.ids : null, overview: r.overview }) }; } catch (err) { patch = { error: err.message || String(err) }; }
    const now = u().phReport;   // closed, or asked for something else, meanwhile
    if (now && now.pid === ask.pid && now.ids.join() === ask.ids && now.overview === ask.overview) setReport({ loading: false, ...patch });
  }
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error("Couldn't copy - select the text instead"));
  }
  const result = () => (u().phReport || {}).result;

  return {
    openReport: el => {
      const id = el.dataset.id ? Number(el.dataset.id) : null;
      set({ sheet: 'report', phReport: { pid: pid(), ids: id ? [id] : [], overview: true, loading: false, result: null, error: '' } });
      if (id) makeReport();
    },
    reportPick: el => {
      const r = u().phReport;
      if (!r || r.loading) return;
      const id = el.dataset.id ? Number(el.dataset.id) : null;
      const ids = !id ? [] : r.ids.includes(id) ? r.ids.filter(x => x !== id) : [...r.ids, id];
      setReport({ ids, result: null, error: '' });
    },
    reportOverview: () => { const r = u().phReport; if (r && !r.loading) setReport({ overview: !r.overview, result: null, error: '' }); },
    reportMake: makeReport,
    reportCopy: () => { const x = result(); if (x) copyText(x.markdown).then(() => toast('Report copied'), fail); },
    reportDownload: () => {
      const x = result();
      if (!x) return;
      const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(new Blob([x.markdown], { type: 'text/markdown' })), download: x.filename });
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    },
    reportShare: () => {
      const x = result();
      if (x) navigator.share({ title: x.title, text: x.markdown }).catch(err => { if (err.name !== 'AbortError') fail(err); });
    },
    openPhase: el => {
      const id = el.dataset.id ? Number(el.dataset.id) : null;
      const ph = id ? phaseOf(id) : null;
      if (id && !ph) return;
      set({ sheet: 'phase', focus: 'phName', phEdit: { id, pid: pid(), name: ph ? ph.name : '', goal: ph ? ph.goal || '' : '', description: ph ? ph.description || '' : '',
        start_date: ph ? ph.start_date || '' : '', end_date: ph ? ph.end_date || '' : '', todos: '', saving: false, error: '' } });
    },
    savePhase,
    openGen: async el => {
      const id = Number(el.dataset.id);
      set({ sheet: 'genTasks', phGen: { id, models: null, model: '', loading: false, error: '' } });
      try {
        const models = await more.genModels();
        if (u().phGen && u().phGen.id === id) set({ phGen: { ...u().phGen, models } });
      } catch (err) { if (u().phGen) set({ phGen: { ...u().phGen, models: [], error: err.message || String(err) } }); }
    },
    genModel: el => { const g = u().phGen; if (g && !g.loading) set({ phGen: { ...g, model: el.dataset.model, error: '' } }); },
    genRun: async () => {
      const g = u().phGen;
      if (!g || g.loading) return;
      set({ phGen: { ...g, loading: true, error: '' } });
      try {
        const r = await more.generateTasks(g.id, g.model || null);
        await refresh();
        if (u().phGen && u().phGen.id === g.id) set({ sheet: null, phGen: null, phOpen: { ...(u().phOpen || {}), [g.id]: true } });
        toast(`Added ${r.tasks.length} task${r.tasks.length === 1 ? '' : 's'}`);
      } catch (err) { if (u().phGen && u().phGen.id === g.id) set({ phGen: { ...u().phGen, loading: false, error: err.message || String(err) } }); }
    },
    deletePhase: async () => {
      const e = u().phEdit;
      if (!e || !e.id) return;
      set({ sheet: null, phEdit: null });
      try { await more.deletePhase(e.id); toast('Phase deleted. Its items are still in the backlog.'); } catch (err) { fail(err); }
      refresh();
    },
    phaseToggle: el => {
      const id = Number(el.dataset.id), ph = phaseOf(id), cur = (u().phOpen || {})[id];
      const isOpen = cur === undefined ? !!ph && ph.status === 'active' : cur;
      set({ phOpen: { ...(u().phOpen || {}), [id]: !isOpen } });
    },
    phaseItem: async el => {
      const id = Number(el.dataset.id), part = el.dataset.part, all = u().phItem || {}, cur = all[id] || {};
      set({ phItem: { ...all, [id]: { ...cur, [part]: !cur[part] } } });
      // Opening a finished item fetches its plain-language summary once (the server caches it on the task).
      const t = (store.data.todos[pid()] || []).find(x => x.id === id);
      if (part !== 'open' || cur.open || !t || !t.task_id || t.task_status !== 'done' || (u().phSum || {})[t.task_id]) return;
      const put = v => set({ phSum: { ...(u().phSum || {}), [t.task_id]: v } });
      put({ loading: true });
      try { put({ text: (await more.summarise(t.task_id)).summary || '' }); } catch (err) { put({ error: err.message || String(err) }); }
    },
    phaseDo: async el => {
      const id = Number(el.dataset.id), verb = el.dataset.verb;
      busy(id, true);
      try {
        await more.phaseDo(id, verb);
        toast({ start: 'Started. Octopus is on it.', pause: 'Paused', complete: 'Marked done', reopen: 'Reopened' }[verb] || 'Done');
      } catch (err) { fail(err); }
      busy(id, false);
      refresh();
    },
    phaseMove: async el => {
      const list = (store.data.phases[pid()] || {}).phases || [];
      const ids = list.map(x => x.id), i = ids.indexOf(Number(el.dataset.id)), j = i + Number(el.dataset.d);
      if (i < 0 || j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      const p0 = pid(), prev = list.map(x => x.id);
      try {
        await more.reorderPhases(p0, ids);
        toast('Phase moved', { action: () => more.reorderPhases(p0, prev).then(refresh, fail), actionLabel: 'Undo' });
      } catch (err) { fail(err); }
      refresh();
    },
    assignPhase: async el => {
      try { await more.assignTodo(Number(el.dataset.id), Number(el.dataset.phase)); } catch (err) { fail(err); }
      refresh();
    },
    autopilotMode: el => patchSettings({ autopilot: el.dataset.mode }),
    ceilingStep: el => {
      const s = store.data.projectSettings[pid()];
      if (s) patchSettings({ autopilot_ceiling: Math.min(100, Math.max(10, (s.autopilot_ceiling || 85) + Number(el.dataset.d))) });
    },
  };
}

export function phaseInput(path, v) {
  if (path.startsWith('ph.') && u().phEdit) {
    u().phEdit[path.slice(3)] = v;
    u().phEdit.error = '';
    return true;
  }
  return false;
}
