// World state derived once per render: what Octopus is doing, what needs you,
// what's queued. Shared by the hero, sidebar, tab badge and Home.

import { store } from './api.js';
import { agoShort, mmss, hms, plural, secondsSince, taskName } from './format.js';
import { moodFor } from './octopus.js';
import { projectById, pendingApprovals, queuedTasks, realProjects, usageWindow } from './data.js';
import { clock } from './ui.js';
import { kindWord } from './views/assistant-card.js';
import { accountById, accountList, restingUntil, shortName } from './views/accounts.js';

// The command an approval is asking about (Bash → its command line).
export function commandOf(a) {
  try {
    const x = JSON.parse(a.tool_input);
    // Personal-assistant approvals carry a human sentence ("Send an email to rahul@example.com: Proposal update").
    if (x && typeof x === 'object' && x.describe) return String(x.describe);
    if (x && typeof x === 'object') return String(x.command ?? x.url ?? x.file_path ?? JSON.stringify(x));
  } catch {}
  return a.tool_input || '';
}

const lowerFirst = s => s.charAt(0).toLowerCase() + s.slice(1);
const noEndPunct = s => s.replace(/[.!?…]+$/, '');

// Name of the thing an approval is about: "deploy.sh", "git", "WebFetch".
export function commandName(a) {
  const cmd = commandOf(a);
  if (String(a.tool_name).startsWith('assistant:')) return kindWord(a);
  if (a.tool_name !== 'Bash') return a.tool_name;
  const first = (cmd.trim().split(/\s+/)[0] || 'the command');
  return first.split('/').pop();
}

// accountId: that Claude account's limit (default: the default account's).
export function limitWindowWords(d = store.data, accountId = null) {
  const acct = accountId && d.settings && (d.settings.claude_accounts || []).find(a => a.id === accountId);
  const usage = acct ? acct.usage : d.settings && d.settings.claude_usage;
  const w = usage && usage.limited_window;
  return w === 'five_hour' ? { hit: 'the 5-hour session limit', full: '5-hour session window is full.' }
    : w === 'seven_day' ? { hit: 'the weekly limit', full: 'The weekly limit is used up.' }
    : { hit: 'the usage limit', full: 'The usage limit is reached.' };
}

