// Recipes & schedules: saved prompts you can run in two taps or put on a schedule.

import { esc, clock, emptyState } from '../ui.js';
import { projectById } from '../data.js';
import { scheduleRow, cadenceLabel } from './home.js';

export function recipeTarget(r) {
  if (r.target === 'all') return 'All active projects';
  if (/^\d+$/.test(r.target)) { const p = projectById(Number(r.target)); return p ? p.name : 'A removed project'; }
  return 'Any project';
}

// `here`: the project whose Recipes tab this is (cards say "Run here" instead of "Run now").
export function recipeCard(c, r, { here = null } = {}) {
  const sc = (c.d.schedules || []).find(s => s.enabled && s.prompt === r.prompt);
  const busy = c.ui.busy['rc' + r.id];
  return `<div class="recipe" data-key="rc${r.id}">
    <div class="recipe__head"><span class="recipe__name">${esc(r.name)}</span>${sc && !here ? `<span class="recipe__sched">${esc(cadenceLabel(sc))} ${esc(clock(sc.next_run_at || new Date().toISOString()))}</span>` : ''}</div>
    <span class="recipe__prompt">${esc(r.prompt)}</span>
    ${here ? '' : `<span class="meta">${esc(recipeTarget(r))}</span>`}
    <div class="recipe__acts">
      <button class="btn btn--primary btn--36" data-action="runRecipe" data-id="${r.id}"${here ? ` data-pid="${here.id}"` : ''}${busy ? ' disabled' : ''}>${here ? 'Run here' : 'Run now'}</button>
      <button class="btn btn--ghost btn--36" data-action="scheduleRecipe" data-id="${r.id}"${here ? ` data-pid="${here.id}"` : ''}>Schedule</button>
    </div>
  </div>`;
}

export function recipes(c) {
  const scheds = c.d.schedules || [];
  const list = c.d.recipes || [];
  return `<div class="page page--wide page--recipes" data-key="page-recipes">
    <span class="page__intro">Recipes are saved prompts. Run one in two taps from +, or put it on a schedule. If the Mac is asleep when a schedule is due, it runs as soon as it wakes.</span>
    <div class="section"><div class="section__head"><span class="section__title">Schedules</span></div>
      ${scheds.length ? `<div class="list list--720">${scheds.map(s => scheduleRow(s)).join('')}</div>`
        : emptyState({ title: 'No schedules yet', body: 'Run a task automatically at a set time. If the Mac is asleep, it runs when it wakes.', label: 'Schedule a task', attrs: 'data-action="openPalette" data-when="daily"', cls: 'estate--720' })}
    </div>
    <div class="section"><div class="section__head"><span class="section__title">Recipes</span></div>
      ${list.length ? `<div class="recipe-grid">${list.map(r => recipeCard(c, r)).join('')}</div>` : emptyState({ title: 'No recipes yet', body: 'Recipes are saved prompts you can run in two taps from the + menu or put on a schedule.', label: 'Add your first task', attrs: 'data-action="openPalette"' })}
    </div>
  </div>`;
}
