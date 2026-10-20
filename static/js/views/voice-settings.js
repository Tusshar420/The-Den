// The Voice page: how Octopus listens and speaks, and which voice reads which language. Only draws what the
// server reports (GET /voice) plus this device's own voices; the actions live in octopus-ui.js.

import { esc } from '../ui.js';
import { ago } from '../format.js';
import { ttsMode, voiceChoices, voicesForLanguage, languageVoices } from '../voice.js';

const SPEEDS = [[0.8, 'Slow'], [1, 'Normal'], [1.25, 'Fast'], [1.5, 'Faster']];
const MANAGE_MAC_VOICES = 'System Settings › Accessibility › Spoken Content › System Voice › Manage Voices';

const provider = (v, s, kind, key) => `<div class="set-provs">${v[kind].map(p => `<button class="set-prov${(s[key] || v.settings[key]) === p.id ? ' is-on' : ''}${p.available ? '' : ' is-off'}" data-action="voiceProvider" data-key="${key}" data-id="${p.id}"${p.available ? '' : ' aria-disabled="true"'}>
    <b>${esc(p.label)}</b><span>${esc(p.available ? p.description : p.reason)}</span></button>`).join('')}</div>`;

const option = (x, on) => `<option value="${esc(x.name)}"${on ? ' selected' : ''}>${esc(x.name)} (${esc(x.language)})</option>`;

function speaking(v, s) {
  const mac = ttsMode() === 'macos', voices = voiceChoices();
  const speed = Number(s.voice_rate) || 1;
  return `<div class="set-group"><span class="set-group__t">Speaking (text to speech)</span>${provider(v, s, 'tts', 'voice_tts')}
    <label class="set-voice"><span class="meta">Default voice${mac ? ' (on the Mac)' : ' (on this device)'} - used for any language you haven't chosen a voice for below</span>
      <select class="input" data-input="octo.voice"><option value="">${mac ? 'System default' : 'Let this device choose'}</option>${voices.map(x => option(x, x.on)).join('')}</select></label>
    <div class="set-line"><span>Speed</span><div class="segmented set-seg" data-noswipe>${SPEEDS.map(([n, l]) => `<button class="segmented__opt${speed === n ? ' is-on' : ''}" data-action="voiceRate" data-v="${n}">${l}</button>`).join('')}</div></div>
    <div class="set-line"><span class="meta">Voice feeds the same pipeline as typing - same plan, same approvals.</span><button class="btn btn--sm" data-action="voiceTest">Test voice</button></div>
  </div>`;
}

function languageRow(lang) {
  const mac = ttsMode() === 'macos', voices = voicesForLanguage(lang.code), chosen = languageVoices()[lang.code] || '';
  const none = mac
    ? `No installed voice speaks ${esc(lang.label)}. Add one in ${MANAGE_MAC_VOICES}.`
    : `This device has no ${esc(lang.label)} voice.`;
  return `<div class="set-cap vlang" data-key="vl-${lang.code}">
    <div class="set-cap__txt"><span class="set-cap__name">${esc(lang.label)}</span>
      <span class="meta">${voices.length ? `${voices.length} voice${voices.length === 1 ? '' : 's'} available` : none}</span></div>
    <select class="input vlang__pick" data-input="octo.langvoice.${lang.code}" aria-label="Voice for ${esc(lang.label)}"${voices.length ? '' : ' disabled'}>
      <option value="">Automatic</option>${voices.map(x => option(x, x.name === chosen)).join('')}</select>
    <button class="btn btn--sm" data-action="voiceLangTest" data-lang="${lang.code}" aria-label="Play ${esc(lang.label)} sample">Play</button>
  </div>`;
}

function perLanguage(v) {
  const mac = ttsMode() === 'macos';
  return `<div class="set-group"><span class="set-group__t">Voice for each language</span>
    <span class="meta prov-note">Octopus reads each answer in the voice you pick for the language it is written in. Automatic uses the default voice when it speaks that language${mac ? '' : ', otherwise lets this device pick one that does'}.
      ${mac ? 'These choices are for the Mac\'s voices and apply everywhere.' : 'Browser voices differ on every device, so these choices are kept on this device only.'}
      Answers in romanised Hindi ("kya haal hai") are read as English.</span>
    ${(v.languages || []).length ? `<div class="list">${v.languages.map(languageRow).join('')}</div>` : '<div class="empty">Restart Octopus to load the language list.</div>'}
  </div>`;
}

