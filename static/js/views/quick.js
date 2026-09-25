// Quick buttons: one-tap starters for everyday jobs (a Sanskrit exam paper to typed text, a summary,
// a reply...). A button fills the Ask box with its prompt - and opens the picture picker if it needs
// a photo - so sending goes through the normal Ask flow. The list lives in the server's
// `quick_buttons` setting, so it is the same on the phone.

import { esc, spinner, toggle } from '../ui.js';

export const quickButtons = c => (c.d.settings && c.d.settings.quick_buttons) || [];

const button = (b, editing) => `<button class="qb${editing ? ' is-editing' : ''}" data-action="${editing ? 'quickRemove' : 'quickUse'}" data-id="${esc(b.id)}"
    title="${esc(editing ? 'Remove ' + b.label : b.prompt)}" aria-label="${esc(editing ? 'Remove ' + b.label : b.label)}" data-key="qb-${esc(b.id)}">
    <span class="qb__icon" aria-hidden="true">${esc(b.icon || '✨')}</span><span class="qb__label truncate">${esc(b.label)}</span>
    ${b.pic && !editing ? '<span class="qb__pic" title="Asks for a picture" aria-hidden="true">📎</span>' : ''}${editing ? '<span class="qb__x" aria-hidden="true">×</span>' : ''}
  </button>`;

export function quickSection(c) {
  const list = quickButtons(c);
  const editing = c.ui.quickEdit;
  const right = `<span class="qb-tools">${list.length ? `<button class="btn--text" data-action="quickEditToggle">${editing ? 'Done' : 'Edit'}</button>` : ''}<button class="btn--text" data-action="quickNew">+ Add</button></span>`;
  return `<section class="section reveal" style="--i:60" data-key="quick">
    <div class="section__head"><span class="section__title">Quick buttons</span>${right}</div>
    ${list.length ? `<div class="qb-grid" role="group" aria-label="Quick buttons">${list.map(b => button(b, editing)).join('')}</div>`
      : `<div class="empty"><span>No quick buttons yet.</span><button class="btn btn--sm" data-action="quickNew">Add one</button></div>`}
  </section>`;
}

// The Add sheet. `wrap` is sheets.js's own frame, passed in so this file needn't import it back.
export function quickSheet(c, wrap) {
  const q = c.ui.quickNew || { icon: '', label: '', prompt: '', pic: false, saving: false, error: '' };
  const ready = q.label.trim() && q.prompt.trim();
  return wrap('quick', `<div class="sheet__body sheet__body--form">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">New quick button</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <span class="pal-label">NAME</span>
    <div class="qb-name"><input id="quickIcon" class="input qb-name__icon" maxlength="8" placeholder="✨" data-input="quickNew.icon" aria-label="Emoji" value="${esc(q.icon)}">
      <input id="quickLabel" class="input" maxlength="40" placeholder="e.g. Sanskrit exam paper" data-input="quickNew.label" aria-label="Button name" value="${esc(q.label)}"></div>
    <span class="pal-label">WHAT IT ASKS OCTOPUS</span>
    <textarea id="quickPrompt" class="input edit-prompt" rows="6" maxlength="2000" data-input="quickNew.prompt" data-autogrow="300" aria-label="Prompt"
      placeholder="Convert the attached photo into typed text…">${esc(q.prompt)}</textarea>
    <div class="qb-pic"><span>Needs a picture<span class="meta">Opens the picture picker when tapped</span></span>${toggle(!!q.pic, 'quickNewPic', 'aria-label="Needs a picture"')}</div>
    ${q.error ? `<span class="form-err">${esc(q.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="quickSave"${q.saving || !ready ? ' disabled' : ''}>${q.saving ? spinner() + 'Saving…' : 'Add button'}</button>
    </div>
  </div>`, 'New quick button');
}
