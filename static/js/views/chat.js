// Chat pane: one conversation (a root task and its replies), with Claude Code's
// step log, inline approvals, reply footers and the composer.

import { ago, replyFooter, statusOf, fmtCost, taskName, PERMISSION_MODES, modeInfo, threadMode, modelState } from '../format.js';
import { projectById, projectColor, queuedTasks, rootId, taskById, crmOnly } from '../data.js';
import { commandOf } from '../derive.js';
import { acctDots, esc, octo, icon, restartTag, spinner, clock, took, routeNote, modelLabel, laneColor, progressBar, usedBy } from '../ui.js';
import { FEATURES } from '../features.js';
import { imageStrip, sentStrip, replyText } from '../attach.js';
import { assistantCard, isAssistant } from './assistant-card.js';
import { KINDS } from '../notifications.js';
import { accountById, accountList, usesClaudeAccount, shortName, accountMenuPart, accountShort } from './accounts.js';

const ACTIVE = ['running', 'rate_limited', 'queued'];

export const QUICK = [
  ['Status', 'Give me a quick status: git status, failing tests, open TODOs.'],
  ['Run tests', 'Run the test suite and report failures.'],
  ['Pull latest', 'Pull latest from origin and rebase this branch.'],
];

// Desktop: one tab per open conversation along the top of the side pane, so several chats stay a click apart.
export function tabInfo(c, id) {
  const { d, w } = c;
  const msgs = d.tasks.filter(t => rootId(t) === id);
  const th = d.threads[id];
  const root = msgs.length ? msgs[msgs.length - 1] : th && th.tasks[0];
  const latest = msgs.length ? msgs[0] : root;
  if (!root) return { title: 'Chat', color: 'var(--text-meta)' };
  const title = taskName(th && th.tasks[0] || root);
  const st = w.uiStatus(latest);
  const proj = projectById(root.project_id);
  return { title, project: proj ? proj.name : '', color: st === 'approval' ? 'var(--wait)' : statusOf(latest).color, live: st === 'running' || st === 'approval' };
}

export function paneTabs(c) {
  if (!c.desk || !c.ui.tabs.length) return '';
  const p = c.ui.pane;
  const active = p && p.taskId != null ? rootId(taskById(p.taskId) || { id: p.taskId }) : null;
  return `<div class="pane__tabs" role="tablist" data-key="pane-tabs" data-noswipe>
    ${c.docked && p && p.type !== 'empty' ? `<button class="ptab__new" data-action="messageList" aria-label="${c.ui.side === 'queue' ? 'Back to the queue' : 'All messages'}" title="${c.ui.side === 'queue' ? 'Back to the queue' : 'All messages'}">${icon(c.ui.side === 'queue' ? 'queue' : 'messages', 16)}</button>` : ''}${c.ui.tabs.map(id => {
      const t = tabInfo(c, id), pinned = (c.ui.pinned || []).includes(id);
      return `<div class="ptab${id === active ? ' is-active' : ''}${pinned ? ' is-pinned' : ''}" data-tabid="${id}" role="tab" aria-selected="${id === active}" data-key="tab-${id}">
        <button class="ptab__main" data-action="switchTab" data-id="${id}" title="${esc(t.title)}"><span class="ptab__dot${t.live ? ' is-live' : ''}" style="background:${t.color}"></span><span class="ptab__text"><span class="truncate ptab__title">${esc(t.title)}</span><span class="truncate ptab__sub"><span class="task-num">#${id}</span>${t.project ? ' · ' + esc(t.project) : ''}</span></span></button>
        <button class="ptab__x ptab__pin" data-action="pinTab" data-id="${id}" aria-pressed="${pinned}" aria-label="${pinned ? 'Unpin' : 'Pin'} this chat" title="${pinned ? 'Unpin' : 'Pin'} this chat"><svg viewBox="0 0 16 16" width="12" height="12" fill="${pinned ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2.5l4 4-2 .8-2.2 2.7.2 2.5-1 1-2.6-2.6-3.4 3.4M5.8 6.2l1-1 2.5.2"/></svg></button>
        <button class="ptab__x ptab__close" data-action="closeTab" data-id="${id}" aria-label="Close tab">✕</button>
      </div>`;
    }).join('')}
    <button class="ptab__new" data-action="openPalette" aria-label="New chat" title="New chat">+</button>
  </div>`;
}

