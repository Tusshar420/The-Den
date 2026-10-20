// Personal assistant: Email, Calendar, Reminders, Connections. This is Octopus's activity and
// control panel for your accounts - not a mail or calendar client. Everything shown comes from
// /assistant/*; no credential ever reaches the browser (the server only sends account names,
// statuses and what services were granted).

import { pendingApprovals } from '../data.js';
import { agoShort } from '../format.js';
import { esc, dayWord, clock, sectionHead } from '../ui.js';
import { assistantCard, isAssistant } from './assistant-card.js';

export const PERSONAL_TABS = [['email', 'Email'], ['calendar', 'Calendar'], ['reminders', 'Reminders'], ['connections', 'Connections']];

// ---------------------------------------------------------------- formatting
const t12 = iso => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const dayHead = iso => {
  const d = new Date(iso), w = dayWord(iso);
  const date = d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  return w === 'today' ? 'Today · ' + date : w === 'tomorrow' ? 'Tomorrow · ' + date : date;
};
const span = e => (e.all_day ? 'All day' : `${t12(e.start)} – ${t12(e.end)}`);

const STATUS = {
  connected: ['var(--ok)', 'Connected'],
  reauth_required: ['var(--wait)', 'Reconnect needed'],
  disconnected: ['var(--text-meta)', 'Disconnected'],
  not_configured: ['var(--wait)', 'Not configured'],
  paid_blocked: ['var(--err)', 'Paid - not enabled'],
  unavailable: ['var(--text-faint)', 'Not available yet'],
};
const TAG = { connected: null, reauth_required: 'RECONNECT', disconnected: 'DISCONNECTED', not_configured: 'NOT CONFIGURED',
  paid_blocked: 'PAID', unavailable: 'COMING SOON' };
const SERVICE_NAMES = { email: 'Gmail', calendar: 'Calendar' };
const isLocal = () => ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);

const TOOL_LABEL = { email: 'Email', calendar: 'Calendar', reminders: 'Reminders', agent: 'Coding agent', connections: 'Connections' };
const ACTION_WORDS = {
  search: 'searched', read: 'opened a message', find_contact: 'looked up a contact', create_draft: 'created a draft', send: 'sent',
  reply: 'replied', forward: 'forwarded', send_draft: 'sent a draft', list_events: 'read the calendar', check_availability: 'checked availability',
  find_slots: 'looked for free slots', create_event: 'created an event', update_event: 'changed an event', cancel_event: 'cancelled an event',
  create: 'set a reminder', list: 'listed reminders', cancel: 'cancelled a reminder', code: 'handed a task to Claude Code',
  connect: 'connected an account', disconnect: 'disconnected an account', refresh: 'refreshed a sign-in',
};
const APPROVAL_BADGE = { requested: ['asked you', 'var(--wait)'], approved: ['you approved', 'var(--ok)'], denied: ['denied', 'var(--err)'],
  expired: ['expired', 'var(--text-meta)'], trusted: ['trusted', 'var(--review)'] };
const RESULT_BADGE = { ok: ['done', 'var(--ok)'], error: ['failed', 'var(--err)'], pending: ['waiting', 'var(--wait)'], denied: ['blocked', 'var(--err)'] };

// ---------------------------------------------------------------- pieces
const badge = ([text, color]) => `<span class="pa-badge" style="--c:${color}">${esc(text)}</span>`;

// "sent" is wrong for a row that only records the request to send, so unfinished approvals read as such.
const INFINITIVE = { send: 'send an email', reply: 'send a reply', forward: 'forward an email', send_draft: 'send a draft',
  create_event: 'create an event', update_event: 'change an event', cancel_event: 'cancel an event', code: 'hand a task to Claude Code' };
function actionLine(r) {
  const inf = INFINITIVE[r.action] || r.action;
  if (r.approval === 'requested') return `asked your approval to ${inf}`;
  if (r.approval === 'denied' && r.result === 'denied') return `did not ${inf} (declined or blocked)`;
  if (r.approval === 'expired') return `approval expired, did not ${inf}`;
  return ACTION_WORDS[r.action] || r.action;
}

