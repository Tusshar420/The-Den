// Glue between the Octopus modules and app.js: every new action (data-action="…") in one place, plus
// octoSent(), which turns POST /octopus's answer into what the screen should do - queued, a plan
// to review, or an instant answer. app.js passes in the few helpers it owns.

import { store } from './api.js';
import { octoApi, putOcto, loadRouting, loadPolicies, loadProviders, loadOverview, loadDiff, loadDetail, latestInChat, loadTokens, usageRange, loadAlexa } from './octopus-data.js';
import { usageCsv } from './views/usage.js';
import * as voice from './voice.js';
import { openPreview } from './preview.js';

const o = () => store.ui.octo;
// Token usage page: change its filters (a new slice starts from the top of the list) and reload.
function setUsage(patch) {
  const u = { ...o().usage, open: null, limit: 50, ...patch };
  if (u.period === 'custom' && (!u.start || !u.end)) [u.start, u.end] = usageRange(o().usage);
  store.ui.octo = { ...o(), usage: u };
  loadTokens(true);
}
const FILTERS = ['period', 'project', 'model', 'lane', 'account'];
const allModels = () => ((store.data.octo.providers || {}).providers || []).flatMap(g => g.models);
const blankModelForm = () => ({ id: null, label: '', provider: 'openai', model: '', modality: 'text', use_for: ['general'], coding: 3, reasoning: 3,
  cost: 3, speed: 3, vision: false, web_search: false, context_tokens: '', price_in: '', price_out: '', price_image: '', note: '', busy: false, error: '' });

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

  // One account change: mark busy, call, take the settings view it returns, show errors in the form.
  async function account(kind, call, done) {
    setOcto({ acct: { ...o().acct, busy: kind, error: '' } });
    try {
      const r = await call();
      if (r && r.claude_accounts) store.data = { ...store.data, settings: r };
      setOcto({ acct: { ...o().acct, busy: '' } });
      done(r);
    } catch (e) {
      setOcto({ acct: { ...o().acct, busy: '', error: kind === 'add' ? e.message : '' } });
      if (kind !== 'add') fail(e);
    }
  }

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
    setVoiceState({ state: 'idle', conversation: false, text: '', error: msg, pending: 0 });
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
  // Meanwhile the pane shows a "working on it" loader instead of Octopus announcing which agent took the job.
  async function followUp(taskId) {
    const bump = n => setVoiceState({ pending: Math.max(0, (o().voice.pending || 0) + n) });
    let spoken = '';
    bump(1);
    try {
      for (let i = 0; i < 15 && talking(); i++) {
        let res;
        try { res = await octoApi.result(taskId, 20); } catch { return; }
        if (res.finished) { spoken = res.spoken || ''; return; }
      }
    } finally {
      bump(-1);
      if (spoken && talking()) speakLate(spoken);
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
    voice.followLanguage(heard, r.spoken || '');
    const st = o().plan;
    if (st && r.routing_id === st.routingId && r.status !== 'proposed') set({ sheet: null, octo: { ...o(), plan: null } });   // "yes, go ahead" answered it
    const out = octoSent(r);
    // The voice pane stays put: opening the task's chat over it would cut the conversation off.
    if (out.taskId != null && !inVoicePane()) armAutoOpen(out.taskId);
    again(); loadOverview();
    if (r.status === 'queued' && r.task) followUp(r.task.id);
    // A voice chat is answered by voice - whatever the "speak replies" habit of typed requests is.
    // A queued task's "handing this to the X agent" line isn't said or written: the pane's loader stands in for it.
    if (r.spoken && r.status !== 'queued') { say('octo', r.spoken); await voice.speak(r.spoken); }
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
    // Token usage. A breakdown row clicked again (its filter already on) takes the filter off.
    usageSet: el => {
      const { k, v } = el.dataset;
      if (!FILTERS.includes(k)) { setOcto({ usage: { ...o().usage, [k]: v, open: null, limit: 50 } }); return; }
      setUsage({ [k]: k !== 'period' && String(o().usage[k]) === v ? '' : v });
      requestRender();
    },
    // On a touch screen the first tap shows that day's numbers (no hover there), a second tap on the same bar opens the day.
    usageDay: el => {
      if (window.matchMedia('(hover: none)').matches && !el.classList.contains('is-tip')) {
        for (const x of document.querySelectorAll('.tk-col.is-tip')) x.classList.remove('is-tip');
        el.classList.add('is-tip');
        return;
      }
      setUsage({ period: 'custom', start: el.dataset.v, end: el.dataset.v }); requestRender(); },
    usageClear: () => { setUsage({ project: '', model: '', lane: '', account: '' }); requestRender(); },
    usageOpen: el => setOcto({ usage: { ...o().usage, open: o().usage.open === el.dataset.v ? null : el.dataset.v } }),
    usageMore: () => setOcto({ usage: { ...o().usage, limit: o().usage.limit + 50 } }),
    usageCsv: () => {
      const t = store.data.octo.tokens;
      if (!t || !t.items) return;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([usageCsv(t)], { type: 'text/csv' }));
      a.download = `token-usage-${t.start}-to-${t.end}.csv`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    },
    toggleModel: el => {
      const id = el.dataset.id;
      const m = ((store.data.octo.providers || {}).providers || []).flatMap(g => g.models).find(x => x.id === id);
      octoApi.setModel(id, !(m && m.enabled !== false)).then(() => { loadProviders(); again(); }, fail);
    },
    // ---- AI Providers › Add / edit a model
    mfNew: () => setOcto({ mf: blankModelForm() }),
    mfEdit: el => {
      const m = allModels().find(x => x.id === el.dataset.id);
      if (!m) return;
      const p = m.price_per_mtok || {};
      setOcto({ mf: { ...blankModelForm(), id: m.id, label: m.label || '', provider: m.provider, model: m.model || '', modality: m.modality || 'text',
        use_for: m.use_for || [], coding: m.coding ?? 3, reasoning: m.reasoning ?? 3, cost: m.cost ?? 3, speed: m.speed ?? 3,
        vision: !!m.vision, web_search: !!m.web_search, context_tokens: m.context_tokens ? String(m.context_tokens) : '',
        price_in: p.input ?? '', price_out: p.output ?? '', price_image: m.price_per_image ?? '', note: m._note || '' } });
    },
    mfCancel: () => setOcto({ mf: null }),
    mfSet: el => {
      const f = o().mf, k = el.dataset.k, v = el.dataset.v;
      const next = { ...f, [k]: ['coding', 'reasoning', 'cost', 'speed'].includes(k) ? Number(v) : v, error: '' };
      if (k === 'provider' || k === 'modality') {
        if (next.provider === 'claude_code') next.modality = 'text';
        const prov = ((store.data.octo.providers || {}).add || { providers: [] }).providers.find(p => p.id === next.provider);
        const ok = ((prov && prov.categories) || {})[next.modality] || [];
        next.use_for = next.use_for.filter(c => ok.includes(c));
        if (!next.use_for.length) next.use_for = next.modality === 'image' ? ['image'] : [];
      }
      setOcto({ mf: next });
    },
    mfCat: el => {
      const f = o().mf, v = el.dataset.v;
      setOcto({ mf: { ...f, use_for: f.use_for.includes(v) ? f.use_for.filter(x => x !== v) : [...f.use_for, v], error: '' } });
    },
    mfFlag: el => { const f = o().mf; setOcto({ mf: { ...f, [el.dataset.k]: !f[el.dataset.k], error: '' } }); },
    mfSave: async () => {
      const f = o().mf;
      const num = (v, what) => { const t = String(v ?? '').trim(); if (!t) return null; const n = Number(t); if (!Number.isFinite(n) || n < 0) throw new Error(what + ' must be a number'); return n; };
      let body;
      try {
        if (!f.label.trim()) throw new Error('Give the model a name');
        if (!f.model.trim() && f.provider !== 'claude_code') throw new Error('Enter the model name as the provider knows it');
        const ctx = num(f.context_tokens, 'Context size');
        body = { label: f.label.trim(), provider: f.provider, model: f.model.trim() || null, modality: f.modality, use_for: f.use_for,
          coding: f.coding, reasoning: f.reasoning, cost: f.cost, speed: f.speed, vision: f.vision, web_search: f.web_search,
          context_tokens: ctx == null ? null : Math.round(ctx),
          price_per_mtok: { input: num(f.price_in, 'Input price'), output: num(f.price_out, 'Output price') },
          price_per_image: num(f.price_image, 'Price per picture'), note: f.note.trim() };
      } catch (e) { return setOcto({ mf: { ...f, error: e.message } }); }
      setOcto({ mf: { ...f, busy: true, error: '' } });
      try {
        const m = f.id ? await octoApi.editModel(f.id, body) : await octoApi.addModel(body);
        setOcto({ mf: null });
        toast(f.id ? `${m.label} saved` : `${m.label} added${m.available ? '' : ' - ' + (m.unavailable_reason || 'not usable yet')}`);
        loadProviders(); again();
      } catch (e) { setOcto({ mf: { ...o().mf, busy: false, error: e.message } }); }
    },
    mfRemove: el => {
      const m = allModels().find(x => x.id === el.dataset.id);
      if (!confirm(`Remove ${m ? m.label : 'this model'}? Octopus stops routing to it. Chats that used it keep their history.`)) return;
      octoApi.removeModel(el.dataset.id).then(() => {
        if (o().mf && o().mf.id === el.dataset.id) setOcto({ mf: null });
        toast('Model removed'); loadProviders(); again();
      }, fail);
    },
    // ---- API keys (AI Providers page)
    keyAdd: async el => {
      const pid = el.dataset.provider, f = { label: '', key: '', ...(o().keys || {})[pid] };
      const put = patch => setOcto({ keys: { ...o().keys, [pid]: { ...f, ...patch } } });
      if (!f.label.trim() || !f.key.trim()) return put({ error: 'Give it a name and paste the key' });
      put({ busy: true, error: '' });
      try {
        putOcto({ providers: await octoApi.addApiKey({ provider: pid, label: f.label, key: f.key }) });
        setOcto({ keys: { ...o().keys, [pid]: { label: '', key: '', busy: false, error: '' } } });
        toast('Key added'); again();
      } catch (e) { put({ busy: false, error: e.message }); }
    },
    keyDefault: el => octoApi.defaultApiKey(el.dataset.id).then(pv => { putOcto({ providers: pv }); toast('Default key changed'); }, fail),
    keyRemove: el => {
      if (!confirm(`Remove the key "${el.dataset.name}"? It's deleted from this Mac; Octopus stops using it.`)) return;
      octoApi.removeApiKey(el.dataset.id).then(pv => { putOcto({ providers: pv }); toast('Key removed'); again(); }, fail);
    },
    // ---- settings
    setAutonomy: el => saveSetting({ autonomy: el.dataset.v }, 'Autonomy · ' + el.dataset.v),
    setPlanReview: el => saveSetting({ plan_review: el.dataset.v }),
    octoPolicy: el => octoApi.setPolicy(el.dataset.cap, el.dataset.policy).then(() => loadPolicies(), e => { fail(e); loadPolicies(); }),
    voiceProvider: el => { if (el.getAttribute('aria-disabled') === 'true') return toast('That provider isn\'t available yet - see the note under it'); saveSetting({ [el.dataset.key]: el.dataset.id }); },
    voiceTest: () => { voice.unlockAudio(); voice.speak('Hello Tushar. This is how I sound.'); },
    voiceRate: async el => { await saveSetting({ voice_rate: Number(el.dataset.v) }); voice.unlockAudio(); voice.speak('Hello Tushar. This is how fast I speak.'); },
    voiceLangTest: el => { voice.unlockAudio(); voice.speak(languageSample(el.dataset.lang)); },
    // ---- Alexa (Voice page)
    alexaPref: el => octoApi.alexaPrefs({ [el.dataset.key]: el.getAttribute('aria-checked') !== 'true' }).then(a => putOcto({ alexa: a }), fail),
    alexaPin: el => {
      const input = el.parentElement.querySelector('input');
      octoApi.alexaPrefs({ approval_pin: input.value.trim() }).then(a => {
        input.value = '';
        putOcto({ alexa: a });
        toast(a.prefs.approval_pin_set ? 'Approval code saved' : 'Approval code removed');
      }, fail);
    },
    alexaTest: () => octoApi.alexaTest().then(() => { toast('Sent - Alexa should light up in a moment'); loadAlexa(); }, e => { fail(e); loadAlexa(); }),
    alexaUnlink: () => {
      if (!confirm('Forget the Amazon account paired with Octopus? The next account to open the skill gets paired instead.')) return;
      octoApi.alexaUnlink().then(a => putOcto({ alexa: a }), fail);
    },
    voicePick: async el => {
      const name = el.dataset.id;   // "" = Automatic: each language goes back to its own default voice
      // The menu stays open so several voices can be tried in a row; the scrim closes it.
      if (voice.ttsMode() === 'macos') await saveSetting({ voice_manual: name });
      else { voice.setBrowserManualVoice(name); requestRender(); }
      if (!name) return;
      voice.unlockAudio();
      voice.speak('Hello Tushar. This is how I sound.');
    },
    // ---- Claude accounts
    openAccount: el => set({ sheet: 'account', octo: { ...o(), acctView: el.dataset.id } }),
    acctSwitch: el => account('switch', () => octoApi.useAccount(el.dataset.id), r => {
      const a = (r.claude_accounts || []).find(x => x.active);
      toast('Default account: ' + (a ? (a.alias || (a.kind === 'mac' ? (a.email || "this Mac's login") : a.label)) : 'that account')
        + (r.resumed ? ` · ${r.resumed} paused task${r.resumed === 1 ? '' : 's'} retrying` : ''));
      // Its limits are read in the background - pick them up once they're in.
      setTimeout(() => octoApi.settings().then(v => { store.data = { ...store.data, settings: v }; requestRender(); }, () => {}), 12000);
    }),
    acctColor: el => account('color', () => octoApi.colorAccount(el.dataset.id, el.dataset.v), () => toast('Color saved')),
    // One sheet with every field, instead of a chain of browser prompts.
    acctEdit: el => {
      const a = ((settings().claude_accounts) || []).find(x => x.id === el.dataset.id);
      if (!a) return;
      set({ sheet: 'accountEdit', octo: { ...o(), acctEdit: { id: a.id, mac: a.kind === 'mac', name: a.label || '', alias: a.alias || '', email: a.email || '', error: '' } } });
    },
    acctEditSave: () => {
      const e = o().acctEdit;
      if (!e) return;
      if (!e.mac && !e.name.trim()) return setOcto({ acctEdit: { ...e, error: 'An account needs a name' } });
      const body = { alias: e.alias.trim() };
      if (!e.mac) { body.name = e.name.trim(); body.email = e.email.trim(); }
      account('edit', () => octoApi.editAccount(e.id, body), () => { set({ sheet: null }); toast('Details saved'); });
    },
    acctRefresh: el => account('refresh', () => octoApi.checkAccount(el.dataset.id), () => toast('Limits updated')),
    acctRemove: el => {
      const a = ((settings().claude_accounts) || []).find(x => x.id === el.dataset.id);
      const moving = a && a.projects && a.projects.length ? ` ${a.projects.map(p => p.name).join(', ')} will use the default account instead.` : '';
      const mac = a && a.kind === 'mac';
      if (!confirm(mac ? `Remove this Mac's login from the list? Claude Code stops using it - you can bring it back under Add an account.${moving}`
        : `Remove ${a ? (a.alias || a.label) : 'this account'}? Its saved token is deleted from this Mac.${moving}`)) return;
      account('remove', () => octoApi.removeAccount(el.dataset.id), () => toast('Account removed'));
    },
    acctShowMac: () => account('show', () => octoApi.showMacAccount(), () => toast("This Mac's login is back")),
    acctAdd: () => {
      const f = o().acct;
      if (!f.label.trim() || !f.token.trim()) return setOcto({ acct: { ...f, error: 'Give it a name and paste the token' } });
      account('add', () => octoApi.addAccount({ label: f.label, email: f.email || null, token: f.token }).then(() => octoApi.settings()), () => {
        setOcto({ acct: { label: '', email: '', token: '', busy: '', error: '' } });
        toast('Account added - switch to it whenever you like');
      });
    },
    // ---- voice
    voiceMic: () => mic(),
    voiceStop: () => endConversation(),
    voiceLang: el => voice.setListenLanguage(el.dataset.v),
    // ---- task page
    openTaskDetail: el => { const id = Number(el.dataset.id); set({ sheet: 'taskDetail', detailTask: id }); loadDetail(latestInChat(id)); },
    taskDiff: el => loadDiff(Number(el.dataset.id)),
    // A file the task made (a PDF, a picture…) opens in the same preview sheet as project documents.
    taskFile: el => {
      const path = el.dataset.path || '';
      openPreview(set, { src: `/tasks/${Number(el.dataset.id)}/file?path=${encodeURIComponent(path)}`, name: path.split('/').pop() });
    },
  };
}

