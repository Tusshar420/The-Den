// Chat pane: one conversation (a root task and its replies), with Claude Code's
// step log, inline approvals, reply footers and the composer.

import { ago, replyFooter, statusOf, fmtCost } from '../format.js';
import { projectById, projectColor, queuedTasks, rootId, taskById } from '../data.js';
import { commandOf } from '../derive.js';
import { esc, octo, icon, spinner, clock, took, routeNote, modelLabel, laneColor } from '../ui.js';
import { FEATURES } from '../features.js';

const ACTIVE = ['running', 'rate_limited', 'queued'];

export const QUICK = [
  ['Status', 'Give me a quick status: git status, failing tests, open TODOs.'],
  ['Run tests', 'Run the test suite and report failures.'],
  ['Deploy', 'Deploy to staging. Ask before the deploy step.'],
  ['Pull latest', 'Pull latest from origin and rebase this branch.'],
];

export function paneHead(c, root, latest) {
  const p = projectById(root.project_id);
  const st = c.w.uiStatus(latest);
  const s = st === 'approval' ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(latest);
  return `<div class="pane__head">
    <button class="icon-btn icon-btn--44 pane__close" data-action="closePane" aria-label="Close">${c.desk ? '✕' : '←'}</button>
    <div class="pane__titles">
      <span class="pane__title truncate">${esc(root.prompt)}</span>
      <span class="pane__sub"><span class="swatch" style="background:${projectColor(p)}"></span>${esc(p ? p.name : '?')} · <span style="color:${laneColor(root.lane)}">${esc(modelLabel(root.model_id))}</span> · <span style="color:${s.color}">${s.label}</span></span>
    </div>
  </div>`;
}

function toolLog(events, running) {
  if (!events.length) return '';
  return `<div class="msg-tools">${events.map((e, i) => {
    const current = running && i === events.length - 1;
    let g = '<span style="color:var(--ok)">✓</span>';
    if (current) g = '<span class="spinner spinner--tiny"></span>';
    else if (e.kind === 'approval' && e.text.startsWith('You denied')) g = '<span style="color:var(--err)">✕</span>';
    else if (e.kind === 'approval') g = '<span style="color:var(--wait)">!</span>';
    return `<div class="msg-tools__item" data-key="e${e.id}">${g}<span>${esc(e.text)}${current ? '…' : ''}</span></div>`;
  }).join('')}</div>`;
}

function inlineApproval(a) {
  return `<div class="msg-approval">
    <span class="msg-approval__label">WAITING FOR YOUR OK</span>
    <span class="msg-approval__cmd"><span class="cmd__prompt">$</span> ${esc(commandOf(a))}</span>
    <div class="msg-approval__acts">
      <button class="btn btn--ghost btn--38" data-action="openApproval" data-id="${a.id}">Details</button>
      <button class="btn btn--ghost btn--38" data-action="deny" data-id="${a.id}">Deny</button>
      <button class="btn btn--approve btn--38 grow" data-action="approve" data-id="${a.id}">Approve</button>
    </div>
  </div>`;
}

const sys = (text, color = 'var(--text-meta)') => `<div class="msg-sys" style="--c:${color}">${esc(text)}</div>`;