export function activityRow(r) {
  const ap = r.approval !== 'not_required' ? APPROVAL_BADGE[r.approval] : null;
  return `<div class="pa-act" data-key="act${r.id}">
    <span class="pa-act__t">${esc(clock(r.created_at))}</span>
    <div class="pa-act__b">
      <span class="pa-act__line"><b>${esc(TOOL_LABEL[r.tool] || r.tool)}</b> ${esc(actionLine(r))}</span>
      ${r.target ? `<span class="meta truncate">${esc(r.target)}</span>` : ''}
      ${r.error ? `<span class="pa-act__err">${esc(r.error)}</span>` : ''}
    </div>
    <span class="pa-badges">${ap ? badge(ap) : ''}${badge(RESULT_BADGE[r.result] || [r.result, 'var(--text-meta)'])}</span>
  </div>`;
}

const activityList = (rows, empty) => (rows && rows.length
  ? `<div class="list">${rows.map(activityRow).join('')}</div>` : `<div class="empty">${esc(empty)}</div>`);

function approvalsSection(c, kind) {
  const list = pendingApprovals(c.d).filter(a => isAssistant(a) && a.tool_name.startsWith('assistant:' + kind + '.'));
  return `<div class="section">${sectionHead('Waiting for your approval', '', list.length || '')}
    ${list.length ? `<div class="pa-cards">${list.map(a => assistantCard(c, a)).join('')}</div>` : `<div class="empty"><span>Nothing is waiting on you.</span></div>`}
  </div>`;
}

function connectionStrip(c, info, service) {
  const conn = info && info.connection;
  if (conn) {
    const [color, word] = STATUS[conn.status];
    const more = (conn.accounts || []).length - 1;
    return `<div class="pa-strip"><span class="dot" style="--c:${color}"></span><span><b>${esc(conn.label)}</b> · ${esc(conn.account || 'account')}${more > 0 ? ` <span class="meta">(default · +${more} more)</span>` : ''}</span>
      <span class="pa-strip__state">${esc(word)}</span>${conn.status === 'reauth_required' ? `<button class="btn btn--sm btn--primary" data-action="goPersonal" data-tab="connections">Reconnect</button>` : ''}</div>`;
  }
  const what = service === 'email' ? 'email account' : 'calendar';
  return `<div class="pa-strip pa-strip--off"><span class="dot"></span><span>No ${what} connected yet.</span>
    <button class="btn btn--sm btn--primary" data-action="goPersonal" data-tab="connections">Connect</button></div>`;
}

const askHint = text => `<div class="pa-hint"><span>Just ask Octopus: <i>${esc(text)}</i></span><button class="btn btn--sm btn--ghost" data-action="openPalette">Ask</button></div>`;

// ---------------------------------------------------------------- tabs
function emailTab(c) {
  const e = c.d.personal.email;
  if (!e) return loading();
  return `${connectionStrip(c, e, 'email')}
    ${approvalsSection(c, 'email')}
    <div class="section">${sectionHead('Drafts Octopus created', e.drafts.length ? `<a class="btn--text" href="https://mail.google.com/mail/u/${e.connection && e.connection.account ? '?authuser=' + encodeURIComponent(e.connection.account) : '0/'}#drafts" target="_blank" rel="noopener">Open drafts in Gmail</a>` : '', e.drafts.length || '')}
      ${e.drafts.length ? `<div class="list">${e.drafts.map(d => `<div class="pa-act" data-key="dr${d.id}"><span class="pa-act__t">${esc(clock(d.created_at))}</span>
        <div class="pa-act__b"><span class="pa-act__line">${esc(d.target || 'Draft')}</span><span class="meta">${esc(agoShort(d.created_at))} ago · not sent</span></div></div>`).join('')}</div>`
        : `<div class="empty"><span>No drafts yet. Nothing Octopus writes is sent without you.</span></div>`}
    </div>
    <div class="section">${sectionHead('Recent email activity')}${activityList(e.activity, 'No email activity yet.')}</div>
    ${askHint('Find the latest email from Rahul')}`;
}

