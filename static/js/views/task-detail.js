// Task details: what a task is, who is doing it, and everything it did - the plan it is following,
// what it is doing now, files changed, git branch and diff, test results, approvals, the activity
// timeline and the final result. Reads GET /tasks/{id}/detail (polled while this tab is open).

import { statusOf, secs, mmss, fmtCost } from '../format.js';
import { esc, clock, spinner } from '../ui.js';
import { riskPill } from './plan-card.js';
import { kindOf } from '../preview.js';

const STEP = { completed: ['✓', 'var(--ok)'], in_progress: ['→', 'var(--run)'], pending: ['○', 'var(--text-ghost)'] };
const FILE = { M: ['M', 'var(--wait)'], A: ['A', 'var(--ok)'], D: ['D', 'var(--err)'], R: ['R', 'var(--review)'] };
const VERDICT = { passed: ['✓ Checked', 'var(--ok)'], failed: ['✕ Failed its checks', 'var(--err)'], unverified: ['◐ Not verified', 'var(--wait)'], 'n/a': ['', ''] };

const sec = (title, body, extra = '') => `<section class="td-sec${extra}"><span class="td-sec__t">${title}</span>${body}</section>`;
const kv = (k, v) => `<div class="td-kv"><span class="td-kv__k">${k}</span><span class="td-kv__v">${v}</span></div>`;

function diffView(id, diff) {
  if (!diff) return '';
  if (diff.loading) return `<div class="td-diff-load">${spinner()} Loading the diff…</div>`;
  if (diff.error) return `<div class="plan-blocked">${esc(diff.error)}</div>`;
  if (!diff.available) return `<span class="meta">${esc(diff.note || 'No diff available.')}</span>`;
  if (!diff.diff.trim()) return '<span class="meta">No changes to show.</span>';
  return `<div class="diff td-diff">${diff.diff.split('\n').map(l => {
    const cls = l.startsWith('+++') || l.startsWith('---') || l.startsWith('diff ') || l.startsWith('index ') ? '' : l.startsWith('@@') ? ' diff__line--hunk' : l.startsWith('+') ? ' diff__line--add' : l.startsWith('-') ? ' diff__line--del' : '';
    return `<div class="diff__line${cls}">${esc(l) || ' '}</div>`;
  }).join('')}${diff.truncated ? '<div class="diff__line diff__line--hunk">… diff truncated</div>' : ''}</div>`;
}