// Desktop, chat window minimised: a chip bottom-right that brings it back.
export function paneChip(c) {
  const p = c.ui.pane;
  const id = p && p.taskId != null ? rootId(taskById(p.taskId) || { id: p.taskId }) : c.ui.tabs[0];
  const t = id != null ? tabInfo(c, id) : { title: 'Chats', color: 'var(--text-meta)' };
  const n = c.ui.tabs.length;
  return `<button class="pane-chip" data-key="pane-chip" data-action="restorePane" title="Open chat window">
    <span class="ptab__dot${t.live ? ' is-live' : ''}" style="background:${t.color}"></span>${id != null ? `<span class="task-num">#${id}</span>` : ''}<span class="truncate">${esc(p && p.type === 'voice' ? 'Voice chat' : t.title)}</span>${n > 1 ? `<span class="pane-chip__n">${n}</span>` : ''}</button>`;
}

// Pane header window buttons. Phone: a back arrow on the left (the pane is full-screen, one screen at a time).
// Desktop: none - pin and close live on each tab. ("Close all tabs" lives in the ⋯ menu.)
export function paneClose(c) {
  if (!c.desk) return `<button class="icon-btn icon-btn--44 pane__close" data-action="closePane" aria-label="Close">←</button>`;
  return '';   // desktop: no window buttons; each tab has its own pin and ✕
}

// Round mic at the bottom right of the side pane: starts a voice chat (opens the voice pane and listens).
export function voiceFab() {
  return '<button class="pane-voice" data-action="voiceMic" title="Talk to Octopus" aria-label="Start voice chat"><svg width="22" height="22" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7.2" y="2.5" width="5.6" height="10" rx="2.8"/><path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5"/></svg></button>';
}

// Desktop, after closing the chat you were on: the remaining tabs stay above, none picked.
export function emptyPane(c) {
  const n = c.ui.tabs.length;
  return `<aside class="pane" data-key="pane-empty">${paneTabs(c)}<div class="pane__head">${paneClose(c)}<span class="pane__title grow">No chat open</span></div>
    <div class="pane-empty">${octo('idle', 64)}
      <span class="pane-empty__title">Pick a chat to continue</span>
      <span class="pane-empty__sub">${n === 1 ? 'Your other chat is' : `Your other ${n} chats are`} in the tabs above - click one to pick up where you left off, or start something new.</span>
      <button class="btn btn--ghost btn--38" data-action="openPalette">New chat<span class="kbd-hint">⌘K</span></button>
    </div>${voiceFab()}</aside>`;
}