function calendarTab(c) {
  const k = c.d.personal.calendar;
  if (!k) return loading();
  const groups = [];
  for (const ev of k.upcoming) {
    const key = new Date(ev.start).toDateString();
    const g = groups.find(x => x.key === key) || groups[groups.push({ key, head: dayHead(ev.start), rows: [] }) - 1];
    g.rows.push(ev);
  }
  // With several calendars connected, say which one each event is on.
  const many = k.connection && (k.connection.accounts || []).length > 1;
  const evRow = ev => `<div class="pa-ev" data-key="ev${esc(ev.id)}"><span class="pa-ev__t">${esc(span(ev))}</span>
    <div class="pa-ev__b"><span class="pa-ev__title truncate">${ev.recurring ? '<span aria-label="Repeats" title="Repeats">↻</span> ' : ''}${esc(ev.title)}</span>${ev.attendees.length ? `<span class="meta truncate">with ${esc(ev.attendees.slice(0, 3).join(', '))}${ev.attendees.length > 3 ? ' +' + (ev.attendees.length - 3) : ''}</span>` : ''}${many && ev.account ? `<span class="meta truncate">${esc(ev.account)}</span>` : ''}</div></div>`;
  return `${connectionStrip(c, k, 'calendar')}
    ${k.error ? `<div class="pa-warn pa-warn--block"><span>!</span>${esc(k.error)}</div>` : ''}
    ${approvalsSection(c, 'calendar')}
    <div class="section">${sectionHead('Today', '', k.today.length || '')}
      ${k.today.length ? `<div class="list">${k.today.map(evRow).join('')}</div>` : `<div class="empty"><span>${k.connection ? 'Nothing on your calendar today.' : 'Connect a calendar to see today here.'}</span></div>`}
    </div>
    <div class="section">${sectionHead('Coming up · next 7 days')}
      ${groups.length ? groups.map(g => `<div class="section" data-key="cg${esc(g.key)}"><span class="act-group__label">${esc(g.head)}</span><div class="list">${g.rows.map(evRow).join('')}</div></div>`).join('')
        : `<div class="empty"><span>Nothing coming up.</span></div>`}
    </div>
    <div class="section">${sectionHead('Recent calendar activity')}${activityList(k.activity, 'No calendar activity yet.')}</div>
    ${askHint('Schedule a meeting with Rahul tomorrow at 3')}`;
}

