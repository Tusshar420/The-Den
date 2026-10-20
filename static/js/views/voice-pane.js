// Voice chat, in the side pane: what you said and what Octopus answered as chat bubbles (its answers type
// themselves out while they are spoken), and a mic that moves while you talk. The talking itself is
// octopus-ui.js; this only draws the state it keeps in store.ui.octo.voice.

import { esc, octo } from '../ui.js';
import { micAvailable, listenChoices } from '../voice.js';
import { paneClose } from './chat.js';

const MIC = '<svg width="26" height="26" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7.2" y="2.5" width="5.6" height="10" rx="2.8"/><path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5"/></svg>';

const LABEL = { listening: 'Listening…', working: 'Thinking…', speaking: 'Speaking…' };

function line(e) {
  if (e.who === 'you') return `<div class="msg-you" data-key="v${e.id}"><div class="msg-you__bubble">${esc(e.text)}</div></div>`;
  const typing = e.typed < e.text.length;
  return `<div class="msg-octo" data-key="v${e.id}">${octo('idle', 30)}<div class="msg-octo__col">
    <div class="msg-octo__bubble">${esc(e.text.slice(0, e.typed))}${typing ? '<span class="vcaret"></span>' : ''}</div></div></div>`;
}

export function voicePane(c) {
  const v = c.ui.octo.voice;
  const state = v.state, on = v.conversation;
  const log = v.log || [];
  // What is being said right now, before it is final.
  const draft = state === 'listening' ? v.text : state === 'working' ? v.heard : '';
  const status = LABEL[state] || (on ? 'Getting ready…' : 'Tap the mic to talk');
  const label = on ? 'End voice chat' : 'Talk to Octopus';
  const langs = listenChoices();
  const close = paneClose(c);
  return `<aside class="pane vpane" data-key="pane-voice" aria-label="Voice chat">
    <div class="pane__head">
      ${c.desk ? '' : close}
      <div class="pane__titles"><span class="pane__title">Voice chat</span><span class="pane__sub">${on ? 'Talk freely - I answer out loud and type it here' : 'Not listening'}</span></div>
      ${c.desk ? close : ''}
    </div>
    <div class="chat vpane__log">
      ${log.length || draft ? '' : `<div class="pane-empty">${octo('idle', 64)}<span class="pane-empty__title">Just say what you need</span><span class="pane-empty__sub">I'll answer out loud, and everything we say is written down here.</span></div>`}
      ${log.map(line).join('')}
      ${v.pending ? `<div class="msg-octo" data-key="vpending">${octo('dance', 30)}<div class="msg-octo__col"><div class="msg-octo__bubble vloading" role="status">Working on it<span class="vloading__dots" aria-hidden="true"><i></i><i></i><i></i></span></div></div></div>` : ''}
      ${draft ? `<div class="msg-you msg-you--draft" data-key="vdraft"><div class="msg-you__bubble">${esc(draft)}</div></div>` : ''}
      ${v.error ? `<div class="msg-sys" style="--c:var(--err)">${esc(v.error)}</div>` : ''}
    </div>
    <div class="vpane__stage" data-state="${state}">
      <div class="vmic${on ? ' is-on' : ''}" data-state="${state}">
        <span class="vmic__ring"></span><span class="vmic__ring"></span>
        <button class="vmic__btn" data-action="voiceMic" aria-label="${label}" aria-pressed="${on}" title="${label}"${micAvailable() || on ? '' : ' disabled'}>${MIC}</button>
      </div>
      <div class="vwave" data-state="${state}" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
      <span class="vpane__status">${esc(status)}</span>
      ${langs.length > 1 ? `<div class="segmented set-seg" role="group" aria-label="I'm speaking">${langs.map(l => `<button class="segmented__opt${l.on ? ' is-on' : ''}" data-action="voiceLang" data-v="${esc(l.code)}" aria-pressed="${l.on}">${esc(l.label)}</button>`).join('')}</div>` : ''}
      ${on ? '<button class="btn btn--ghost btn--38" data-action="voiceStop">End chat</button>' : ''}
    </div>
  </aside>`;
}
