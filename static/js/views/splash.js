// Splash / welcome (every load, dismissed only by a double tap or double click) and
// the API-key gate shown when this browser has no key yet.

import { greeting } from '../format.js';
import { octopusSVG } from '../octopus.js';
import { threadSummaries, realProjects } from '../data.js';
import { esc, octo } from '../ui.js';

const LAST_SEEN_KEY = 'octopus-last-seen';
export function markSeen() { try { localStorage.setItem(LAST_SEEN_KEY, new Date().toISOString()); } catch {} }
function lastSeen() {
  try { const v = localStorage.getItem(LAST_SEEN_KEY); if (v) return new Date(v).getTime(); } catch {}
  return Date.now() - 14 * 3600 * 1000;   // first visit: "while you were away" = the last 14 hours
}

export function splash(c) {
  const { d, ui, w, desk } = c;
  const since = lastSeen();
  const done = d.loaded ? threadSummaries(d).filter(t => t.status === 'done' && new Date(t.finished_at || t.created_at).getTime() > since).length : 0;
  const g = greeting({ date: ui.bootDate, rand: ui.bootRand, doneRecently: done, needsYou: w.needs.length, firstRun: d.loaded && realProjects(d).length === 0 });
  return `<div class="splash${ui.splashLeaving ? ' is-leaving' : ''}" data-key="splash" data-splash>
    <div class="splash__octo">${octopusSVG('dance')}</div>
    <div class="splash__copy">
      <span class="splash__hello">${esc(g.hello)} <b>${esc(g.name)}</b></span>
      <span class="splash__line">${esc(g.line)}</span>
      ${d.loaded ? `<span class="splash__recap${g.recapHasNeeds ? ' has-needs' : ''}">${esc(g.recap)}</span>` : ''}
      <span class="splash__status">stretching tentacles…</span>
    </div>
    <div class="splash__bar"></div>
    <span class="splash__hint"><span>${desk ? 'Double-click' : 'Double-tap'} anywhere to dive in</span></span>
  </div>`;
}

export function gate(c) {
  return `<div class="gate" data-key="gate">
    <div class="gate__card">
      ${octo('sleep', 96)}
      <h1 class="gate__title">Workspace is locked</h1>
      <p class="gate__text">Enter the API key from the service's launchd plist (<code>API_KEY</code>). This browser remembers it.</p>
      <form class="gate__form" data-submit="saveKey">
        <input id="keyInput" class="input input--mono" type="password" placeholder="X-API-Key" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="API key">
        <button class="btn btn--primary" type="submit">Unlock</button>
      </form>
      ${c.ui.gateError ? `<p class="gate__err">${esc(c.ui.gateError)}</p>` : ''}
    </div>
  </div>`;
}