function remindersTab(c) {
  const rows = c.d.personal.reminders;
  const st = c.d.personal.status;
  const pending = rows.filter(r => r.status === 'pending'), fired = rows.filter(r => r.status === 'fired').slice(0, 8);
  const row = (r, live) => `<div class="pa-ev" data-key="rm${r.id}"><span class="pa-ev__t">${esc(new Date(r.due_at).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }))}</span>
    <div class="pa-ev__b"><span class="pa-ev__title">${esc(r.text)}</span></div>
    ${live ? `<button class="btn btn--sm btn--ghost" data-action="cancelReminder" data-id="${r.id}">Cancel</button>` : `<span class="meta">rang ${esc(agoShort(r.fired_at || r.due_at))} ago</span>`}</div>`;
  return `${st && !st.push_configured ? `<div class="pa-warn pa-warn--block"><span>!</span>Phone notifications aren't set up (ACTIVITYSMITH_API_KEY is not set on the service), so reminders only show here.</div>` : ''}
    <div class="section">${sectionHead('Coming up', '', pending.length || '')}
      ${pending.length ? `<div class="list">${pending.map(r => row(r, true)).join('')}</div>` : `<div class="empty"><span>No reminders set.</span></div>`}
    </div>
    ${fired.length ? `<div class="section">${sectionHead('Recently rang')}<div class="list">${fired.map(r => row(r, false)).join('')}</div></div>` : ''}
    ${askHint('Remind me tomorrow morning to call Rahul')}`;
}

const svcChips = (services, granted) => services.map(s => `<span class="pa-svc${granted.includes(s) ? ' is-on' : ''}">${SERVICE_NAMES[s] || s}</span>`).join('');

// One connected account of a provider. With more than one, each says what it's the default for
// (what Octopus uses when you don't name an account) and can be made the default.
function accountRow(c, p, a, many) {
  const [color, word] = STATUS[a.status] || STATUS.disconnected;
  const dis = c.ui.busy['conn-' + a.id] ? ' disabled' : '';
  const defaults = many ? a.default_for.map(s => `<span class="pa-tag" style="--c:var(--ok)">DEFAULT FOR ${(SERVICE_NAMES[s] || s).toUpperCase()}</span>`).join('') : '';
  const makeDefault = many ? a.services_granted.filter(s => !a.default_for.includes(s)).map(s =>
    `<button class="btn btn--sm btn--ghost" data-action="assistDefault" data-id="${esc(a.id)}" data-service="${s}"${dis}>Default for ${SERVICE_NAMES[s] || s}</button>`).join('') : '';
  return `<div class="pa-acct" data-key="pacct-${esc(a.id)}">
    <div class="pa-acct__top"><span class="dot" style="--c:${color}"></span><b class="truncate">${esc(a.account || 'Account')}</b>${a.status !== 'connected' ? `<span class="meta">${esc(word)}</span>` : ''}${defaults}</div>
    <span class="pa-svcs">${svcChips(p.services, a.services_granted)}</span>
    ${a.last_error && a.status !== 'connected' ? `<span class="pa-act__err">${esc(a.last_error)}</span>` : ''}
    <div class="pa-conn__acts">${a.status === 'reauth_required' ? `<button class="btn btn--sm btn--primary" data-action="assistConnect" data-id="${p.id}" data-account="${esc(a.id)}"${dis}>Reconnect</button>` : ''}${makeDefault}
      <button class="btn btn--sm btn--ghost" data-action="assistDisconnect" data-id="${esc(a.id)}" data-name="${esc(a.account || p.label)}"${dis}>Disconnect</button></div>
  </div>`;
}

function connectionCard(c, p) {
  const [color, word] = STATUS[p.status] || STATUS.disconnected;
  const busy = c.ui.busy['conn-' + p.id];
  const accounts = p.accounts || [];
  let actions = '';
  if (accounts.length && p.can_add) actions = `<button class="btn btn--ghost btn--44" data-action="assistConnect" data-id="${p.id}"${busy ? ' disabled' : ''}>+ Add another ${esc(p.label)} account</button>`;
  else if (p.status === 'disconnected') actions = `<button class="btn btn--primary btn--44" data-action="assistConnect" data-id="${p.id}"${busy ? ' disabled' : ''}>Connect ${esc(p.label)}</button>`;
  const setup = p.status === 'not_configured'
    ? `<div class="pa-setup"><span class="eyebrow">TO SET UP (FREE)</span><ol>${p.setup_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>
        <span class="meta">Needs ${p.env.map(esc).join(' and ')} · redirect ${esc(p.redirect_uri)}</span></div>` : '';
  const note = p.status === 'unavailable' ? `<span class="meta">${esc(p.note)}</span>`
    : p.status === 'paid_blocked' ? `<span class="meta">This provider needs a paid plan. Nothing is enabled until you switch it on yourself.</span>` : '';
  const localNote = ['disconnected', 'reauth_required'].includes(p.status) && !isLocal()
    ? `<span class="meta">Sign-in returns to ${esc(p.redirect_uri.replace(/\/assistant.*/, ''))}, so finish it in a browser on the Mac itself.</span>` : '';
  return `<div class="pa-conn" data-key="conn-${p.id}">
    <div class="pa-conn__head"><b>${esc(p.label)}</b><span class="pa-tag pa-tag--${p.cost}" title="${esc(p.cost_note)}">${p.cost === 'paid' ? 'PAID' : 'FREE'}</span>${TAG[p.status] ? `<span class="pa-tag" style="--c:${color}">${TAG[p.status]}</span>` : ''}</div>
    ${accounts.length ? accounts.map(a => accountRow(c, p, a, accounts.length > 1)).join('') : `
    <div class="pa-kv"><span>Account</span><span>-</span></div>
    <div class="pa-kv"><span>Status</span><span><span class="dot" style="--c:${color}"></span> ${esc(word)}</span></div>
    <div class="pa-kv"><span>Access</span><span class="pa-svcs">${svcChips(p.services, [])}</span></div>`}
    ${setup}${note}${localNote}
    ${actions ? `<div class="pa-conn__acts">${actions}</div>` : ''}
  </div>`;
}

const POLICY_LABEL = { allow: 'Allow', ask: 'Ask', deny: 'Deny' };
function policyRow(p) {
  const trusted = p.policy === 'allow' && p.default !== 'allow';
  return `<div class="pa-pol" data-key="pol-${p.capability}">
    <div class="pa-pol__b"><span class="pa-pol__name">${esc(p.label)}</span><span class="meta">${esc(p.capability)} · ${trusted ? 'trusted - runs without asking' : esc(p.help)}</span></div>
    <div class="pa-seg" role="radiogroup" aria-label="${esc(p.label)}" data-noswipe>${['allow', 'ask', 'deny'].map(v =>
      `<button class="pa-seg__b pa-seg__b--${v}${p.policy === v ? ' is-on' : ''}" role="radio" aria-checked="${p.policy === v}" data-action="setPolicy" data-cap="${p.capability}" data-policy="${v}">${POLICY_LABEL[v]}</button>`).join('')}</div>
  </div>`;
}

function connectionsTab(c) {
  const st = c.d.personal.status;
  if (!st) return loading();
  const pl = st.planner || {};
  return `<div class="section">${sectionHead('Accounts')}
      <span class="pa-note">Octopus signs in with OAuth in your own browser - it never sees or stores a password. Gmail and Google Calendar are separate from any ChatGPT, Claude or Gemini subscription.
        Connect as many Google accounts as you like: Octopus reads all of them, replies from the account that got the message, and uses the default for anything new - or say which, e.g. <i>"email Rahul from my work account"</i>.</span>
      <div class="pa-conns">${st.connections.map(p => connectionCard(c, p)).join('')}</div>
    </div>
    <div class="section">${sectionHead('Understanding requests')}
      <div class="pa-kvcard"><div class="pa-kv"><span>Plain lookups</span><span>No model - direct API calls</span></div>
        <div class="pa-kv"><span>Drafting &amp; planning</span><span>${pl.available ? `<span class="dot" style="--c:var(--ok)"></span> ${esc(pl.label)}${pl.note ? ' · ' + esc(pl.note) : ''}` : `<span class="dot" style="--c:var(--err)"></span> ${esc(pl.reason || 'No model available')}`}</span></div>
        <div class="pa-kv"><span>Time zone</span><span>${esc(st.timezone)}</span></div></div>
    </div>
    <div class="section">${sectionHead('Permissions')}
      <span class="pa-note">What Octopus may do on its own. <b>Ask</b> parks the action as an approval card until you say yes. Anything that can email other people or change your calendar asks by default.</span>
      <div class="list pa-pols">${st.policies.map(policyRow).join('')}</div>
    </div>
    <div class="section">${sectionHead('Activity log')}${activityList(c.d.personal.activity, 'Nothing has happened yet.')}</div>`;
}

const loading = () => `<div class="empty"><span>Loading…</span></div>`;

// ---------------------------------------------------------------- page
export function personal(c) {
  const tab = c.ui.personalTab;
  const pending = pendingApprovals(c.d).filter(isAssistant);
  const badgeFor = k => pending.filter(a => a.tool_name.startsWith('assistant:' + k + '.')).length;
  const body = { email: emailTab, calendar: calendarTab, reminders: remindersTab, connections: connectionsTab }[tab] || emailTab;
  return `<div class="page page--narrow page--personal" data-key="page-personal">
    <div class="chip-row pa-tabs" role="tablist" data-noswipe>${PERSONAL_TABS.map(([k, l]) => {
      const n = badgeFor(k);
      return `<button class="chip${tab === k ? ' is-on' : ''}" role="tab" aria-selected="${tab === k}" data-action="goPersonal" data-tab="${k}" data-key="pt-${k}">${l}${n ? ` <span class="count-badge count-badge--sm">${n}</span>` : ''}</button>`;
    }).join('')}</div>
    ${body(c)}
  </div>`;
}

