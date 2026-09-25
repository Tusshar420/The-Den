// The approval card for personal-assistant actions (EMAIL APPROVAL, MEETING APPROVAL...).
// Built entirely from the preview the server stored with the approval, so a new tool gets
// a correct card without any change here: a title, labelled fields, warnings, two buttons.
// What's on the card is exactly what runs when Approve is pressed.

import { esc, spinner } from '../ui.js';

export const isAssistant = a => !!a && typeof a.tool_name === 'string' && a.tool_name.startsWith('assistant:');

export function previewOf(a) {
  try {
    const p = JSON.parse(a.tool_input);
    return { ...(p.preview || {}), describe: p.describe || '', redacted: !!p.redacted };
  } catch { return null; }
}

// "this email", "this meeting invite" - for sentences like "needs a yes before I act on ___".
const KIND_WORDS = { email: 'this email', meeting: 'this meeting invite', event: 'this event', calendar_change: 'this calendar change',
  calendar_cancel: 'this cancellation', agent: 'this coding task' };
export const kindWord = a => KIND_WORDS[(previewOf(a) || {}).kind] || 'this action';

const field = f => (f.block
  ? `<div class="pa-field pa-field--block"><span class="pa-field__k">${esc(f.label)}</span><div class="pa-field__msg">${esc(f.value)}</div></div>`
  : `<div class="pa-field"><span class="pa-field__k">${esc(f.label)}</span><span class="pa-field__v">${esc(f.value)}</span></div>`);

export function assistantCard(c, a) {
  const pv = previewOf(a);
  if (!pv) return '';
  const busy = c.ui.busy['ap' + a.id];
  return `<div class="pa-card" data-key="pa-${a.id}">
    <span class="pa-card__title">${esc(pv.title || 'APPROVAL')}</span>
    <div class="pa-card__fields">${(pv.fields || []).map(field).join('') || `<div class="pa-field"><span class="pa-field__v">${esc(pv.describe)}</span></div>`}</div>
    ${(pv.warnings || []).map(w => `<div class="pa-warn"><span>!</span>${esc(w)}</div>`).join('')}
    <div class="pa-card__acts">
      <button class="btn btn--ghost btn--46" data-action="deny" data-id="${a.id}"${busy ? ' disabled' : ''}>${esc(pv.deny || 'Cancel')}</button>
      <button class="btn btn--approve btn--46 grow" data-action="approve" data-id="${a.id}"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Working…' : esc(pv.approve || 'Approve')}</button>
    </div>
  </div>`;
}
