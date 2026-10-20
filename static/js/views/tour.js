// First-run tour (register a project, add a task, watch it run) and the "What is this?" help panel.
// The tour shows once per device on the first visit; Settings > General reopens it, and so does the help panel.

import { esc, octo } from '../ui.js';

const SEEN = 'octopus-tour-seen';
export const tourSeen = () => { try { return !!localStorage.getItem(SEEN); } catch { return true; } };
export const markTourSeen = () => { try { localStorage.setItem(SEEN, '1'); } catch {} };

// act: the button on the step. It closes the tour and does what it says (see tourGo in app.js).
const STEPS = [
  { mood: 'dance', title: 'Welcome to your workspace', body: 'This is where work for Claude gets lined up, run while you are away, and checked when you are back. It takes three steps to see it work.' },
  { mood: 'sleep', title: '1. Register a project', body: 'A project is a folder on this Mac that Claude may work in. Add one and it shows up here, so tasks can be sent to it from any device.', act: ['addProject', 'Add a project'] },
  { mood: 'dance', title: '2. Add a task', body: 'Write what you want done in plain words, pick a project, and send it. It goes into a line and runs one at a time. Press N on a keyboard for a quick task.', act: ['newTask', 'New task'] },
  { mood: 'sleep', title: '3. Watch it run', body: 'Home shows what is running now and what is waiting. Tap a task to read its chat live. If Claude needs your OK for something risky, it asks you and waits.', act: ['activity', 'Open Activity'] },
];

export const TOUR_LENGTH = STEPS.length;

function wrapSheet(name, inner, label) {
  return `<div class="scrim" data-action="scrimClose" data-key="sheet-${name}"><div class="sheet sheet--${name}" role="dialog" aria-modal="true" aria-label="${label}">${inner}</div></div>`;
}

export function tourSheet(c) {
  const i = Math.min(Math.max((c.ui.tour || {}).step || 0, 0), STEPS.length - 1);
  const s = STEPS[i], last = i === STEPS.length - 1;
  return wrapSheet('tour', `<div class="sheet__body">
    <div class="sheet__head sheet__head--flush"><span class="meta">Step ${i + 1} of ${STEPS.length}</span><button class="icon-btn icon-btn--44" data-action="tourDone" aria-label="Close tour">✕</button></div>
    <div class="tour__art">${octo(s.mood, 84)}</div>
    <h2 class="tour__title">${esc(s.title)}</h2>
    <p class="tour__text">${esc(s.body)}</p>
    <div class="tour__dots" aria-hidden="true">${STEPS.map((_, k) => `<i class="${k === i ? 'is-on' : ''}"></i>`).join('')}</div>
    <div class="tour__nav">
      ${i ? '<button class="btn btn--ghost" data-action="tourBack">Back</button>' : '<button class="btn btn--ghost" data-action="tourDone">Skip</button>'}
      ${s.act ? `<button class="btn" data-action="tourGo" data-do="${s.act[0]}">${esc(s.act[1])}</button>` : ''}
      ${last ? '<button class="btn btn--primary" data-action="tourDone">Done</button>' : '<button class="btn btn--primary" data-action="tourNext">Next</button>'}
    </div>
    <button class="btn btn--sm btn--ghost tour__help" data-action="helpOpen">What is all this?</button>
  </div>`, 'Guided tour');
}

const TERMS = [
  ['Octopus', 'The assistant you talk to. Tell it what you want, by typing or by voice, and it picks the right helper, shows you a plan when something is risky, and gets it done. Its one worker also runs queued tasks one after another.'],
  ['Project', 'A folder on this Mac that Claude can work in, such as one app or one website.'],
  ['Task', 'One request for Claude, such as "fix the login bug". Tasks wait in a line and run one at a time. Risky steps stop and ask you first.'],
  ['Phase', 'A stage of a bigger piece of work, such as "Design", then "Build", then "Launch". Each phase holds a to-do list that Octopus feeds into the line one item at a time, and it stops and tells you if one fails. Autopilot can start phases on a date, or whenever your Claude usage has room.'],
  ['Recipe', 'A saved task you run again in two taps, or put on a schedule such as every weekday at 18:00.'],
  ['Autonomy', 'How much Octopus may do without asking. Deleting, deploying and sending email always stay behind your approval. Change it in Settings.'],
];

export function helpSheet() {
  return wrapSheet('help', `<div class="sheet__body">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">What is this?</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <p class="tour__text">Your workspace lets you queue work for Claude on your projects and have it run unattended, from this Mac, your phone or anywhere. In plain words:</p>
    <dl class="help__terms">${TERMS.map(([t, d]) => `<div><dt>${esc(t)}</dt><dd>${esc(d)}</dd></div>`).join('')}</dl>
    <button class="btn btn--sm" data-action="tourOpen">Replay the tour</button>
  </div>`, 'What is this?');
}

export function tourSettings() {
  return `<div class="set-line"><span class="meta">A short walkthrough of the first steps, and a plain-words explanation of Octopus, phases and recipes.</span>
    <span class="set-line__btns"><button class="btn btn--sm" data-action="tourOpen">Replay the tour</button><button class="btn btn--sm" data-action="helpOpen">What is this?</button></span></div>`;
}
