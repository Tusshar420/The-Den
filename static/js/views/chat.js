// Chat pane: one conversation (a root task and its replies), with Claude Code's
// step log, inline approvals, reply footers and the composer.

import { ago, replyFooter, statusOf, fmtCost, taskName, PERMISSION_MODES, modeInfo, threadMode, modelState } from '../format.js';
import { projectById, projectColor, queuedTasks, rootId, taskById } from '../data.js';
import { commandOf } from '../derive.js';
import { esc, octo, icon, spinner, clock, took, routeNote, modelLabel, laneColor } from '../ui.js';
import { FEATURES } from '../features.js';
import { imageStrip, sentStrip } from '../attach.js';
import { assistantCard, isAssistant } from './assistant-card.js';
import { taskDetail, detailSwitch } from './task-detail.js';
import { latestInChat } from '../octopus-data.js';

const ACTIVE = ['running', 'rate_limited', 'queued'];

export const QUICK = [
  ['Status', 'Give me a quick status: git status, failing tests, open TODOs.'],
  ['Run tests', 'Run the test suite and report failures.'],
  ['Deploy', 'Deploy to staging. Ask before the deploy step.'],
  ['Pull latest', 'Pull latest from origin and rebase this branch.'],
];

// Desktop: one tab per open conversation along the top of the side pane, so several chats stay a click apart.
function tabInfo(c, id) {
  const { d, w } = c;
  const msgs = d.tasks.filter(t => rootId(t) === id);
  const th = d.threads[id];
  const root = msgs.length ? msgs[msgs.length - 1] : th && th.tasks[0];
  const latest = msgs.length ? msgs[0] : root;
  if (!root) return { title: 'Chat', color: 'var(--text-meta)' };
  const title = taskName(th && th.tasks[0] || root);
  const st = w.uiStatus(latest);
  return { title, color: st === 'approval' ? 'var(--wait)' : statusOf(latest).color, live: st === 'running' || st === 'approval' };
}

export function paneTabs(c) {
  if (!c.desk || !c.ui.tabs.length) return '';
  const p = c.ui.pane;
  const active = p && p.taskId != null ? rootId(taskById(p.taskId) || { id: p.taskId }) : null;
  return `<div class="pane__tabs" role="tablist" data-key="pane-tabs" data-noswipe>
    ${c.ui.tabs.map(id => {
      const t = tabInfo(c, id);
      return `<div class="ptab${id === active ? ' is-active' : ''}" role="tab" aria-selected="${id === active}" data-key="tab-${id}">
        <button class="ptab__main" data-action="switchTab" data-id="${id}" title="${esc(t.title)}"><span class="ptab__dot${t.live ? ' is-live' : ''}" style="background:${t.color}"></span><span class="truncate">${esc(t.title)}</span></button>
        <button class="ptab__x" data-action="closeTab" data-id="${id}" aria-label="Close tab">✕</button>
      </div>`;
    }).join('')}
    <button class="ptab__new" data-action="openPalette" aria-label="New chat" title="New chat">+</button>
  </div>`;
}

// Desktop, after closing the chat you were on: the remaining tabs stay above, none picked.
export function emptyPane(c) {
  const n = c.ui.tabs.length;
  return `<aside class="pane" data-key="pane-empty">${paneTabs(c)}<div class="pane__head"><span class="pane__title grow">No chat open</span><button class="icon-btn icon-btn--44" data-action="closePane" aria-label="Close">✕</button></div>
    <div class="pane-empty">${octo('idle', 64)}
      <span class="pane-empty__title">Pick a chat to continue</span>
      <span class="pane-empty__sub">${n === 1 ? 'Your other chat is' : `Your other ${n} chats are`} in the tabs above - click one to pick up where you left off, or start something new.</span>
      <button class="btn btn--ghost btn--38" data-action="openPalette">New chat<span class="kbd-hint">⌘K</span></button>
    </div></aside>`;
}

