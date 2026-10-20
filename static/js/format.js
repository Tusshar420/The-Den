// Pure formatting + mapping helpers shared by every view. No DOM, no fetch.

// ---------------------------------------------------------------- time
export const secondsSince = (iso, now = Date.now()) => Math.max(0, (now - new Date(iso).getTime()) / 1000);

export function ago(iso, now) {
  const s = secondsSince(iso, now);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  if (s < 172800) return 'yesterday';
  return Math.round(s / 86400) + 'd ago';
}
export const agoShort = (iso, now) => ago(iso, now).replace(' ago', '');
export const startedAgo = (iso, now) => { const e = Math.floor(secondsSince(iso, now)); return 'started ' + (e < 60 ? e + 's' : Math.floor(e / 60) + 'm') + ' ago'; };
export const mmss = s => { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
export const hms = s => { s = Math.max(0, Math.floor(s)); return Math.floor(s / 3600) + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };
export const secs = ms => (ms == null ? '' : (ms / 1000).toFixed(1) + 's');                 // 2100 → "2.1s"
export const waitFmt = sec => { sec = Math.max(0, sec); return sec >= 3600 ? Math.floor(sec / 3600) + 'h ' + Math.floor(sec % 3600 / 60) + 'm' : Math.max(1, Math.ceil(sec / 60)) + 'm'; };

// ---------------------------------------------------------------- money / counts
export const money = n => '$' + n.toFixed(2);
export const fmtCost = n => (n == null ? '' : n < 0.01 ? '<$0.01' : '~$' + n.toFixed(2)); // estimated → "~" prefix
export const plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');
export const ktok = n => Math.round(n / 1000) + 'k tokens';
export function usageSummary(u) {
  if (!u || !u.tasks) return 'unused';
  return `${u.tasks} tasks · ${Math.round((u.done / u.tasks) * 100)}% ok · ${fmtCost(u.cost_usd)}`;
}

// ---------------------------------------------------------------- task name
// A task's display name: its chat's title (every reply carries its root's), else the
// prompt. Approval rows carry it as task_title.
export const taskName = t => (t && (t.title || t.task_title || t.prompt)) || '';

// How far along a task is. Its plan (Claude's TodoWrite list) when it wrote one, a step in progress
// counting half; otherwise, while running, time so far against how long runs in its project usually
// take (typical_ms, from /tasks) - eased from 80% so a long run slows down and a guess never says 100.
// null = nothing to go on (queued, no history, paused mid-guess).
// Rough time left as text ("~2m left"), or '' when it's too early to say.
const timeLeft = sec => (sec < 45 ? '<1m left' : sec < 90 ? '~1m left' : sec < 3600 ? `~${Math.round(sec / 60)}m left` : `~${Math.round(sec / 360) / 10}h left`);

export function taskProgress(t, now = Date.now()) {
  const steps = (t && t.plan_steps) || [];
  if (steps.length) {
    const done = steps.filter(s => s.status === 'completed').length;
    const doing = steps.some(s => s.status === 'in_progress') ? 0.5 : 0;
    const percent = t.status === 'done' ? 100 : Math.round(100 * Math.min(done + doing, steps.length) / steps.length);
    const cur = steps.find(s => s.status === 'in_progress');
    // With a plan, pace so far projects the rest - but only once a few steps have given it something to go on.
    const spent = t.started_at ? secondsSince(t.started_at, now) : 0;
    const left = t.status === 'running' && !t.paused && done >= 2 && percent > 0 && percent < 100 ? ' · ' + timeLeft(spent * (100 - percent) / percent) : '';
    return { percent, label: `${percent}% · ${done}/${steps.length}${left}`, estimate: false,
      title: `${done} of ${steps.length} plan steps done` + (cur ? ` - now: ${cur.content}` : '') };
  }
  if (!t || t.status !== 'running' || t.paused || !t.started_at || !t.typical_ms) return null;
  const r = secondsSince(t.started_at, now) * 1000 / t.typical_ms;
  const percent = Math.round(r < 0.8 ? 100 * r : 95 - 15 * Math.exp(-(r - 0.8) / 0.4));
  const remaining = t.typical_ms / 1000 - r * t.typical_ms / 1000;
  return { percent, label: '~' + percent + '%' + (r < 1 ? ' · ' + timeLeft(remaining) : ' · running long'), estimate: true,
    title: `Estimate - no plan yet. Runs here usually take about ${Math.max(1, Math.round(t.typical_ms / 60000))}m` };
}

// ---------------------------------------------------------------- task status
// Backend statuses: queued | running | done | error | needs_review | rate_limited | cancelled
// plus the UI-only 'approval' (a task with a pending row in /approvals).
export const STATUS = {
  running:      { label: 'Running',        color: 'var(--run)',       glyph: '·' },
  approval:     { label: 'Waiting on you', color: 'var(--wait)',      glyph: '·' },
  error:        { label: 'Failed',         color: 'var(--err)',       glyph: '✕' },
  needs_review: { label: 'Needs review',   color: 'var(--review)',    glyph: '◐' },
  rate_limited: { label: 'Rate-limited',   color: 'var(--sleep)',     glyph: '·' },
  queued:       { label: 'Queued',         color: 'var(--queued)',    glyph: '·' },
  done:         { label: 'Done',           color: 'var(--ok)',        glyph: '✓' },
  cancelled:    { label: 'Cancelled',      color: 'var(--cancelled)', glyph: '–' },
};
const PAUSED = { label: 'Paused', color: 'var(--text-meta)', glyph: '⏸' };
export const statusOf = t => (t.paused && ['queued', 'running', 'rate_limited'].includes(t.status) ? PAUSED : STATUS[t.status]) || { label: t.status, color: 'var(--text-meta)', glyph: '·' };

// Projects page quick filters → which statuses they match.
export const QUICK_FILTERS = [
  { key: 'all',     label: 'All',      statuses: null,                         dot: null },
  { key: 'pending', label: 'Pending',  statuses: ['approval', 'needs_review'], dot: 'var(--wait)' },
  { key: 'ongoing', label: 'Ongoing',  statuses: ['running', 'rate_limited'],  dot: 'var(--run)' },
  { key: 'queue',   label: 'In queue', statuses: ['queued'],                   dot: 'var(--queued)' },
  { key: 'failed',  label: 'Failed',   statuses: ['error'],                    dot: 'var(--err)' },
  { key: 'done',    label: 'Done',     statuses: ['done'],                     dot: 'var(--ok)' },
];

// UI priority labels ↔ backend integer (worker orders by priority DESC, created_at ASC).
export const PRIORITY = [{ value: 1, label: 'Urgent' }, { value: 0, label: 'Normal' }, { value: -1, label: 'Low' }];
export const priorityLabel = p => (p > 0 ? 'Urgent' : p < 0 ? 'Low' : 'Normal');

// ---------------------------------------------------------------- models / lanes
// lane "agent" = Claude Code (acts on the project). lane "api" = answer-only model.
export function modelChip(task, models) {
  const m = models.find(x => x.id === task.model_id) || { label: 'Claude Code', lane: task.lane || 'agent' };
  const lane = task.lane || m.lane || 'agent';
  return { label: m.label, lane, glyph: lane === 'agent' ? '›_' : lane === 'assistant' ? '✦' : '“', className: 'model-chip model-chip--' + lane };
}
export const modelChipHTML = c => `<span class="${c.className}"><span class="model-chip__glyph">${c.glyph}</span>${escapeHTML(c.label)}</span>`;

// GET /octopus/models row → dot color + muted status text for the Models panel and pickers.
export function modelState(m, now = Date.now()) {
  const until = m.rate_limited_until ? new Date(m.rate_limited_until).getTime() : 0;
  const rate = until > now;
  const available = m.enabled && m.available && !rate;
  let status;
  if (rate) status = 'rate limited · back in ' + waitFmt((until - now) / 1000);
  else if (!m.enabled) status = m.unavailable_reason || 'disabled in models.json';
  else if (!m.available) status = m.unavailable_reason || 'unavailable';
  else status = m.lane === 'agent' ? 'agent · runs in the project' : m.lane === 'assistant' ? 'tools · your email, calendar, reminders'
    : m.lane === 'control' ? 'instant · commands about Octopus' : m.modality === 'image' ? 'api · makes images' : 'api · answers only';
  const dot = rate ? 'var(--sleep)' : available ? 'var(--ok)' : 'var(--offline)';
  const pickerSuffix = rate ? ' (rate limited)' : !available ? ' (unavailable)' : '';
  return { ...m, rate, available, status, dot, pickerSuffix };
}

// Claude Code permission modes a task can run under (mirrors db.PERMISSION_MODES). A task with none runs as 'acceptEdits'.
export const PERMISSION_MODES = [
  { id: 'acceptEdits', label: 'Accept edits', short: 'Edits', sub: 'edits files on its own, asks before commands', dot: 'var(--ok)' },
  { id: 'plan', label: 'Plan', short: 'Plan', sub: 'reads and plans only, changes nothing', dot: 'var(--sleep)' },
  { id: 'auto', label: 'Auto', short: 'Auto', sub: 'Claude decides what is safe to run unasked', dot: 'var(--coral)' },
  { id: 'manual', label: 'Ask first', short: 'Ask', sub: 'asks before every edit and command', dot: 'var(--text)' },
];
export const modeInfo = id => PERMISSION_MODES.find(m => m.id === (id || 'acceptEdits')) || PERMISSION_MODES[0];
// The mode a chat last ran under - what its next reply starts on.
export const threadMode = tasks => ((tasks || []).slice().reverse().find(t => t.permission_mode) || {}).permission_mode || null;

// Who actually ran a chat / queue item, from what each of its tasks recorded (model_id, claude_account) -
// nothing is guessed. `msgs` are the chat's tasks. Only tasks that started count. Account applies to Claude Code
// (agent lane) tasks only: api-lane models don't use a Claude account. `unrecorded` = a Claude Code task ran but
// no account was stored (history from before accounts were tracked).
export function attribution(msgs) {
  const models = [], accounts = [];
  let unrecorded = false, claudeRan = false;
  for (const m of msgs || []) {
    if (!m.started_at) continue;
    if (m.model_id && !models.includes(m.model_id)) models.push(m.model_id);
    if ((m.lane || 'agent') !== 'agent') continue;
    claudeRan = true;
    if (!m.claude_account) unrecorded = true;
    else if (!accounts.includes(m.claude_account)) accounts.push(m.claude_account);
  }
  return { models, accounts, unrecorded, claudeRan };
}

// "Claude Code · GPT-6 Luna" / "Account: Work, Personal" - modelName / accountName turn ids into labels.
export function attributionParts(a, modelName, accountName) {
  const out = { models: a.models.map(modelName).join(' · '), account: '' };
  if (a.accounts.length) out.account = (a.accounts.length > 1 ? 'Accounts: ' : 'Account: ') + a.accounts.map(accountName).join(', ')
    + (a.unrecorded ? ' + unrecorded' : '');
  else if (a.claudeRan) out.account = 'Account not recorded';
  return out;
}

// Chat reply footer: "Done · 2m ago · GPT-6 Luna · 2.1s · <$0.01" (missing values are dropped)
export function replyFooter(task, modelLabel, now) {
  const s = statusOf(task);
  const rest = [task.finished_at ? ago(task.finished_at, now) : '', modelLabel, secs(task.duration_ms), fmtCost(task.cost_usd)].filter(Boolean).join(' · ');
  return { status: s.label, color: s.color, rest };
}
export const routeNote = t => (t.category === 'manual' ? 'Routed: ' + (t.route_reason || 'you picked this model') : 'Routed: ' + (t.category ? t.category + ': ' : '') + (t.route_reason || ''));

// Ask box success line pieces: → **GPT-6 Luna** · simple: short factual question · open #123
export const askSuccess = (decision, task) => ({ model: decision.model_label, lane: decision.lane, why: `${decision.category}: ${decision.reason}`, num: task.id });

// ---------------------------------------------------------------- greeting (splash)
const GREETINGS = {
  morning:   ['Good morning,',   ["Coffee's on you. The queue's on me.", 'I stretched all eight arms. Ready when you are.', "Fresh day, fresh diffs. Let's make something."]],
  afternoon: ['Good afternoon,', ['Back already? I missed you a little.', 'I snacked on some tests while you were out.', 'Halfway through the day and still no merge conflicts.']],
  evening:   ['Good evening,',   ["Queue something before bed. I'll take the night shift.", 'Long day? Hand me a few of those.', 'You did good today. I have receipts.']],
  night:     ['Up late,',        ["Octopuses don't really sleep, so I'm here.", "Tell me what to do, then go to bed. I've got it.", 'The quiet hours are my favourite. Fewer rate limits.']],
};
export function greeting({ name = 'Tushar', date = new Date(), playful = true, doneRecently = 0, needsYou = 0, firstRun = false, rand = Math.random() } = {}) {
  const h = date.getHours();
  const part = h >= 5 && h < 12 ? 'morning' : h >= 12 && h < 17 ? 'afternoon' : h >= 17 && h < 22 ? 'evening' : 'night';
  const [hello, lines] = GREETINGS[part];
  const recap = firstRun ? "first time here? let's set you up"
    : `while you were away: ${doneRecently} done · ` + (needsYou ? `${needsYou} need${needsYou === 1 ? 's' : ''} you` : 'nothing needs you');
  return {
    hello, name: part === 'night' ? name + '?' : name + '!',
    line: playful ? lines[Math.floor(rand * lines.length)] : 'Your workspace is loading.',
    recap, recapHasNeeds: needsYou > 0,
  };
}

// ---------------------------------------------------------------- misc
export function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