export function taskDetail(c, id) {
  const d = c.d.octo.detail[id];
  if (!d) return `<div class="td"><div class="empty">${spinner()} Loading the details…</div></div>`;
  const t = d.task;
  const st = statusOf(t);
  const live = t.status === 'running' ? (t.current_step || '') : '';
  const plan = d.routing && d.routing.plan;
  const diff = c.d.octo.diffs[id];
  const verdict = d.validation && VERDICT[d.validation.status] ? VERDICT[d.validation.status] : ['', ''];
  const steps = d.plan_steps.length ? `<div class="td-steps">${d.plan_steps.map(s => {
    const [g, color] = STEP[s.status] || STEP.pending;
    return `<div class="td-step${s.status === 'in_progress' ? ' is-now' : ''}"><span style="color:${color}">${g}</span><span>${esc(s.content)}</span></div>`;
  }).join('')}${d.progress ? `<div class="bar6 td-progress"><div style="width:${d.progress.percent}%;background:var(--run)"></div></div>` : ''}</div>`
    : `<span class="meta">${t.lane === 'agent' ? 'Claude Code hasn\'t written a plan for this yet.' : 'Answered in one step - no plan to follow.'}</span>`;

  const files = d.files.length ? `<div class="td-files">${d.files.map(f => {
    const [g, color] = FILE[f.status] || FILE.M;
    const canView = f.status !== 'D' && kindOf(f.path);
    return `<div class="td-file"><span class="td-file__s" style="color:${color}">${g}</span><span class="td-file__p truncate">${esc(f.path)}</span>${canView ? `<button class="btn btn--sm td-file__view" data-action="taskFile" data-id="${id}" data-path="${esc(f.path)}">Preview</button>` : ''}<span class="td-file__n"><i class="td-add">+${f.additions}</i> <i class="td-del">−${f.deletions}</i></span></div>`;
  }).join('')}</div>` : '<span class="meta">No files changed.</span>';

  const git = d.git ? `${kv('BRANCH', `${esc(d.git.branch || '?')}${d.git.base_branch && d.git.base_branch !== d.git.branch ? ` <span class="meta">(started on ${esc(d.git.base_branch)})</span>` : ''}`)}
    ${d.git.commits.length ? kv('COMMITS', d.git.commits.map(x => `<div class="td-commit">${esc(x)}</div>`).join('')) : ''}
    ${d.git.uncommitted ? '<span class="meta">Some changes are not committed yet.</span>' : ''}
    ${d.git.started_dirty ? `<span class="meta">The project already had ${d.git.started_dirty} uncommitted change${d.git.started_dirty === 1 ? '' : 's'} when this started, so the list may include those.</span>` : ''}
    ${d.files.length ? `<button class="btn btn--sm" data-action="taskDiff" data-id="${id}">${diff && !diff.loading ? 'Reload' : 'Show'} the diff</button>` : ''}
    ${diffView(id, diff)}` : `<span class="meta">${t.lane === 'agent' ? 'This project isn\'t a git repository.' : 'Not applicable.'}</span>`;

  const tests = d.tests.length ? d.tests.map(x => `<div class="td-test"><span style="color:${x.ok ? 'var(--ok)' : 'var(--err)'}">${x.ok ? '✓ passed' : '✕ failed'}</span><code>${esc(x.command)}</code>${x.ok ? '' : `<pre>${esc(x.excerpt)}</pre>`}</div>`).join('')
    : `<span class="meta">${t.lane === 'agent' ? 'No test command was run.' : 'Not applicable.'}</span>`;

  const approvals = d.approvals.length ? d.approvals.map(a => `<div class="td-appr"><span class="td-appr__s td-appr__s--${esc(a.status)}">${esc(a.status)}</span><span class="truncate">${esc(a.describe)}</span>${a.status === 'pending' ? `<button class="btn btn--sm" data-action="openApproval" data-id="${a.id}">Review</button>` : ''}</div>`).join('')
    : '<span class="meta">Nothing needed your approval.</span>';

  const timeline = d.events.length ? `<div class="td-time">${d.events.map(e => `<div class="td-time__row td-time__row--${esc(e.kind)}"><span class="td-time__at">${clock(e.created_at)}</span><span>${esc(e.text)}</span></div>`).join('')}</div>` : '<span class="meta">Nothing recorded yet.</span>';

  const result = d.result ? `<div class="td-result${t.status === 'error' ? ' is-error' : ''}">${esc(d.result)}</div>` : `<span class="meta">${['queued', 'running', 'rate_limited'].includes(t.status) ? 'Not finished yet.' : 'No result.'}</span>`;
  const took = t.duration_ms != null ? (t.lane === 'agent' ? mmss(t.duration_ms / 1000) : secs(t.duration_ms)) : '';

  return `<div class="td" data-key="td-${id}">
    <div class="td-head">
      <span class="td-head__title">${esc(t.title || t.prompt.slice(0, 80))}</span>
      <div class="td-head__row"><span class="td-status" style="--c:${st.color}">${esc(st.label)}</span>${t.risk ? riskPill(t.risk) : ''}${took ? `<span class="meta">${took}</span>` : ''}${t.cost_usd != null ? `<span class="meta">${fmtCost(t.cost_usd)}</span>` : ''}</div>
      ${live ? `<span class="td-live">${spinner()}<span class="truncate">${esc(live)}</span></span>` : ''}
    </div>
    <div class="td-grid">
      ${kv('PROJECT', d.project && !d.project.is_general ? esc(d.project.name) : '<span class="meta">none</span>')}
      ${kv('AGENT', d.agent ? esc(d.agent.label) : '<span class="meta">not recorded</span>')}
      ${kv('MODEL', esc(d.model.label || d.model.id || '?'))}
      ${kv('AI TOOL', esc((d.ai_tool && d.ai_tool.label) || d.model.label || '?'))}
      ${kv('ACCOUNT', d.ai_tool && d.ai_tool.account ? esc(d.ai_tool.account.label) : '<span class="meta">n/a (API key)</span>')}
      ${kv('TOOLS', d.tools.length ? d.tools.map(x => `<span class="risk-chip">${esc(x.label)}</span>`).join(' ') : '<span class="meta">none</span>')}
    </div>
    ${plan ? sec('WHY', `<span class="td-why">${esc(d.routing.plan.reason || '')}</span><span class="meta">${esc(t.route_reason || '')}</span>`) : (t.route_reason ? sec('WHY', `<span class="meta">${esc(t.route_reason)}</span>`) : '')}
    ${sec('PLAN', steps)}
    ${verdict[0] ? sec('CHECKED', `<span style="color:${verdict[1]}">${verdict[0]}</span> <span class="meta">${esc(d.validation.note)}</span>`) : ''}
    ${sec(`FILES CHANGED${d.files.length ? ` <i>${d.files.length}</i>` : ''}`, files)}
    ${sec('GIT', git)}
    ${sec('TESTS', tests)}
    ${sec(`APPROVALS${d.approvals.length ? ` <i>${d.approvals.length}</i>` : ''}`, approvals)}
    ${sec('ACTIVITY', timeline)}
    ${sec('FINAL RESULT', result)}
  </div>`;
}
