// Glue between the Octopus modules and app.js: every new action (data-action="…") in one place, plus
// octoSent(), which turns POST /octopus's answer into what the screen should do - queued, a plan
// to review, or an instant answer. app.js passes in the few helpers it owns.

import { store } from './api.js';
import { octoApi, putOcto, loadRouting, loadPolicies, loadProviders, loadOverview, loadDiff, loadDetail, latestInChat } from './octopus-data.js';
import * as voice from './voice.js';

const o = () => store.ui.octo;

export function octopusActions(h) {
  const { set, toast, fail, again, armAutoOpen, requestRender, openVoicePane } = h;
  const setOcto = patch => set({ octo: { ...o(), ...patch } });
  const setPlan = patch => setOcto({ plan: o().plan ? { ...o().plan, ...patch } : null });
  const settings = () => store.data.settings || {};
  const saveSetting = async (patch, msg) => {
    store.data = { ...store.data, settings: { ...settings(), ...patch } };
    requestRender();
    try { await octoApi.setSettings(patch); if (msg) toast(msg); } catch (e) { fail(e); }
    again();
    loadPolicies();
  };

  // ------------------------------------------------------------ plan card
  function openPlan(r, preview = null, over = { project: null, model: null, agent: null }) {
    set({ sheet: 'plan', palette: null, octo: { ...o(), plan: { routingId: r.routing_id ?? null, plan: r.plan || r, over, why: false, advanced: false, busy: false, error: '', preview } } });
  }
  octoSent.h = { openPlan };
  const overArgs = st => ({
    project_id: st.over.project ?? undefined,
    model: st.over.model || undefined,
    agent: st.over.agent || undefined,
  });

  // Re-plan with the overrides applied so the card always shows what Run will do.
  async function replan() {
    const st = o().plan;
    if (!st) return;
    setPlan({ busy: true, error: '' });
    try {
      const keepProject = st.over.project ?? (st.plan.project && !st.plan.project.is_general ? st.plan.project.id : undefined);
      const plan = await octoApi.plan(st.plan.request, { project_id: keepProject, model: st.over.model || 'auto', agent: st.over.agent || undefined, images: st.plan.images });
      setPlan({ plan, busy: false });
    } catch (e) { setPlan({ busy: false, error: e.message }); }
  }

  async function runPlan() {
    const st = o().plan;
    if (!st || st.busy) return;
    setPlan({ busy: true, error: '' });
    try {
      const ov = overArgs(st);
      const r = st.routingId != null
        ? await octoApi.runPlan(st.routingId, { ...ov, permission_mode: st.preview && st.preview.mode || undefined })
        : await octoApi.send(st.plan.request, { ...ov, model: ov.model || 'auto', review: 'skip', priority: st.preview ? st.preview.priority : 0, permission_mode: st.preview ? st.preview.mode : undefined,
          images: st.plan.images && st.plan.images.length ? st.plan.images : undefined });
      set({ sheet: null, octo: { ...o(), plan: null } });
      const out = octoSent(r);
      if (st.routingId == null) { store.ui.ask.text = ''; const ta = document.getElementById('askInput'); if (ta) { ta.value = ''; ta.style.height = ''; } }
      const d = r.decision || {};
      toast(r.task ? `Running · ${d.agent || 'Octopus'} · ${d.model_label || ''} · #${r.task.id}` : (out.answer || 'Done'), r.task ? { taskId: r.task.id } : undefined);
      if (out.taskId != null) armAutoOpen(out.taskId);
      again();
    } catch (e) { setPlan({ busy: false, error: e.message }); }
  }

  async function cancelPlan() {
    const st = o().plan;
    if (!st || st.busy) return;
    set({ sheet: null, octo: { ...o(), plan: null } });
    if (st.routingId != null) { try { await octoApi.cancelPlan(st.routingId); } catch (e) { return fail(e); } toast('Cancelled · nothing was run'); loadOverview(); }
  }

  // ------------------------------------------------------------ voice
  // Tapping the mic starts a conversation: listen, answer out loud, listen again - until you tap the mic,
  // say "stop", or stay quiet for a couple of turns.
  const setVoiceState = patch => setOcto({ voice: { ...o().voice, ...patch } });
  const talking = () => o().voice.conversation;
  const inVoicePane = () => (store.ui.pane || {}).type === 'voice';

  // The voice pane's transcript. Octopus's lines type themselves out while they are spoken.
  let lineId = 0;
  const say = (who, text) => {
    const id = ++lineId, mine = who === 'octo';
    setVoiceState({ log: [...(o().voice.log || []), { id, who, text, typed: mine ? 0 : text.length }] });
    if (!mine) return;
    const timer = setInterval(() => {
      const log = o().voice.log || [], e = log.find(x => x.id === id);
      if (!e || e.typed >= text.length) return clearInterval(timer);   // finished, or the chat was cleared
      setVoiceState({ log: log.map(x => x.id === id ? { ...x, typed: Math.min(text.length, x.typed + 2) } : x) });
    }, 45);
  };
  const ENDS = /^\s*(stop|stop listening|goodbye|good bye|bye|that'?s all|that is all|thanks,? that'?s all|end (the )?(conversation|chat)|never ?mind)\s*[.!]*\s*$/i;
  let silent = 0;

  function endConversation(msg = '') {
    voice.cancelListening();
    voice.stopSpeaking();
    silent = 0;
    setVoiceState({ state: 'idle', conversation: false, text: '', error: msg });
  }
  function listenTurn() {
    if (!talking() || o().voice.state !== 'idle') return;
    voice.listen(text => handleVoice({ text }), blob => handleVoice({ blob }), failed => {
      if (!talking()) return;
      if (failed) return endConversation(o().voice.error);
      if (++silent >= 2) return endConversation('Conversation ended - I didn\'t hear anything.');
      listenTurn();
    });
  }
  const resume = () => { if (talking() && o().voice.state === 'idle') listenTurn(); };

  // A finished task's answer can arrive while you are mid-conversation: say it as soon as nothing else is happening.
  async function speakLate(text) {
    for (let i = 0; i < 120; i++) {
      const v = o().voice;
      if (v.state === 'idle') break;
      if (v.state === 'listening' && v.conversation && !v.text) { voice.cancelListening(); break; }
      await new Promise(r => setTimeout(r, 500));
    }
    if (!talking() || o().voice.state !== 'idle') return;
    say('octo', text);
    await voice.speak(text);
    resume();
  }
  // Waits (up to about five minutes) for a queued task to finish, then says its answer - as long as the chat is still on.
  async function followUp(taskId) {
    for (let i = 0; i < 15 && talking(); i++) {
      let res;
      try { res = await octoApi.result(taskId, 20); } catch { return; }
      if (res.finished) { if (talking() && res.spoken) speakLate(res.spoken); return; }
    }
  }
  async function handleVoice({ text, blob }) {
    silent = 0;
    if (text && talking() && ENDS.test(text)) return endConversation();
    setVoiceState({ state: 'working', heard: text || '…', error: '' });
    let r;
    try { r = blob ? await voice.sendAudio(blob) : await voice.sendText(text); }
    catch (e) { return talking() ? endConversation(e.message) : setVoiceState({ state: 'idle', error: e.message }); }
    if (!talking() && o().voice.state !== 'working') return;   // stopped while it was thinking
    const heard = r.transcript || text || '';
    if (talking() && ENDS.test(heard)) return endConversation();
    setVoiceState({ heard });
    if (heard) say('you', heard);
    const st = o().plan;
    if (st && r.routing_id === st.routingId && r.status !== 'proposed') set({ sheet: null, octo: { ...o(), plan: null } });   // "yes, go ahead" answered it
    const out = octoSent(r);
    // The voice pane stays put: opening the task's chat over it would cut the conversation off.
    if (out.taskId != null && !inVoicePane()) armAutoOpen(out.taskId);
    again(); loadOverview();
    if (r.status === 'queued' && r.task) followUp(r.task.id);
    // A voice chat is answered by voice - whatever the "speak replies" habit of typed requests is.
    if (r.spoken) { say('octo', r.spoken); await voice.speak(r.spoken); }
    else setVoiceState({ state: 'idle' });
    resume();
  }
  // Tapping the mic opens the voice pane on the right and starts the conversation there.
  function mic() {
    if (talking() || o().voice.state === 'listening') return endConversation();
    voice.unlockAudio();                                       // browsers only allow sound after a tap
    silent = 0;
    if (!inVoicePane()) { openVoicePane(); setVoiceState({ log: [] }); }
    setVoiceState({ conversation: true, state: 'idle', error: '' });
    listenTurn();
  }

  return {
    // ---- Ask box
    askAgent: el => { setOcto({ agent: el.dataset.id }); Object.assign(store.ui.ask, { menu: null, result: null }); requestRender(); },
    askPreview: async () => {
      const a = store.ui.ask, text = a.text.trim();
      if (!text) return;
      const gen = store.data.projects.find(p => p.is_general);
      const pid = a.projectId != null && !(gen && a.projectId === gen.id) ? a.projectId : undefined;
      try {
        const plan = await octoApi.plan(text, { project_id: pid, model: a.model, agent: o().agent === 'auto' ? undefined : o().agent });
        // the overrides already chosen in the Ask box carry into Run
        openPlan({ routing_id: null, plan }, { mode: a.mode, priority: 0 },
          { project: pid ?? null, model: a.model === 'auto' ? null : a.model, agent: o().agent === 'auto' ? null : o().agent });
      } catch (e) { fail(e); }
    },
    askAgain: el => { store.ui.ask.text = el.dataset.text; store.ui.ask.result = null; h.navigate('home'); },
    // ---- plan card
    planRun: () => runPlan(),
    planCancel: () => cancelPlan(),
    planClose: (el, e) => { if (e.target === el) set({ sheet: null, octo: { ...o(), plan: null } }); },
    planWhy: () => setPlan({ why: !o().plan.why }),
    planAdvanced: () => setPlan({ advanced: !o().plan.advanced }),
    planProject: el => { const st = o().plan; setPlan({ over: { ...st.over, project: el.dataset.id === '' ? null : Number(el.dataset.id) } }); replan(); },
    planModel: el => { const st = o().plan; setPlan({ over: { ...st.over, model: el.dataset.id === 'auto' ? null : el.dataset.id } }); replan(); },
    planAgent: el => { const st = o().plan; setPlan({ over: { ...st.over, agent: el.dataset.id === 'auto' ? null : el.dataset.id } }); replan(); },
    planReview: el => {
      const w = ((store.data.octo.overview || {}).plans_waiting || []).find(x => x.routing_id === Number(el.dataset.id));
      if (w) return openPlan({ routing_id: w.routing_id, plan: w.plan });
      octoApi.routing('limit=200').then(rows => { const r = rows.find(x => x.id === Number(el.dataset.id)); if (r) openPlan({ routing_id: r.id, plan: r.plan }); }, fail);
    },
    // ---- pages
    routingFilter: el => { setOcto({ routing: { ...o().routing, [el.dataset.k]: el.dataset.v, open: null } }); loadRouting(); },
    routingOpen: el => { const id = Number(el.dataset.id); setOcto({ routing: { ...o().routing, open: o().routing.open === id ? null : id } }); },
    toggleModel: el => {
      const id = el.dataset.id;
      const m = ((store.data.octo.providers || {}).providers || []).flatMap(g => g.models).find(x => x.id === id);
      octoApi.setModel(id, !(m && m.enabled !== false)).then(() => { loadProviders(); again(); }, fail);
    },
    // ---- settings
    setAutonomy: el => saveSetting({ autonomy: el.dataset.v }, 'Autonomy · ' + el.dataset.v),
    setPlanReview: el => saveSetting({ plan_review: el.dataset.v }),
    octoPolicy: el => octoApi.setPolicy(el.dataset.cap, el.dataset.policy).then(() => loadPolicies(), e => { fail(e); loadPolicies(); }),
    voiceProvider: el => { if (el.getAttribute('aria-disabled') === 'true') return toast('That provider isn\'t available yet - see the note under it'); saveSetting({ [el.dataset.key]: el.dataset.id }); },
    voiceTest: () => { voice.unlockAudio(); voice.speak('Hello Tushar. This is how I sound.'); },
    voicePick: async el => {
      const name = el.dataset.id;
      // The menu stays open so several voices can be tried in a row; the scrim closes it.
      if (voice.ttsMode() === 'macos') await saveSetting({ voice_tts_voice: name });
      else { voice.setBrowserVoiceName(name); requestRender(); }
      voice.unlockAudio();
      voice.speak('Hello Tushar. This is how I sound.');
    },
    // ---- voice
    voiceMic: () => mic(),
    voiceStop: () => endConversation(),
    // ---- task page
    taskTab: el => { setOcto({ detailOpen: el.dataset.v === 'details' }); const p = store.ui.pane; if (el.dataset.v === 'details' && p) loadDetail(latestInChat(p.taskId)); },
    taskDiff: el => loadDiff(Number(el.dataset.id)),
  };
}