export function paneHead(c, root, latest) {
  const p = projectById(root.project_id);
  const st = c.w.uiStatus(latest);
  const s = st === 'approval' ? { label: 'Waiting on you', color: 'var(--wait)' } : statusOf(latest);
  const close = paneClose(c);
  const by = usedBy(c.d.tasks.filter(t => rootId(t) === root.id));   // every model and account the chat's tasks recorded
  return `${paneTabs(c)}<div class="pane__head">
    ${close}
    <div class="pane__titles">
      <button class="pane__title pane__title--btn truncate" data-action="openTaskDetail" data-id="${root.id}" title="Show details" aria-label="Show details">${esc(taskName(root))}</button>
      <span class="pane__sub"><span class="task-num">#${root.id}</span><span class="swatch" style="background:${projectColor(p)}"></span>${esc(p ? p.name : '?')} · <span style="color:${laneColor(latest.lane || root.lane)}">${esc(by.models || modelLabel(latest.model_id || root.model_id))}</span>${by.account ? ' · ' + acctDots(c.d.tasks.filter(t => rootId(t) === root.id)) + esc(by.account) : ''} · <span style="color:${s.color}">${s.label}</span>${restartTag({ id: root.id, msgs: c.d.tasks.filter(t => rootId(t) === root.id) })}</span>
    </div>
    <button class="btn btn--ghost btn--38 pane__more" data-action="chatActions" data-id="${root.id}" title="More: summarise, move or delete" aria-label="More options">⋯</button>
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
  if (isAssistant(a)) return `<div class="msg-approval msg-approval--pa" data-key="ap${a.id}">${assistantCard(c, a)}</div>`;
  return `<div class="msg-approval" data-key="ap${a.id}">
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

// "@octopus ..." in a reply: Octopus took it as a request of its own, and the chat keeps this note
// (octopus/service.py handoff). It isn't the chat's model, so it never counts as the chat's latest message.
export const MENTION_RE = /(^|[^\w@])@octopus\b/i;
export const isHandoff = t => t.lane === 'control' && t.routed_by === 'mention';
export const lastReal = msgs => [...msgs].reverse().find(t => !isHandoff(t)) || msgs[msgs.length - 1];
const withMention = s => esc(s).replace(/(^|[^\w@])(@octopus)\b/gi, '$1<b class="mention">$2</b>');

function handoffReply(c, t) {
  const s = t.result_summary || '';
  const open = /^Octopus is on it in #(\d+)/.exec(s);
  const btn = open ? `<button class="btn btn--ghost btn--sm" data-action="openChat" data-id="${open[1]}">Open #${open[1]}</button>`
    : s.startsWith('Octopus has a plan') && t.routing_id ? `<button class="btn btn--ghost btn--sm" data-action="planReview" data-id="${t.routing_id}">Review plan</button>` : '';
  return `<div class="msg-octo" data-key="o${t.id}">${octo('idle', 30)}<div class="msg-octo__col">
    <div class="msg-octo__bubble">${replyText(s)}</div>
    <div class="msg-foot">${btn}<span>Octopus${t.finished_at ? ' · ' + esc(ago(t.finished_at, c.now)) : ''}</span></div>
  </div></div>`;
}

export function chatPane(c, taskId) {
  const { d, w } = c;
  const seed = taskById(taskId);
  const th = d.threads[taskId];
  const msgs = th ? th.tasks : seed ? d.tasks.filter(t => rootId(t) === rootId(seed)).sort((a, b) => a.id - b.id) : [];
  // Thread still on its way: the chat's name and a plain "Opening this chat…", nothing that looks like work under way.
  if (!msgs.length) return `<aside class="pane" data-key="pane-chat-${taskId}">${paneTabs(c)}<div class="pane__head">${paneClose(c)}<span class="pane__title grow truncate">${esc(tabInfo(c, taskId).title)}</span></div>
    <div class="pane-empty"><span class="meta">Opening this chat…</span></div></aside>`;
  const root = msgs[0];
  // Status can be fresher in the 5-second snapshot than in the thread fetch.
  const fresh = msgs.map(m => ({ ...m, ...(taskById(m.id) || {}) }));
  const latest = lastReal(fresh);
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
    // A message handed straight to a running Claude has no reply of its own: show it as delivered.
    const passed = t.status === 'done' && /^Passed to Claude/.test(t.result_summary || '');
    // One tick = sent (waiting), two grey = Claude has it, two blue = answered / handed over.
    const tick = passed || t.status === 'done' ? ['✓✓', 'read', 'Answered']
      : t.status === 'running' || t.status === 'approval' ? ['✓✓', '', 'Claude has it']
      : ['✓', '', 'Sent'];
    const editBtn = t.status === 'queued' ? `<button class="msg-you__edit" title="Edit prompt" aria-label="Edit prompt" data-action="editTask" data-id="${t.id}">${icon('edit', 14)}</button>` : '';
    out.push(`<div class="msg-you" data-key="y${t.id}"><div class="msg-you__bubble">${withMention(t.prompt)}${sentStrip(t.images)}</div>${editBtn}<span class="msg-when">${esc(ago(t.created_at, c.now))}<span class="msg-tick ${tick[1]}" title="${tick[2]}">${tick[0]}</span></span></div>`);
    if (passed) return out.join('');
    if (isHandoff(t)) return out.join('') + handoffReply(c, t);
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
      const rest = [t.finished_at ? ago(t.finished_at, c.now) : '', modelLabel(t.model_id || root.model_id), usedBy([t]).account, took(t), fmtCost(t.cost_usd)].filter(Boolean).join(' · ');
      const note = firstReply ? routeNote(root) : '';
      firstReply = false;
      out.push(`<div class="msg-octo" data-key="o${t.id}">${octo(mood, 30)}<div class="msg-octo__col">
        <div class="msg-octo__bubble">${replyText(text)}</div>
        <div class="msg-foot"><span class="msg-foot__status" style="--c:${f.color}">${f.status}</span>${acctDots([t])}<span>${esc(rest)}</span></div>
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
        ${queuedOpts(c, t)}</div>`);
    } else if (t.status === 'rate_limited') {
      const acct = accountById(c, t.claude_account);
      out.push(sys(`Hit ${acct ? shortName(acct) + "'s" : 'the'} usage limit · resumes ${t.resume_at ? clock(t.resume_at) : 'soon'} on the same account automatically`, 'var(--sleep)'));
    }
    return out.join('');
  }).join('');

  const working = latest.status === 'running' && !latest.paused;
  const typing = working ? `<div class="typing" data-key="typing">${octo('dance', 30)}<div class="typing__dots"><i></i><i></i><i></i></div><span class="meta">${api ? esc(label) + ' is writing…' : 'Octopus is on it'}</span>${api ? '' : progressBar(latest, c.now, 'typing__prog')}</div>` : '';

  const quick = [];
  if (assist) ASSIST_QUICK.forEach(([l, txt]) => quick.push([l, `data-action="quickReply" data-text="${esc(txt)}"`]));
  // A CRM-only project (no folder) can't run anything, so no Retry / Run again - only the way to fix that.
  const noFolder = crmOnly(projectById(root.project_id));
  if (noFolder) quick.push(['Link a folder to let Octopus work on this project', `data-action="openProject" data-id="${root.project_id}"`]);
  if (!api && !assist && !noFolder) {
    const st = latest.status;
    if (st === 'error') quick.push(['Retry', 'data-action="retry" data-id="' + latest.id + '"']);
    if (st === 'needs_review') quick.push(['Looks good', 'data-action="reviewed" data-id="' + latest.id + '"']);
    if (st === 'cancelled') quick.push(['Retry', 'data-action="retry" data-id="' + latest.id + '"']);
    if (['error', 'cancelled'].includes(st)) quick.push(['✎ Edit and retry', 'data-action="editRetry" data-id="' + latest.id + '"']);
    if (['done', 'cancelled', 'error'].includes(st)) quick.push(['↻ Run again as new', 'data-action="runAgain" data-id="' + root.id + '"']);
    if (st === 'done') quick.push(['Open a PR', 'data-action="fillReply" data-text="Open a PR for this with a short description."']);
    if (st === 'running' && !w.apTask.has(latest.id)) {
      quick.push(latest.paused ? ['Play', 'data-action="playTask" data-id="' + latest.id + '"'] : ['Pause', 'data-action="pauseTask" data-id="' + latest.id + '"']);
      quick.push(['Stop', 'data-action="stopTask" data-id="' + latest.id + '"']);
    }
    quick.push(['Hide', 'data-action="hideChat" data-id="' + root.id + '"']);
    quick.push(['Deploy', `data-action="deploy" data-pid="${root.project_id}"`]);
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
  const placeholder = nextLane === 'api' ? `Reply to ${nextLabel}…` : assist ? 'Ask your assistant…' : activeAgent && !switching ? 'Tell Claude something while it works…' : 'Reply to Claude Code…';
  const reply = c.ui.reply;
  const busy = c.ui.polishing === 'reply';
  // Agent-lane replies only. Starts on the mode the chat last ran under; picking one applies to the next reply.
  const canMode = nextLane === 'agent' && !assist;
  const mode = modeInfo(c.ui.replyMode || threadMode(fresh));
  // Claude Code replies: the account it runs on - the one picked, else the one the chat's last message was pinned to.
  const acctVal = replyAccount(c, latest);
  const acctPart = nextLane === 'agent' && !assist ? accountMenuPart(c, { model: nextId, value: acctVal, action: 'replyAccount', projectId: root.project_id }) : '';
  const acctName = acctPart ? accountShort(c, acctVal, root.project_id) : '';
  const modelMenu = c.ui.replyMenu === 'model' && (switchable.length > 1 || acctPart) ? `<div class="menu-scrim" data-action="closeMenus"></div>
      <div class="menu menu--mode menu--up" role="menu"><span class="menu__label">SEND THIS REPLY TO</span>
      ${switchable.map(m => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${nextId === m.id}" data-action="replyModel" data-id="${m.id}"${m.available || m.rate ? '' : ' aria-disabled="true"'}>
        <span class="dot" style="--c:${m.dot}"></span><span class="menu__two"><span class="menu__main">${esc(m.label + m.pickerSuffix)}</span><span class="menu__sub">${esc(m.id === onId ? 'this chat is on it now' : m.status)}</span></span><span class="menu__check">${nextId === m.id ? '✓' : ''}</span></button>`).join('')}
      ${acctPart}
      </div>` : '';

  const jump = !!c.ui.chatNew && c.ui.chatNew.pane === 'pane-chat-' + root.id;

  return `<aside class="pane" data-key="pane-chat-${root.id}" aria-label="Chat">
    <div class="pane__top">${paneHead(c, root, latest)}</div>
    <div class="chat">${body}${typing}</div>
    <div class="composer">
      ${voiceFab()}
      ${jump ? `<button class="chat-new" data-action="chatJump" data-key="chat-new">${c.ui.chatNew.approval ? 'Waiting for your OK' : 'New reply'} ↓</button>` : ''}
      ${chatNote(c.ui.chatNote, root.id, jump)}
      ${quick.length ? `<div class="composer__quick" data-noswipe>${quick.map(([l, attrs]) => `<button ${attrs}>${esc(l)}</button>`).join('')}</div>` : ''}
      ${modelMenu}
      ${MENTION_RE.test(reply) ? `<div class="composer__switch"><b>Octopus</b> takes this one · ${esc(label)} won't answer it</div>`
        : switching ? `<div class="composer__switch">Switching to <b>${esc(nextLabel)}</b> · it gets this whole conversation</div>` : ''}
      ${imageStrip((c.ui.replyImages || {})[root.id], 'reply')}
      <div class="composer__box">
        ${switchable.length > 1 || acctPart ? `<button class="ask__model composer__model${switching ? ' is-switching' : ''}" data-action="replyModelMenu" title="Change the model or account for this reply" aria-label="Model: ${esc(nextLabel)}${acctName ? ', account: ' + esc(acctName) : ''}"><span class="dot" style="--c:${next ? next.dot : 'var(--text)'};width:7px;height:7px"></span><span class="truncate">${esc(nextLabel)}${acctName ? ` · ${esc(acctName)}` : ''}</span></button>` : ''}
        ${canMode ? `<div class="segmented mode-seg composer__mode" role="group" aria-label="How Claude Code works from here" data-noswipe>${PERMISSION_MODES.map(o => `<button class="segmented__opt${mode.id === o.id ? ' is-on' : ''}" title="${esc(o.label + ' - ' + o.sub)}" aria-pressed="${mode.id === o.id}" data-action="replyMode" data-id="${o.id}">${esc(o.short)}</button>`).join('')}</div>` : ''}
        ${FEATURES.images ? `<label class="tool-btn tool-btn--40" title="Attach picture" aria-label="Attach picture"><input type="file" accept="image/*" multiple hidden data-attach="reply">${icon('image', 20)}</label>` : ''}
        <button class="tool-btn tool-btn--40" data-action="polish" data-target="reply" title="Polish text" aria-label="Polish text"${!reply.trim() || busy ? ' disabled' : ''}>${busy ? '<span class="spinner spinner--tool"></span>' : icon('polish', 18, { strokeWidth: 1.6 })}</button>
        <textarea id="replyInput" rows="1" placeholder="${esc(placeholder)}" aria-label="Reply" data-input="reply" data-enter="sendReply" data-autogrow="120" data-live-style>${esc(reply)}</textarea>
        <button class="ask__send" data-action="sendReply" aria-label="Send"${!reply.trim() || c.ui.busy.reply ? ' disabled' : ''}>${c.ui.busy.reply ? spinner() : '↑'}</button>
      </div>
    </div>
  </aside>`;
}

// The account a reply goes to: the one picked in the menu, else the chat's last pinned one (if it still exists), else Auto ('').
export function replyAccount(c, latest) {
  if (c.ui.replyAccount != null) return c.ui.replyAccount;
  return latest && latest.pinned_account && accountById(c, latest.pinned_account) ? latest.pinned_account : '';
}

// Right-click menu on a tab (position and tab id live in ui.tabMenu).
// Model dropdown + mode toggle on a queued message; each change saves straight away.
function queuedOpts(c, t) {
  const agent = t.lane !== 'api' && t.lane !== 'assistant';
  const ms = c.d.models.filter(m => m.lane !== 'control').map(m => modelState(m, c.now));
  const cur = t.thread_id ? '' : (t.model_id || 'auto');
  const mode = modeInfo(t.permission_mode || 'acceptEdits');
  const picker = t.thread_id ? '' : `<select class="input input--sm qopt__model" data-qopt="model" data-id="${t.id}" aria-label="Model" data-noswipe>
    ${[['auto', 'Auto'], ...ms.map(m => [m.id, m.label])].map(([id, l]) => `<option value="${esc(id)}"${cur === id ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  const seg = agent ? `<div class="segmented mode-seg" role="group" aria-label="Mode" data-noswipe>${PERMISSION_MODES.map(o => `<button class="segmented__opt${mode.id === o.id ? ' is-on' : ''}" title="${esc(o.label + ' - ' + o.sub)}" aria-pressed="${mode.id === o.id}" data-action="queuedMode" data-id="${t.id}" data-v="${o.id}">${esc(o.short)}</button>`).join('')}</div>` : '';
  // Claude Code messages only: which account runs it (blank = the project's own, else the default).
  const accts = accountList(c), onAcct = t.pinned_account && accountById(c, t.pinned_account) ? t.pinned_account : '';
  const acct = agent && usesClaudeAccount(c, cur) && accts.length ? `<select class="input input--sm qopt__model" data-qopt="account" data-id="${t.id}" aria-label="Account" data-noswipe>
    ${[['', 'Auto account'], ...accts.map(a => [a.id, shortName(a)])].map(([id, l]) => `<option value="${esc(id)}"${onAcct === id ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select>` : '';
  return picker + acct + seg;
}

export function tabMenu(c) {
  const l = c.ui.linkMenu;
  if (l) return `<div class="tabmenu-back" data-key="tabmenu-back" data-action="closeTabMenu"></div>
  <div class="tabmenu" data-key="tabmenu" role="menu" style="left:${l.x}px;top:${l.y}px">
    <button role="menuitem" data-action="linkNewTab" data-url="${esc(l.url)}">Open in new tab</button>
  </div>`;
  const m = c.ui.tabMenu;
  if (!m || !c.desk) return '';
  return `<div class="tabmenu-back" data-key="tabmenu-back" data-action="closeTabMenu"></div>
  <div class="tabmenu" data-key="tabmenu" role="menu" style="left:${m.x}px;top:${m.y}px">
    ${m.url ? `<button role="menuitem" data-action="linkNewTab" data-url="${esc(m.url)}">Open in new tab</button>` : ''}
    <button role="menuitem" data-action="tabMenuPick" data-op="others" data-id="${m.id}"${c.ui.tabs.length < 2 ? ' disabled' : ''}>Close others</button>
    <button role="menuitem" data-action="tabMenuPick" data-op="all" data-id="${m.id}">Close all</button>
    <button role="menuitem" data-action="tabMenuPick" data-op="one" data-id="${m.id}">Close</button>
  </div>`;
}

// A short note just above the reply box ("Mode changed", "Approved", "Proceeding now") - app.js chatToast().
// Sits over the "New reply" button's spot, or just above it when that's showing.
function chatNote(n, rootId, jump) {
  if (!n || n.root !== rootId) return '';
  const K = KINDS[n.kind] || KINDS.info;
  return `<div class="chat-note${jump ? ' chat-note--up' : ''}${n.leaving ? ' is-leaving' : ''}" data-key="chat-note-${n.id}" role="status" style="--k:${K.c}">
    ${icon(K.icon, 14, { strokeWidth: 2 })}<span class="truncate">${esc(n.text)}</span>${n.actionLabel ? `<button class="chat-note__act" data-action="chatNoteAct">${esc(n.actionLabel)}</button>` : ''}</div>`;
}