export function chatPane(c, taskId) {
  const { d, w } = c;
  const seed = taskById(taskId);
  const th = d.threads[taskId];
  const msgs = th ? th.tasks : seed ? d.tasks.filter(t => rootId(t) === rootId(seed)).sort((a, b) => a.id - b.id) : [];
  if (!msgs.length) return `<aside class="pane" data-key="pane-chat-${taskId}"><div class="pane__head"><button class="icon-btn icon-btn--44" data-action="closePane" aria-label="Close">${c.desk ? '✕' : '←'}</button><span class="pane__title">Loading…</span></div></aside>`;
  const root = msgs[0];
  // Status can be fresher in the 5-second snapshot than in the thread fetch.
  const fresh = msgs.map(m => ({ ...m, ...(taskById(m.id) || {}) }));
  const latest = fresh[fresh.length - 1];
  const api = root.lane === 'api';
  const label = modelLabel(root.model_id);
  const queue = queuedTasks(d);
  const events = (th && th.events) || {};
  let firstReply = true;

  const body = fresh.map(t => {
    const out = [];
    out.push(`<div class="msg-you" data-key="y${t.id}"><div class="msg-you__bubble">${esc(t.prompt)}</div><span class="msg-when">${esc(ago(t.created_at, c.now))}</span></div>`);
    const ev = (events[t.id] || []);
    if (!api) out.push(toolLog(ev, t.status === 'running'));
    const pending = w.apTask.get(t.id);
    if (pending) out.push(inlineApproval(pending));
    let text = null, mood = 'idle';
    if (t.status === 'done') text = t.result_summary || '(done, no summary)';
    else if (t.status === 'error' || t.status === 'needs_review') { text = t.error_message || 'Something went wrong.'; mood = t.status === 'error' ? 'error' : 'idle'; }
    if (text != null) {
      const f = replyFooter(t, modelLabel(t.model_id || root.model_id), c.now);
      // "2m ago · GPT-6 Luna · 2.1s · <$0.01" - missing values are dropped.
      const rest = [t.finished_at ? ago(t.finished_at, c.now) : '', modelLabel(t.model_id || root.model_id), took(t), fmtCost(t.cost_usd)].filter(Boolean).join(' · ');
      const note = firstReply ? routeNote(root) : '';
      firstReply = false;
      out.push(`<div class="msg-octo" data-key="o${t.id}">${octo(mood, 30)}<div class="msg-octo__col">
        <div class="msg-octo__bubble">${esc(text)}</div>
        <div class="msg-foot"><span class="msg-foot__status" style="--c:${f.color}">${f.status}</span><span>${esc(rest)}</span></div>
        ${note ? `<span class="msg-note">${esc(note)}</span>` : ''}
      </div></div>`);
    } else if (t.status === 'cancelled') {
      out.push(sys(t.error_message === 'Stopped from the dashboard' ? 'Stopped' : 'Cancelled'));
    } else if (t.status === 'queued') {
      const pos = queue.findIndex(x => x.id === t.id) + 1;
      out.push(sys(t.thread_id ? 'Queued · continues this session' : `Queued · #${pos || '?'} in line`, t.thread_id ? 'var(--run)' : 'var(--text-meta)'));
    } else if (t.status === 'rate_limited') {
      out.push(sys(`Hit the usage limit · resumes ${t.resume_at ? clock(t.resume_at) : 'soon'} automatically`, 'var(--sleep)'));
    }
    return out.join('');
  }).join('');

  const working = latest.status === 'running';
  const typing = working ? `<div class="typing" data-key="typing">${octo('dance', 30)}<div class="typing__dots"><i></i><i></i><i></i></div><span class="meta">${api ? esc(label) + ' is writing…' : 'Octopus is on it'}</span></div>` : '';

  const quick = [];
  if (!api) {
    const st = latest.status;
    if (st === 'error') quick.push(['Retry', 'data-action="retry" data-id="' + latest.id + '"']);
    if (st === 'needs_review') quick.push(['Looks good', 'data-action="reviewed" data-id="' + latest.id + '"']);
    if (['done', 'cancelled', 'error'].includes(st)) quick.push(['↻ Run again as new', 'data-action="runAgain" data-id="' + root.id + '"']);
    if (st === 'done') quick.push(['Open a PR', 'data-action="fillReply" data-text="Open a PR for this with a short description."']);
    if (st === 'running' && !w.apTask.has(latest.id)) quick.push(['Stop', 'data-action="stopTask" data-id="' + latest.id + '"']);
    QUICK.forEach(([l, txt]) => quick.push([l, `data-action="quickReply" data-text="${esc(txt)}"`]));
  }
  const activeAgent = !api && (ACTIVE.includes(latest.status) || w.apTask.has(latest.id));
  const placeholder = api ? `Reply to ${label}…` : activeAgent ? 'Add a note for when this turn ends…' : 'Reply to Claude Code…';
  const reply = c.ui.reply;
  const busy = c.ui.polishing === 'reply';

  return `<aside class="pane" data-key="pane-chat-${root.id}" aria-label="Chat">
    ${paneHead(c, root, latest)}
    <div class="chat">${body}${typing}</div>
    <div class="composer">
      ${quick.length ? `<div class="composer__quick" data-noswipe>${quick.map(([l, attrs]) => `<button ${attrs}>${esc(l)}</button>`).join('')}</div>` : ''}
      <div class="composer__box">
        ${FEATURES.images ? `<label class="tool-btn tool-btn--40" aria-label="Attach image"><input type="file" accept="image/*" multiple hidden>${icon('image', 20)}</label>` : ''}
        <button class="tool-btn tool-btn--40" data-action="polish" data-target="reply" title="Polish text" aria-label="Polish text"${!reply.trim() || busy ? ' disabled' : ''}>${busy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 18, { strokeWidth: 1.6 })}</button>
        <textarea id="replyInput" rows="1" placeholder="${esc(placeholder)}" aria-label="Reply" data-input="reply" data-enter="sendReply" data-autogrow="120" data-live-style>${esc(reply)}</textarea>
        <button class="ask__send" data-action="sendReply" aria-label="Send"${!reply.trim() || c.ui.busy.reply ? ' disabled' : ''}>${c.ui.busy.reply ? spinner() : '↑'}</button>
      </div>
    </div>
  </aside>`;
}
