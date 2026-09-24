// Approval detail pane: risk read, the command and where it runs, what the task
// did before asking, the project's uncommitted changes, and the decision buttons.

import { agoShort, plural } from '../format.js';
import { projectById, taskById } from '../data.js';
import { commandOf } from '../derive.js';
import { esc, spinner } from '../ui.js';
import { paneHead } from './chat.js';

const tildify = p => (p || '').replace(/^\/Users\/[^/]+/, '~');
const LEVEL_COLOR = { 1: 'var(--ok)', 2: 'var(--wait)', 3: 'var(--err)' };

export function approvalPane(c, approval) {
  const { d, ui } = c;
  const ctx = d.approvalCtx[approval.id];
  const task = taskById(approval.task_id) || { id: approval.task_id, prompt: approval.task_prompt, project_id: approval.project_id, lane: approval.lane, model_id: approval.model_id, status: 'running' };
  const p = projectById(task.project_id);
  const busy = ui.busy['ap' + approval.id];
  const cmd = commandOf(approval);

  let details = `<div class="empty">Reading the command and the project…</div>`;
  if (ctx) {
    const lc = LEVEL_COLOR[ctx.level] || 'var(--wait)';
    const facts = [['cwd', tildify(ctx.cwd) || '—'], ['tool', ctx.tool_name], ['task', `${task.prompt} · step ${ctx.step_no}`]];
    const changes = ctx.is_git
      ? `<div class="ap-section"><span class="ap-section__title">Changes so far${ctx.summary ? ` · ${plural(ctx.summary.files, 'file')} +${ctx.summary.added} −${ctx.summary.deleted}` : ''}</span>
          ${ctx.files.length ? `<div class="ap-diff">
            ${ctx.files.slice(0, 12).map(f => `<div class="ap-file"><span class="truncate">${esc(f.name)}</span><span class="ap-file__n">${f.untracked ? '<span class="meta">new</span>' : `<span style="color:var(--ok)">+${f.added}</span> <span style="color:var(--err)">−${f.deleted}</span>`}</span></div>`).join('')}
            ${ctx.diff.length ? `<div class="diff">${ctx.diff.map(l => `<div class="diff__line${l.kind === 'ctx' ? '' : ' diff__line--' + l.kind}">${esc(l.text)}</div>`).join('')}</div>` : ''}
          </div>` : `<span class="section__empty">No uncommitted changes in the project.</span>`}
        </div>` : '';
    details = `
      <div class="ap-risk">
        <div class="ap-risk__top"><span class="eyebrow eyebrow--meta">RISK</span><span class="meta">waiting ${esc(agoShort(approval.created_at, c.now))}</span></div>
        <div class="ap-risk__level"><span class="ap-risk__word" style="color:${lc}">${esc(ctx.risk)}</span><div class="risk-meter">${[1, 2, 3].map(i => `<i class="${i <= ctx.level ? 'is-on' : ''}" style="${i <= ctx.level ? 'background:' + lc : ''}"></i>`).join('')}</div></div>
        ${ctx.reasons.map(r => `<div class="ap-reason"><span style="color:${r.kind === 'ok' ? 'var(--ok)' : 'var(--wait)'}">●</span>${esc(r.text)}</div>`).join('')}
      </div>
      <div class="ap-cmd"><span class="ap-cmd__line"><span class="cmd__prompt">$</span> ${esc(cmd)}</span>
        ${facts.map(([k, v]) => `<div class="ap-fact"><span>${k}</span><span>${esc(v)}</span></div>`).join('')}</div>
      <div class="ap-section"><span class="ap-section__title">What I was doing</span>
        <div class="ap-steps">${ctx.steps.map(s => `<div class="ap-step${s.state === 'current' ? ' is-current' : ''}"><span>${s.state === 'current' ? '›' : '✓'}</span>${esc(s.text)}</div>`).join('')}</div></div>
      ${changes}`;
  }
  const always = ctx && ctx.allow_entry && !ctx.already_allowed
    ? `<button class="btn btn--ghost btn--46" data-action="approveAlways" data-id="${approval.id}">Always allow <span class="mono" style="color:var(--wait)">${esc(ctx.allow_entry)}</span> in ${esc(p ? p.name : 'this project')}</button>` : '';
  return `<aside class="pane" data-key="pane-ap-${approval.id}" aria-label="Approval">
    ${paneHead(c, task, task)}
    <div class="ap-body">
      ${details}
      <div class="sticky-actions">
        <button class="btn btn--approve btn--52" data-action="approve" data-id="${approval.id}"${busy ? ' disabled' : ''}>${busy ? spinner() + 'Approving…' : 'Approve once'}</button>
        ${always}
        <form class="ap-deny" data-submit="denyWithNote" data-id="${approval.id}">
          <input id="denyNote" class="input grow" placeholder="Deny with a note (optional)" value="${esc(ui.denyNote)}" data-input="denyNote" aria-label="Deny note" autocomplete="off">
          <button class="btn btn--danger btn--46" type="submit">Deny</button>
        </form>
        ${c.desk ? `<span class="ap-hint">A approve · D deny · Esc close</span>` : ''}
      </div>
    </div>
  </aside>`;
}