// The input handler's branch for data-input="octo.*" fields.
export function octoInput(path, value, h) {
  if (path === 'octo.voice') {
    store.data = { ...store.data, settings: { ...(store.data.settings || {}), voice_tts_voice: value } };
    octoApi.setSettings({ voice_tts_voice: value }).catch(h.fail);
  }
}

// What to do with the answer to POST /octopus (or a plan run). Returns { taskId, result }:
//   queued            -> the task to follow, and the line the Ask box shows
//   proposed / needs_input -> the plan card opens
//   done / cancelled  -> an instant answer, shown under the Ask box
export function octoSent(r, h = octoSent.h) {
  const d = r.decision || {};
  if (r.status === 'proposed' || r.status === 'needs_input') {
    if (h && h.openPlan) h.openPlan(r);
    return { taskId: null, result: null, plan: true };
  }
  if (r.status === 'done' || r.status === 'cancelled') {
    const text = (r.result && r.result.text) || '';
    return { taskId: null, result: { model: 'Octopus', lane: 'control', why: d.category || 'instant', id: r.task && r.task.id, answer: text }, answer: text };
  }
  const reason = d.reason || '';
  const why = [d.agent, d.category === 'manual' || reason.startsWith(d.category) ? reason : `${d.category}: ${reason}`].filter(Boolean).join(' · ');
  return { taskId: r.task ? r.task.id : null, result: { model: d.model_label, lane: d.lane, why, id: r.task && r.task.id } };
}