// Alexa (alexa/): is the skill connected, can it notify you, and what reaches it while you're away.
const toggle = (on, key, label) => `<button class="toggle" role="switch" aria-checked="${!!on}" data-action="alexaPref" data-key="${key}" aria-label="${esc(label)}"></button>`;

function alexaSection(a) {
  if (!a) return '<div class="set-group"><span class="meta">Loading… (if this stays, restart Octopus to load the Alexa code)</span></div>';
  const skill = !a.skill_id_set ? 'Not set up yet - add ALEXA_SKILL_ID to the service (README › Alexa)'
    : a.paired ? `Connected${a.last_request_at ? ' · last used ' + ago(a.last_request_at) : ''}`
    : 'Waiting for you to say "Alexa, open my octopus" once';
  const notes = a.notifications_configured ? `Ready${a.subscribed ? ' · allowed in the Alexa app' : ''}` : a.notifications_reason;
  const last = a.last_notify ? (a.last_notify.ok ? `Last sent ${ago(a.last_notify.at)}` : `Last attempt failed: ${a.last_notify.error}`) : '';
  const here = a.presence.present ? `You're using The Den now${a.presence.device ? ' (' + a.presence.device + ')' : ''} - nothing goes to Alexa` : 'Away from The Den - approvals go to Alexa';
  return `<div class="set-group"><span class="set-group__t">Connection</span>
      <div class="set-line"><span>Skill</span><span class="meta">${esc(skill)}</span></div>
      <div class="set-line"><span>Notifications</span><span class="meta">${esc(notes)}${last ? ' · ' + esc(last) : ''}</span></div>
      <div class="set-line"><span>Right now</span><span class="meta">${esc(here)}</span></div>
    </div>
    <div class="set-group"><span class="set-group__t">When you're away from The Den</span>
      <div class="set-line"><span>Tell Alexa when an approval is waiting</span>${toggle(a.prefs.notify_approvals, 'notify_approvals', 'Approvals to Alexa')}</div>
      <div class="set-line"><span>Tell Alexa when a task you started from Alexa finishes</span>${toggle(a.prefs.notify_results, 'notify_results', 'Finished tasks to Alexa')}</div>
      <span class="meta prov-note">Alexa lights up and says you have messages from Octopus; say "Alexa, open my octopus" to hear them and answer. Only a count goes to Amazon's notification service - the details are read out by the skill.</span>
    </div>
    <div class="set-group"><span class="set-group__t">Approval code</span>
      <span class="meta prov-note">Anyone near the speaker can say "approve". Set a 4 to 8 digit code and approving by voice needs it too. ${a.prefs.approval_pin_set ? 'A code is set.' : 'No code set.'}</span>
      <div class="set-line"><input class="input" data-key="alexa-pin" type="password" inputmode="numeric" autocomplete="off" placeholder="${a.prefs.approval_pin_set ? 'New code (empty = remove)' : 'e.g. 4321'}" aria-label="Approval code">
        <button class="btn btn--sm" data-action="alexaPin">Save code</button></div>
    </div>
    <div class="set-line"><button class="btn btn--sm" data-action="alexaTest"${a.notifications_configured ? '' : ' disabled'}>Send a test notification</button>
      ${a.paired ? '<button class="btn btn--sm" data-action="alexaUnlink">Forget the paired Amazon account</button>' : ''}</div>`;
}

export function voiceSettings(c) {
  const v = c.d.octo.voice, s = c.d.settings || {};
  if (!v) return '<div class="page page--narrow page--settings" data-key="page-voice"><div class="empty">Loading…</div></div>';
  return `<div class="page page--narrow page--settings" data-key="page-voice">
    <section class="section" data-key="vs-listen"><div class="section__head"><span class="section__title">Listening</span></div>
      <div class="set-group"><span class="set-group__t">Speech to text</span>${provider(v, s, 'stt', 'voice_stt')}</div></section>
    <section class="section" data-key="vs-speak"><div class="section__head"><span class="section__title">Speaking</span></div>${speaking(v, s)}</section>
    <section class="section" data-key="vs-langs"><div class="section__head"><span class="section__title">Languages</span></div>${perLanguage(v)}</section>
    <section class="section" data-key="vs-alexa"><div class="section__head"><span class="section__title">Alexa</span></div>${alexaSection(c.d.octo.alexa)}</section>
  </div>`;
}