export function paneHead(c, root, latest) {
  const p = projectById(root.project_id);
  const st = c.w.uiStatus(latest);
  const s = st === 'approval' ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(latest);
  // Phone: a back arrow on the left (the pane is full-screen). Desktop: ✕ on the right.
  const close = `<button class="icon-btn icon-btn--44 pane__close" data-action="closePane" aria-label="Close">${c.desk ? '✕' : '←'}</button>`;
  return `${paneTabs(c)}<div class="pane__head">
    ${c.desk ? '' : close}
    <div class="pane__titles">
      <span class="pane__title truncate" title="${esc(taskName(root))}">${esc(taskName(root))}</span>
      <span class="pane__sub"><span class="swatch" style="background:${projectColor(p)}"></span>${esc(p ? p.name : '?')} · <span style="color:${laneColor(latest.lane || root.lane)}">${esc(modelLabel(latest.model_id || root.model_id))}</span> · <span style="color:${s.color}">${s.label}</span></span>
    </div>
    <button class="btn btn--ghost btn--38 pane__sum" data-action="summarise" data-id="${root.id}" title="Summarise this task" aria-label="Summarise this task">${icon('summary', 16)}<span class="pane__sum-label">Summarise</span></button>
    ${c.desk ? close : ''}
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

// What to offer under an assistant chat: the everyday reads. (Claude Code chats get their own set above.)
const ASSIST_QUICK = [
  ['Today', "What's on my calendar today?"], ['Tomorrow', "What's on my calendar tomorrow?"],
  ['Inbox', 'Check my inbox'], ['Reminders', 'What reminders do I have?'],
];

function inlineApproval(c, a) {
  if (isAssistant(a)) return `<div class="msg-approval msg-approval--pa">${assistantCard(c, a)}</div>`;
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
  // Thread still on its way: the chat's name and a plain "Opening this chat…", nothing that looks like work under way.
  if (!msgs.length) return `<aside class="pane" data-key="pane-chat-${taskId}">${paneTabs(c)}<div class="pane__head">${c.desk ? '' : `<button class="icon-btn icon-btn--44" data-action="closePane" aria-label="Close">←</button>`}<span class="pane__title grow truncate">${esc(tabInfo(c, taskId).title)}</span>${c.desk ? `<button class="icon-btn icon-btn--44" data-action="closePane" aria-label="Close">✕</button>` : ''}</div>
    <div class="pane-empty"><span class="meta">Opening this chat…</span></div></aside>`;
  const root = msgs[0];
  // Status can be fresher in the 5-second snapshot than in the thread fetch.
  const fresh = msgs.map(m => ({ ...m, ...(taskById(m.id) || {}) }));
  const latest = fresh[fresh.length - 1];
  // A chat can change models between messages: what it's on now is its latest message's.
  const api = latest.lane === 'api';
  const assist = root.lane === 'assistant';
  const label = modelLabel(latest.model_id || root.model_id);
  const queue = queuedTasks(d);
  const events = (th && th.events) || {};
  let firstReply = true;
  let prevModel = null;

  const body = fresh.map(t => {
    const out = [];
    if (prevModel && t.model_id && t.model_id !== prevModel) out.push(sys(`Switched to ${modelLabel(t.model_id)} · it has the conversation so far`));
    prevModel = t.model_id || prevModel;
    out.push(`<div class="msg-you" data-key="y${t.id}"><div class="msg-you__bubble">${esc(t.prompt)}${sentStrip(t.images)}</div><span class="msg-when">${esc(ago(t.created_at, c.now))}</span></div>`);
    const ev = (events[t.id] || []);
    if (t.lane !== 'api') out.push(toolLog(ev, t.status === 'running'));
    const pending = w.apTask.get(t.id);
    if (pending) out.push(inlineApproval(c, pending));
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
      const label = t.paused ? 'Paused · keeps its place in line' : t.run_now ? 'Starting now' : pos === 1 ? 'Queued · next in line' : `Queued · #${pos || '?'} in line`;
      out.push(`<div class="msg-queued" data-key="q${t.id}"><span class="msg-queued__label" style="--c:${t.run_now ? 'var(--run)' : 'var(--text-meta)'}">${esc(label)}</span>
        ${t.paused ? `<button class="btn btn--ghost btn--sm" data-action="playTask" data-id="${t.id}">${icon('play', 12)} Play</button>`
          : `${t.run_now ? '' : `<button class="btn btn--ghost btn--sm" data-action="runNow" data-id="${t.id}">▶ Proceed now</button>`}
        <button class="btn btn--ghost btn--sm" data-action="pauseTask" data-id="${t.id}">${icon('pause', 12)} Pause</button>`}
        <button class="btn btn--ghost btn--sm" data-action="editTask" data-id="${t.id}">Edit</button></div>`);
    } else if (t.status === 'rate_limited') {
      out.push(sys(`Hit the usage limit · resumes ${t.resume_at ? clock(t.resume_at) : 'soon'} automatically`, 'var(--sleep)'));
    }
    return out.join('');
  }).join('');

  const working = latest.status === 'running' && !latest.paused;
  const typing = working ? `<div class="typing" data-key="typing">${octo('dance', 30)}<div class="typing__dots"><i></i><i></i><i></i></div><span class="meta">${api ? esc(label) + ' is writing…' : 'Octopus is on it'}</span></div>` : '';

  const quick = [];
  if (assist) ASSIST_QUICK.forEach(([l, txt]) => quick.push([l, `data-action="quickReply" data-text="${esc(txt)}"`]));
  if (!api && !assist) {
    const st = latest.status;
    if (st === 'error') quick.push(['Retry', 'data-action="retry" data-id="' + latest.id + '"']);
    if (st === 'needs_review') quick.push(['Looks good', 'data-action="reviewed" data-id="' + latest.id + '"']);
    if (['done', 'cancelled', 'error'].includes(st)) quick.push(['↻ Run again as new', 'data-action="runAgain" data-id="' + root.id + '"']);
    if (st === 'done') quick.push(['Open a PR', 'data-action="fillReply" data-text="Open a PR for this with a short description."']);
    if (st === 'running' && !w.apTask.has(latest.id)) {
      quick.push(latest.paused ? ['Play', 'data-action="playTask" data-id="' + latest.id + '"'] : ['Pause', 'data-action="pauseTask" data-id="' + latest.id + '"']);
      quick.push(['Stop', 'data-action="stopTask" data-id="' + latest.id + '"']);
    }
    QUICK.forEach(([l, txt]) => quick.push([l, `data-action="quickReply" data-text="${esc(txt)}"`]));
  }
  const activeAgent = !api && !assist && (ACTIVE.includes(latest.status) || w.apTask.has(latest.id));
  // The next reply goes to the picked model, or stays on the one the chat is on.
  const onId = latest.model_id || root.model_id || 'claude-code';
  const switchable = assist ? [] : c.d.models.filter(m => (m.lane === 'agent' || m.lane === 'api') && m.modality !== 'image').map(m => modelState(m, c.now));
  const nextId = c.ui.replyModel || onId;
  const next = switchable.find(m => m.id === nextId);
  const switching = nextId !== onId;
  const nextLane = next ? next.lane : latest.lane;
  const nextLabel = next ? next.label : label;
  const placeholder = nextLane === 'api' ? `Reply to ${nextLabel}…` : assist ? 'Ask your assistant…' : activeAgent && !switching ? 'Add a note for when this turn ends…' : 'Reply to Claude Code…';
  const reply = c.ui.reply;
  const busy = c.ui.polishing === 'reply';
  // Agent-lane replies only. Starts on the mode the chat last ran under; picking one applies to the next reply.
  const canMode = nextLane === 'agent' && !assist;
  const mode = modeInfo(c.ui.replyMode || threadMode(fresh));
  const modelMenu = c.ui.replyMenu === 'model' && switchable.length > 1 ? `<div class="menu-scrim" data-action="closeMenus"></div>
      <div class="menu menu--mode menu--up" role="menu"><span class="menu__label">SEND THIS REPLY TO</span>
      ${switchable.map(m => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${nextId === m.id}" data-action="replyModel" data-id="${m.id}"${m.available || m.rate ? '' : ' aria-disabled="true"'}>
        <span class="dot" style="--c:${m.dot}"></span><span class="menu__two"><span class="menu__main">${esc(m.label + m.pickerSuffix)}</span><span class="menu__sub">${esc(m.id === onId ? 'this chat is on it now' : m.status)}</span></span><span class="menu__check">${nextId === m.id ? '✓' : ''}</span></button>`).join('')}
      </div>` : '';
  const modeMenu = canMode && c.ui.replyMenu === 'mode' ? `<div class="menu-scrim" data-action="closeMenus"></div>
      <div class="menu menu--mode menu--up" role="menu"><span class="menu__label">HOW CLAUDE CODE WORKS FROM HERE</span>
      ${PERMISSION_MODES.map(o => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${mode.id === o.id}" data-action="replyMode" data-id="${o.id}">
        <span class="dot" style="--c:${o.dot}"></span><span class="menu__two"><span class="menu__main">${esc(o.label)}</span><span class="menu__sub">${esc(o.sub)}</span></span><span class="menu__check">${mode.id === o.id ? '✓' : ''}</span></button>`).join('')}
      </div>` : '';

  const details = c.ui.octo.detailOpen;
  return `<aside class="pane" data-key="pane-chat-${root.id}" aria-label="Chat">
    ${paneHead(c, root, latest)}
    ${detailSwitch(c)}
    ${details ? `<div class="chat chat--detail">${taskDetail(c, latestInChat(root.id))}</div>` : `<div class="chat">${body}${typing}</div>`}
    ${details ? '' : `<div class="composer">
      ${quick.length ? `<div class="composer__quick" data-noswipe>${quick.map(([l, attrs]) => `<button ${attrs}>${esc(l)}</button>`).join('')}</div>` : ''}
      ${modeMenu}${modelMenu}
      ${switching ? `<div class="composer__switch">Switching to <b>${esc(nextLabel)}</b> · it gets this whole conversation</div>` : ''}
      ${imageStrip((c.ui.replyImages || {})[root.id], 'reply')}
      <div class="composer__box">
        ${switchable.length > 1 ? `<button class="ask__model composer__model${switching ? ' is-switching' : ''}" data-action="replyModelMenu" title="Change the model for this reply" aria-label="Model: ${esc(nextLabel)}"><span class="dot" style="--c:${next ? next.dot : 'var(--text)'};width:7px;height:7px"></span><span class="truncate">${esc(nextLabel)}</span></button>` : ''}
        ${canMode ? `<button class="ask__model composer__mode" data-action="replyMenu" title="${esc(mode.label + ' - ' + mode.sub)}" aria-label="Mode: ${esc(mode.label)}"><span class="dot" style="--c:${mode.dot};width:7px;height:7px"></span>${esc(mode.short)}</button>` : ''}
        ${FEATURES.images ? `<label class="tool-btn tool-btn--40" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="reply">${icon('image', 20)}</label>` : ''}
        <button class="tool-btn tool-btn--40" data-action="polish" data-target="reply" title="Polish text" aria-label="Polish text"${!reply.trim() || busy ? ' disabled' : ''}>${busy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 18, { strokeWidth: 1.6 })}</button>
        <textarea id="replyInput" rows="1" placeholder="${esc(placeholder)}" aria-label="Reply" data-input="reply" data-enter="sendReply" data-autogrow="120" data-live-style>${esc(reply)}</textarea>
        <button class="ask__send" data-action="sendReply" aria-label="Send"${!reply.trim() || c.ui.busy.reply ? ' disabled' : ''}>${c.ui.busy.reply ? spinner() : '↑'}</button>
      </div>
    </div>`}
  </aside>`;
}