// The input handler's branch for data-input="octo.*" fields.
const languageSample = code => ((store.data.octo.voice || {}).languages || []).find(l => l.code === code)?.sample || 'Hello Tushar. This is how I sound.';

export function octoInput(path, value, h) {
  const patchSettings = patch => {
    store.data = { ...store.data, settings: { ...(store.data.settings || {}), ...patch } };
    return octoApi.setSettings(patch).catch(h.fail);
  };
  if (path.startsWith('octo.mf.') && store.ui.octo.mf) {
    store.ui.octo.mf = { ...store.ui.octo.mf, [path.slice('octo.mf.'.length)]: value, error: '' };
    return;
  }
  if (path.startsWith('octo.keys.')) {
    const [pid, k] = path.slice('octo.keys.'.length).split('.');
    const keys = store.ui.octo.keys || {};
    store.ui.octo.keys = { ...keys, [pid]: { label: '', key: '', busy: false, ...keys[pid], [k]: value, error: '' } };
    return;
  }
  if (path.startsWith('octo.usage.')) {
    const k = path.slice('octo.usage.'.length);
    if (k === 'month') { if (value) setUsage({ period: value }); return; }
    if ((k === 'start' || k === 'end') && !value) return;      // half-cleared date box: keep the old day
    setUsage({ [k]: value });
    return;
  }
  if (path.startsWith('octo.acctEdit.') && store.ui.octo.acctEdit) {
    store.ui.octo.acctEdit = { ...store.ui.octo.acctEdit, [path.slice('octo.acctEdit.'.length)]: value, error: '' };
    return;
  }
  if (path.startsWith('octo.acct.')) {
    const k = path.slice('octo.acct.'.length);
    store.ui.octo.acct = { ...store.ui.octo.acct, [k]: value, error: '' };
    return;
  }
  if (path === 'octo.voice') {
    // The default voice: the Mac's is a server setting, a browser's stays on this device.
    if (voice.ttsMode() === 'macos') patchSettings({ voice_tts_voice: value });
    else { voice.setBrowserVoiceName(value); store.set({}); }
  } else if (path.startsWith('octo.langvoice.')) {
    // The voice for one language; picking it plays that language's sample so the choice can be heard at once.
    const code = path.slice('octo.langvoice.'.length);
    let saved = Promise.resolve();
    if (voice.ttsMode() === 'macos') {
      const map = { ...(store.data.settings || {}).voice_language_voices };
      value ? map[code] = value : delete map[code];
      saved = patchSettings({ voice_language_voices: map });
    } else { voice.setBrowserLanguageVoice(code, value); store.set({}); }
    saved.then(() => { voice.unlockAudio(); voice.speak(languageSample(code)); });
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
