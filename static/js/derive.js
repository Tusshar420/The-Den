// World state derived once per render: what Octopus is doing, what needs you,
// what's queued. Shared by the hero, sidebar, tab badge and Home.

import { store } from './api.js';
import { agoShort, mmss, hms, plural, secondsSince } from './format.js';
import { moodFor } from './octopus.js';
import { projectById, pendingApprovals, queuedTasks, realProjects, usageWindow } from './data.js';
import { clock } from './ui.js';

// The command an approval is asking about (Bash → its command line).
export function commandOf(a) {
  try {
    const x = JSON.parse(a.tool_input);
    if (x && typeof x === 'object') return String(x.command ?? x.url ?? x.file_path ?? JSON.stringify(x));
  } catch {}
  return a.tool_input || '';
}

const lowerFirst = s => s.charAt(0).toLowerCase() + s.slice(1);
const noEndPunct = s => s.replace(/[.!?…]+$/, '');

// Name of the thing an approval is about: "deploy.sh", "git", "WebFetch".
export function commandName(a) {
  const cmd = commandOf(a);
  if (a.tool_name !== 'Bash') return a.tool_name;
  const first = (cmd.trim().split(/\s+/)[0] || 'the command');
  return first.split('/').pop();
}

export function limitWindowWords(d = store.data) {
  const w = d.settings && d.settings.claude_usage && d.settings.claude_usage.limited_window;
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
    ...approvals.map(a => ({ kind: 'approval', approval: a, task: tasks.find(t => t.id === a.task_id) || { id: a.task_id, prompt: a.task_prompt, project_id: a.project_id, model_id: a.model_id, lane: a.lane } })),
    ...errors.map(t => ({ kind: 'error', task: t })),
    ...reviews.map(t => ({ kind: 'review', task: t })),
  ];
  const nq = queue.length;
  const schedules = (d.schedules || []).filter(s => s.enabled && s.next_run_at).sort((a, b) => a.next_run_at.localeCompare(b.next_run_at));
  const nextSched = schedules[0];
  const hasHistory = tasks.length > 0;

  let hero;
  const pname = t => (projectById(t.project_id, d) || {}).name || t.project_name || '?';
  const skipNote = pausedSkips.length ? ` Skipping ${pausedSkips.length} in ${pname(pausedSkips[0])} (paused).` : '';
  if (empty) {
    hero = { label: 'NO PROJECTS', color: 'var(--text-meta)', short: 'Setting up', line: 'Give me something to hold.', sub: '', actions: [] };
  } else if (approval) {
    const t = approvalTask || { prompt: approval.task_prompt, project_id: approval.project_id };
    hero = {
      label: 'WAITING ON YOU · ' + agoShort(approval.created_at, now).toUpperCase(), color: 'var(--wait)', short: 'Waiting on you',
      line: "I'm holding the queue until you answer.",
      sub: `${noEndPunct(t.prompt)} (${pname(t)}) needs a yes before I run ${commandName(approval)}. ` + (nq ? plural(nq, 'task') + ' waiting behind it.' : 'Nothing behind it.'),
      actions: [{ label: 'Review request', action: 'openApproval', id: approval.id, kind: 'approve' }],
    };
  } else if (rateLimited && rateLimited.resume_at) {
    const until = clock(rateLimited.resume_at);
    const left = (new Date(rateLimited.resume_at).getTime() - now) / 1000;
    const ping = d.settings && d.settings.notify_on_resume;
    hero = {
      label: 'RATE-LIMITED · RESUMES ' + until, color: 'var(--sleep)', short: 'Asleep until ' + until,
      line: `Out of juice. Napping until ${until}.`,
      sub: `Hit ${limitWindowWords(d).hit} partway through "${noEndPunct(rateLimited.prompt)}". I'll resume it on my own in ${hms(left)}` + (nq ? `, then run ${plural(nq, 'more task')}.` : '.'),
      actions: d.settings && d.settings.ntfy_topic ? [{ label: ping ? 'Will ping you ✓' : 'Ping me when back', action: 'togglePingBack' }] : [],
    };
  } else if (agentRun) {
    const el = agentRun.started_at ? secondsSince(agentRun.started_at, now) : 0;
    hero = {
      label: 'WORKING · ' + mmss(el), color: 'var(--run)', short: 'Working',
      line: `On it: ${lowerFirst(noEndPunct(agentRun.prompt))}.`,
      sub: `${agentRun.current_step || 'Getting started'} in ${pname(agentRun)}. ` + (nq ? plural(nq, 'task') + ' queued after this.' : 'Nothing queued after this.') + skipNote,
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
  const mood = moodFor({ empty, approval: !!approval, rateLimited: !!rateLimited, running: !!agentRun, allHeld: !!(nq && pausedSkips.length === nq), hasErrors: errors.length > 0 });

  return {
    approvals, apTask, approval, approvalTask, running, agentRun, rateLimited, queue, pausedSkips, errors, reviews,
    needs, nq, empty, hero, mood, schedules, nextSched,
    session: usageWindow('five_hour', d), week: usageWindow('seven_day', d),
    uiStatus: t => (apTask.has(t.id) ? 'approval' : t.status),
    isPaused: paused,
  };
}