export function derive(d = store.data, now = Date.now()) {
  const approvals = pendingApprovals(d);
  const apTask = new Map(approvals.map(a => [a.task_id, a]));
  const tasks = d.tasks;
  const empty = d.loaded && realProjects(d).length === 0;

  const approval = approvals[0] || null;
  const approvalTask = approval ? tasks.find(t => t.id === approval.task_id) : null;
  const running = tasks.filter(t => t.status === 'running').sort((a, b) => (a.lane === 'agent' ? 0 : 1) - (b.lane === 'agent' ? 0 : 1));
  const agentRun = running.find(t => t.lane !== 'api' && !apTask.has(t.id)) || null;
  const rateLimited = tasks.filter(t => t.status === 'rate_limited').sort((a, b) => (a.lane === 'agent' ? 0 : 1) - (b.lane === 'agent' ? 0 : 1))[0] || null;
  const queue = queuedTasks(d);
  const paused = t => { const p = projectById(t.project_id, d); return p && !p.active; };
  const pausedSkips = queue.filter(paused);
  const errors = tasks.filter(t => t.status === 'error' && !t.dismissed);
  const reviews = tasks.filter(t => t.status === 'needs_review');
  const needs = [
    ...approvals.map(a => ({ kind: 'approval', approval: a, task: tasks.find(t => t.id === a.task_id) || { id: a.task_id, prompt: a.task_prompt, title: a.task_title, project_id: a.project_id, model_id: a.model_id, lane: a.lane } })),
    ...errors.map(t => ({ kind: 'error', task: t })),
    ...reviews.map(t => ({ kind: 'review', task: t })),
  ];
  const nq = queue.length;
  const schedules = (d.schedules || []).filter(s => s.enabled && s.next_run_at).sort((a, b) => a.next_run_at.localeCompare(b.next_run_at));
  const nextSched = schedules[0];
  const hasHistory = tasks.length > 0;

  let hero;
  const limitedName = t => { const a = accountById({ d }, t.claude_account); return a ? shortName(a) : 'its Claude account'; };
  const pname = t => (projectById(t.project_id, d) || {}).name || t.project_name || '?';
  const skipNote = pausedSkips.length ? ` Skipping ${pausedSkips.length} in ${pname(pausedSkips[0])} (paused).` : '';
  if (empty) {
    hero = { label: 'NO PROJECTS', color: 'var(--text-meta)', short: 'Setting up', line: 'Give me something to hold.', sub: '', actions: [] };
  } else if (approval) {
    const t = approvalTask || { prompt: approval.task_prompt, title: approval.task_title, project_id: approval.project_id };
    hero = {
      label: 'WAITING ON YOU · ' + agoShort(approval.created_at, now).toUpperCase(), color: 'var(--wait)', short: 'Waiting on you',
      line: "I'm holding the queue until you answer.",
      sub: `${noEndPunct(taskName(t))} (${pname(t)}) needs a yes before I ${String(approval.tool_name).startsWith('assistant:') ? 'act on' : 'run'} ${commandName(approval)}. ` + (nq ? plural(nq, 'task') + ' waiting behind it.' : 'Nothing behind it.'),
      actions: [{ label: 'Review request', action: 'openApproval', id: approval.id, kind: 'approve' }],
    };
  } else if (rateLimited && rateLimited.resume_at && !agentRun) {
    const until = clock(rateLimited.resume_at);
    const left = (new Date(rateLimited.resume_at).getTime() - now) / 1000;
    const ping = d.settings && d.settings.notify_on_resume;
    // Only the account the task is on is at its limit - name it, and the ones that can still run.
    const acct = accountById({ d }, rateLimited.claude_account);
    const who = acct ? shortName(acct) : '';
    const free = accountList({ d }).filter(a => a !== acct && !restingUntil(a) && !a.auth_error).map(shortName);
    hero = {
      label: 'RATE-LIMITED · ' + (who ? who.toUpperCase() + ' · ' : '') + 'RESUMES ' + until, color: 'var(--sleep)',
      short: (who ? who + ' at limit' : 'Asleep') + ' until ' + until,
      line: free.length ? `${who || 'One account'} is out of juice until ${until}. ${free.join(', ')} still ${free.length === 1 ? 'has' : 'have'} room.` : `Out of juice. Napping until ${until}.`,
      sub: `${who ? who + ' hit' : 'Hit'} ${limitWindowWords(d, rateLimited.claude_account).hit} partway through "${noEndPunct(taskName(rateLimited))}". I'll resume it on ${who || 'the same account'} on my own in ${hms(left)}`
        + (free.length ? `. Tasks on ${free.join(', ')} can run right now.` : nq ? `, then run ${plural(nq, 'more task')}.` : '.'),
      actions: d.settings && d.settings.push_configured ? [{ label: ping ? 'Will ping you ✓' : 'Ping me when back', action: 'togglePingBack' }] : [],
    };
  } else if (agentRun) {
    const el = agentRun.started_at ? secondsSince(agentRun.started_at, now) : 0;
    hero = {
      label: 'WORKING · ' + mmss(el), color: 'var(--run)', short: 'Working',
      line: `On it: ${lowerFirst(noEndPunct(taskName(agentRun)))}.`,
      sub: `${agentRun.current_step || 'Getting started'} in ${pname(agentRun)}. ` + (nq ? plural(nq, 'task') + ' queued after this.' : 'Nothing queued after this.') + skipNote
        + (rateLimited && rateLimited.resume_at ? ` ${pname(rateLimited)} waits for ${limitedName(rateLimited)}'s limit to reset at ${clock(rateLimited.resume_at)}.` : ''),
      actions: [{ label: 'Watch', action: 'openChat', id: agentRun.id }],
    };
  } else if (nq && pausedSkips.length === nq) {
    const p = projectById(pausedSkips[0].project_id, d);
    hero = {
      label: 'IDLE · QUEUE HELD', color: 'var(--text-meta)', short: 'Idle, queue held',
      line: "Nothing I'm allowed to touch.",
      sub: `The ${plural(nq, 'task')} left belong to paused projects. Resume a project to let me pick them up.`,
      actions: p ? [{ label: 'Open ' + p.name, action: 'openProject', id: p.id }] : [],
    };
  } else {
    const morning = (d.recipes || []).find(r => r.name === 'Morning status check');
    hero = {
      label: 'IDLE', color: 'var(--ok)', short: 'All clear',
      line: hasHistory ? 'All clear. Tentacles free.' : 'Ready. Your first task is two taps away.',
      sub: (errors.length ? plural(errors.length, 'failed task') + ' still ' + (errors.length === 1 ? 'needs' : 'need') + ' a look. ' : '')
        + (nextSched ? `Next scheduled: ${nextSched.label} at ${clock(nextSched.next_run_at)}.` : 'Nothing scheduled.'),
      actions: [{ label: 'New task', action: 'openPalette', kind: 'primary' },
        ...(morning ? [{ label: 'Morning status check', action: 'runRecipe', id: morning.id }] : [])],
    };
  }
  // "Going to sleep": an overlay on whatever the hero says, since it changes how the queue runs, not what it is doing.
  const sm = d.settings && d.settings.sleep_mode;
  const asleep = !!(sm && sm.until && new Date(sm.until).getTime() > now);
  if (!empty) {
    if (asleep) {
      const done = tasks.filter(t => t.status === 'done' && t.finished_at && new Date(t.finished_at) >= new Date(sm.since)).length;
      hero.label += ' · SLEEP MODE';
      hero.sub = `Sleep mode until ${clock(sm.until)}: I'll keep going through the queue and use every bit of the limits. `
        + (done ? plural(done, 'task') + ' finished so far. ' : '') + (hero.sub || '');
    }
    hero.actions = [...hero.actions, { label: asleep ? "I'm awake" : 'Going to sleep', action: 'toggleSleep' }];
  }
  const mood = moodFor({ empty, approval: !!approval, rateLimited: !!rateLimited, running: !!agentRun, allHeld: !!(nq && pausedSkips.length === nq), hasErrors: errors.length > 0 });

  return {
    approvals, apTask, approval, approvalTask, running, agentRun, rateLimited, queue, pausedSkips, errors, reviews,
    needs, nq, empty, hero, mood, schedules, nextSched,
    session: usageWindow('five_hour', d), week: usageWindow('seven_day', d),
    uiStatus: t => (apTask.has(t.id) ? 'approval' : t.status),
    isPaused: paused,
  };
}
