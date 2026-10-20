// Claude accounts (accounts.py): which subscription Claude Code runs on. The header has a quick
// switch of the default account (beside the usage limits) with its details and limits; Settings › Claude account lists every
// account with its own limits and projects, and adds or removes one (a token from `claude setup-token`).
// This Mac's login can be removed from the list (hidden) once there's a token account, and brought back.
// A project can have its own account (its Settings tab - projectAccountCard): its tasks always run on
// that one and wait out that account's limit rather than move to another.

import { esc, clock } from '../ui.js';
import { icon } from '../icons.js';

const PLAN = { team: 'Team', pro: 'Pro', max: 'Max', enterprise: 'Enterprise', free: 'Free' };

export const accountList = c => (c.d.settings && c.d.settings.claude_accounts) || [];
export const activeAccount = c => accountList(c).find(a => a.active) || null;
export const shortName = a => a.alias || (a.kind === 'mac' ? (a.org || 'Mac login') : a.label);
export const accountById = (c, id) => accountList(c).find(a => a.id === id) || null;
// When this account's usage limit resets, if it's waiting one out now.
export const restingUntil = a => (a && a.limited_until && new Date(a.limited_until).getTime() > Date.now() ? a.limited_until : null);
const resting = a => restingUntil(a)
  ? `<span class="acct-rest">At its usage limit until ${clock(a.limited_until)} - its projects wait for it, the others keep going.</span>` : '';

// { pct, resets, stale } for one usage window of one account, or null if unknown.
function win(a, name) {
  const w = a.usage && a.usage.windows && a.usage.windows[name];
  if (!w || w.utilization == null) return null;
  const resets = w.resets_at ? new Date(w.resets_at) : null;
  if (resets && resets.getTime() < Date.now()) return { pct: 0, resets: null };
  return { pct: Math.max(0, Math.min(100, Math.round(w.utilization * 100))), resets };
}

const when = (d, weekly) => !d ? '' : weekly
  ? d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

const barColor = pct => (pct >= 90 ? 'var(--err)' : pct >= 70 ? 'var(--wait)' : 'var(--ok)');

function limitBar(label, w, weekly) {
  if (!w) return `<div class="acct-bar"><span class="acct-bar__l">${label}</span><span class="bar6"></span><span class="meta">not known yet</span></div>`;
  return `<div class="acct-bar"><span class="acct-bar__l">${label}</span>
    <span class="bar6"><span style="width:${w.pct}%;background:${barColor(w.pct)};display:block;height:100%"></span></span>
    <span class="meta">${w.pct}% used${w.resets ? ` · resets ${when(w.resets, weekly)}` : ''}</span></div>`;
}

const limits = a => limitBar('5-hour limit', win(a, 'five_hour'), false) + limitBar('Weekly limit', win(a, 'seven_day'), true) + lastHit(a);

// "Last hit its limit Thu 9:28 - 5-hour 100%, weekly 26% used" (what each window showed then).
function lastHit(a) {
  const h = a.last_hit;
  if (!h || !h.at) return '';
  const d = new Date(h.at);
  const p = v => (v == null ? null : Math.round(v * 100) + '%');
  const bits = [p(h.five_hour) && `5-hour ${p(h.five_hour)}`, p(h.seven_day) && `weekly ${p(h.seven_day)}`].filter(Boolean);
  return `<span class="meta">Last hit its limit ${d.toLocaleDateString([], { weekday: 'short' })} ${when(d)}${bits.length ? ' · ' + bits.join(', ') + ' used' : ''}</span>`;
}

const checkedAt = a => (a.usage && a.usage.updated_at ? new Date(a.usage.updated_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '');

function details(a, withChecked = true) {
  const bits = [a.email, a.kind === 'mac' ? a.org : null, a.plan ? (PLAN[a.plan] || a.plan) + ' plan' : null].filter(Boolean);
  const checked = withChecked && checkedAt(a) ? ` · limits checked ${checkedAt(a)}` : '';
  return esc(bits.join(' · ') || (a.kind === 'mac' ? 'the account Claude Code is logged into on this Mac' : 'saved token')) + checked;
}

// A plain heads-up when a limit is nearly used up (and the account isn't already resting).
function nearLimit(a) {
  if (restingUntil(a)) return '';
  const w5 = win(a, 'five_hour'), w7 = win(a, 'seven_day');
  const hit = [[w5, 'the 5-hour limit', false], [w7, 'the weekly limit', true]].find(([w]) => w && w.pct >= 90);
  if (!hit) return '';
  const [w, name, weekly] = hit;
  return `<span class="acct-warn">Almost out of ${name}${w.resets ? ` - new Claude Code tasks may wait until it resets at ${when(w.resets, weekly)}` : ''}.</span>`;
}

function problem(a) {
  if (a.auth_error) return `<span class="acct-err">Login failed - the token may have expired. Run <code>claude setup-token</code> again and re-add it.</span>`;
  if (a.kind === 'mac' && a.logged_in === false) return '<span class="acct-err">This Mac isn\'t logged into Claude Code - run <code>claude</code> then /login in Terminal.</span>';
  return '';
}

function usedBy(a) {
  const names = (a.projects || []).map(p => p.name);
  const own = names.length ? 'Projects on it: ' + names.join(', ') : '';
  return esc(a.active ? (own ? own + ' · plus every project without its own account' : 'Used by every project without its own account')
    : own || 'No project uses it yet - pick it in a project\'s Settings tab');
}

// ---------------------------------------------------------------- a project's Settings tab
export function projectAccountCard(c, s) {
  const list = accountList(c);
  if (!list.length) return '';
  const def = activeAccount(c);
  const inUse = accountById(c, s.claude_account_in_use) || def;
  const own = s.claude_account && accountById(c, s.claude_account) ? s.claude_account : '';
  const opt = (id, label, title) => `<button class="segmented__opt${own === id ? ' is-on' : ''}" data-action="setProjectAccount" data-v="${esc(id)}" title="${esc(title)}">${esc(label)}</button>`;
  const rest = restingUntil(inUse);
  const status = !inUse ? '' : rest
    ? `<span class="acct-rest">${esc(shortName(inUse))} is at its usage limit. This project's tasks wait and carry on by themselves at ${clock(rest)}.</span>`
    : problem(inUse) || `<span class="card__desc">Runs on ${esc(inUse.alias || (inUse.kind === 'mac' ? "this Mac's login" : inUse.label))}${inUse.email ? ' (' + esc(inUse.email) + ')' : ''}${own ? '' : ', the default account'}.</span>`;
  return `<div class="card card--pad" data-key="proj-acct"><span class="card__title">Claude account</span>
    <span class="card__desc">Every Claude Code task in this project runs on this account. If it hits its usage limit, this project waits for it to reset - it never moves to another account. Projects on different accounts run at the same time.</span>
    <div class="segmented segmented--start acct-pick" data-noswipe>${opt('', 'Default', 'Whichever account is the default' + (def ? ' - now ' + shortName(def) : ''))}${list.map(a => opt(a.id, shortName(a), a.email || a.label)).join('')}</div>
    ${status}
    ${list.length < 2 ? '<button class="btn--text" data-action="go" data-view="settings">Add another Claude account</button>' : ''}
  </div>`;
}

// ---------------------------------------------------------------- sidebar
// Every account's state at a glance: which one is at its limit (and until when), which still has room.
export function sidebarAccounts(c) {
  const list = accountList(c);
  if (!list.length) return '';
  const row = a => {
    const rest = restingUntil(a), bad = problem(a);
    const used = [win(a, 'five_hour'), win(a, 'seven_day')].filter(Boolean).map(w => w.pct);
    const meta = bad ? 'login failed' : rest ? 'at limit · ' + clock(rest) : used.length ? Math.max(...used) + '% used' : 'has room';
    const tip = [shortName(a), a.active ? 'default' : '', rest ? 'at its usage limit until ' + clock(rest) : bad ? 'login failed' : 'has room' + (used.length ? ` - 5h ${used[0]}%` : '')].filter(Boolean).join(' · ');
    return `<button class="nav-item nav-proj nav-acct" data-action="openAccount" data-id="${esc(a.id)}" data-key="sa-${esc(a.id)}" title="${esc(tip)}">
      <span class="dot" style="--c:${healthColor(a)}"></span><span class="grow truncate">${esc(shortName(a))}${a.active ? ' <i class="nav-acct__def">default</i>' : ''}</span>
      <span class="nav-proj__meta" style="color:${bad ? 'var(--err)' : rest ? 'var(--sleep)' : 'var(--text-meta)'}">${esc(meta)}</span></button>`;
  };
  return `<div class="sidebar__group sidebar__accts" data-key="sidebar-accts"><span class="sidebar__label">CLAUDE ACCOUNTS</span>${list.map(row).join('')}</div>`;
}

// ---------------------------------------------------------------- header switcher

// Green / amber / red by how much of its tightest limit is used; purple while it waits out a limit.
function healthColor(a) {
  if (problem(a)) return 'var(--err)';
  if (restingUntil(a)) return 'var(--sleep)';
  const used = [win(a, 'five_hour'), win(a, 'seven_day')].filter(Boolean).map(w => w.pct);
  return used.length ? barColor(Math.max(...used)) : 'var(--text-meta)';
}

function switchOpt(x, busy) {
  const w5 = win(x, 'five_hour'), w7 = win(x, 'seven_day');
  const rest = restingUntil(x);
  const sub = [x.email, rest ? 'at its limit until ' + clock(rest) : [w5 && `5h ${w5.pct}%`, w7 && `wk ${w7.pct}%`].filter(Boolean).join(' · ') || 'limits not known yet'].filter(Boolean).join(' · ');
  // Picking the default again just closes the menu.
  const act = x.active ? 'data-action="closeMenus"' : `data-action="acctSwitch" data-id="${esc(x.id)}"${busy ? ' aria-disabled="true" disabled' : ''}`;
  return `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${!!x.active}" ${act}>
    <span class="dot" style="--c:${healthColor(x)}"></span><span class="menu__two"><span class="menu__main">${esc(shortName(x))}</span><span class="menu__sub">${esc(sub)}</span></span><span class="menu__check">${x.active ? '✓' : ''}</span></button>`;
}

// Every account in the header at once, each with its own limits - click one to make it the default
// (the default's card opens the usage sheet). No dropdown to open.
// When the window resets: just the time for the 5h one, weekday + time for the weekly one.
const resetAt = (d, weekly) => (weekly ? d.toLocaleDateString([], { weekday: 'short' }) + ' ' : '') + clock(d.toISOString());

// Same layout as the old single usage pill (label, bar, %, reset time), one tile per account.
function miniBar(label, w, weekly = false) {
  return `<div class="usage-pill__row"><span class="usage-pill__lbl">${label}</span><div class="bar4"><div style="width:${w ? w.pct : 0}%;background:${w ? barColor(w.pct) : 'transparent'}"></div></div><span class="usage-pill__pct">${w ? w.pct + '%' : '–'}</span><span class="usage-pill__reset">${w && w.resets ? '<i>resets </i>' + resetAt(w.resets, weekly) : ''}</span></div>`;
}

function accountCard(x, shown) {
  const rest = restingUntil(x), bad = problem(x);
  const tip = [shortName(x), x.email, rest ? 'at its limit until ' + clock(rest) : '', bad ? 'login failed' : '', x.active ? 'default account' : '', shown && !x.active ? "this project's account" : ''].filter(Boolean).join(' · ');
  return `<button class="usage-pill acct-card${shown ? ' is-on' : ''}" data-action="openAccount" data-id="${esc(x.id)}" title="${esc(tip)}" aria-label="Claude account ${esc(shortName(x))}: details">
    <span class="acct-card__name"><span class="dot" style="--c:${healthColor(x)}"></span><span class="truncate">${esc(shortName(x))}</span>${rest ? `<em>until ${esc(clock(rest))}</em>` : ''}</span>
    <span class="acct-card__lims">${miniBar('5h', win(x, 'five_hour'))}${miniBar('wk', win(x, 'seven_day'), true)}</span>
  </button>`;
}

// The details sheet one header tile opens: who it is, both limits, and a refresh.
// `overall` is the overall usage (API spend, by project, switches), shown below it on the same sheet.
export function accountSheet(c, wrap, overall = '') {
  const x = accountById(c, c.ui.octo.acctView);
  if (!x) return '';
  const f = c.ui.octo.acct, busy = !!f.busy, checking = f.busy === 'refresh';
  const inner = `<div class="sheet__head"><span class="sheet__title">${esc(shortName(x))}</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    <div class="sheet__body">
      <div class="card card--pad">
        <div class="set-agent__top"><b>${esc(shortName(x))}</b>${x.active ? '<span class="risk-chip risk-chip--ok">default</span>' : ''}</div>
        <span class="meta">${details(x)}</span>${problem(x)}${resting(x)}${nearLimit(x)}
        ${limits(x)}
        <span class="meta">${usedBy(x)}</span>
      </div>
      <div class="acct-actions">
        <button class="btn btn--sm" data-action="acctRefresh" data-id="${esc(x.id)}"${busy ? ' disabled' : ''}>${checking ? 'Checking…' : 'Refresh limits'}</button>
        ${x.active ? '<button class="btn btn--ghost btn--sm" disabled>✓ Selected account</button>' : `<button class="btn btn--primary btn--sm" data-action="acctSwitch" data-id="${esc(x.id)}"${busy ? ' disabled' : ''}>Select this account</button>`}
        <button class="btn--text" data-action="go" data-view="settings">Manage accounts</button>
      </div>
      ${overall ? `<span class="pal-label acct-overall">OVERALL USAGE</span>${overall}` : ''}
    </div>`;
  return wrap('account', inner, 'Claude account');
}

// Edit one account's name, alias and email together. This Mac's login keeps its own name and email, so only the alias shows.
export function accountEditSheet(c, wrap) {
  const e = c.ui.octo.acctEdit, f = c.ui.octo.acct;
  if (!e || !accountById(c, e.id)) return '';
  const busy = f.busy === 'edit';
  const field = (key, label, hint, attrs = '') => `<label class="field"><span class="set-group__t">${label}</span>
    <input class="input" value="${esc(e[key])}" data-input="octo.acctEdit.${key}" maxlength="${key === 'email' ? 120 : 60}" autocomplete="off" ${attrs}>
    <span class="meta">${hint}</span></label>`;
  return wrap('accountEdit', `<div class="sheet__body sheet__body--form">
    <div class="sheet__head sheet__head--flush"><span class="sheet__title">Edit account</span><button class="icon-btn icon-btn--44" data-action="closeSheet" aria-label="Close">✕</button></div>
    ${e.mac ? '' : field('name', 'Name', 'How you know this account.')}
    ${field('alias', 'Short name', 'Shown instead of the full name everywhere. Leave empty to use the name.')}
    ${e.mac ? '' : field('email', 'Email', 'Optional, only shown for reference.', 'type="email" autocapitalize="off" spellcheck="false"')}
    ${colorPick(accountById(c, e.id), f.busy)}
    ${e.error ? `<span class="form-err">${esc(e.error)}</span>` : ''}
    <div class="need__row edit-acts">
      <button class="btn btn--primary btn--44 grow" data-action="acctEditSave"${busy ? ' disabled' : ''}>${busy ? 'Saving…' : 'Save'}</button>
      <button class="btn btn--ghost btn--44" data-action="closeSheet">Cancel</button>
    </div>
  </div>`, 'Edit account');
}

export function accountSwitcher(c) {
  const list = accountList(c);
  if (!list.length || !activeAccount(c)) return '';
  // Inside a project the highlighted tile is that project's account (its own, else the default).
  const proj = c.ui.view === 'project' ? (c.d.projects || []).find(p => p.id === c.ui.projectId && !p.is_general) : null;
  const shown = (proj && list.find(a => a.id === proj.claude_account) || activeAccount(c)).id;
  return `<span class="acct-sw" data-key="acct-sw" role="group" aria-label="Claude accounts">${list.map(a => accountCard(a, a.id === shown)).join('')}</span>`;
}

// Which account a new task runs on: "Auto" = its project's own / the default, or pin one. Used by the Ask box.
export function accountPicker(c, value, action, disabled = false, any = null) {
  const list = accountList(c);
  if (!list.length) return '';
  const opt = (id, label, title, dot) => `<button class="segmented__opt${(value || '') === id ? ' is-on' : ''}" data-action="${action}" data-id="${esc(id)}" title="${esc(title)}" aria-pressed="${(value || '') === id}"${disabled ? ' disabled' : ''}>${dot ? `<span class="dot" style="--c:${dot};width:6px;height:6px"></span>` : ''}${esc(label)}</button>`;
  return `<div class="segmented mode-seg acct-seg" role="group" aria-label="Which Claude account runs this" data-noswipe>${any ? opt('', any[0], any[1], '') : opt('', 'Auto', "The project's own account, or the default", '')}${list.map(a => opt(a.id, shortName(a), 'Run on ' + (a.email || shortName(a)), healthColor(a))).join('')}</div>`;
}

// What a model that isn't Claude Code runs on instead of a Claude account (octopus/api.py PROVIDER_LABELS).
const KEYS = { openai: 'OpenAI API key', gemini: 'Gemini API key', tools: 'Your connected email & calendar accounts' };
// The model a form has picked: null = Auto (the router picks - Claude Code unless it says otherwise).
const modelOf = (c, id) => (id && id !== 'auto' ? (c.d.models || []).find(m => m.id === id) || null : null);
// Whether a model id (or Auto) runs on a Claude account - the only kind you can choose.
export const usesClaudeAccount = (c, id) => { const m = modelOf(c, id); return !m || m.lane === 'agent'; };
// The account Auto lands on for a project: its own, else the default.
export function autoAccount(c, projectId) {
  const p = projectId != null && (c.d.projects || []).find(x => x.id === projectId);
  return (p && accountById(c, p.claude_account)) || activeAccount(c);
}
const usedNote = a => {
  const bits = [['5-hour', win(a, 'five_hour')], ['weekly', win(a, 'seven_day')]].filter(([, w]) => w).map(([l, w]) => `${l} ${w.pct}%`);
  return [a.email, bits.length ? bits.join(' · ') + ' used' : null, restingUntil(a) ? 'at its limit until ' + clock(a.limited_until) : null].filter(Boolean).join(' · ');
};

// The ACCOUNT row of a task / backlog form, following the model picked above it: a Claude Code model (or Auto)
// picks a Claude account - Auto = the project's own / the default, named so you can see which - and any other
// model shows the key it's connected through instead (nothing to pick). Chips like the MODEL / MODE rows.
// value: the chosen account id ('' = Auto). attrs: extra attributes on each chip (data-id etc. for the action).
// inline: the label sits at the start of the chip row (the New task sheet's style) instead of above it.
export function accountField(c, { model, value, action, projectId = null, label = 'ACCOUNT', disabled = false, attrs = '', inline = false }) {
  const head = `<span class="pal-label">${label}</span>`;
  const row = (chips, aria = '') => (inline
    ? `<div class="pal-chips" data-noswipe${aria}>${head}${chips}</div>`
    : `${head}<div class="pal-chips pal-chips--flush" data-noswipe${aria}>${chips}</div>`);
  const m = modelOf(c, model);
  if (m && m.lane !== 'agent') {
    const key = KEYS[m.provider];
    if (!key) return '';
    return row(`<span class="pal-model is-on acct-key" title="${esc(m.available ? key : m.unavailable_reason || key)}"><span class="dot" style="--c:${m.available ? 'var(--ok)' : 'var(--text-faint)'};width:7px;height:7px"></span>${esc(key)}${m.available ? '' : ' · not set up'}</span>`);
  }
  const list = accountList(c);
  if (!list.length) return '';
  const auto = autoAccount(c, projectId);
  const dis = disabled ? ' disabled' : '';
  const chip = (id, text, title, dot) => `<button class="pal-model${(value || '') === id ? ' is-on' : ''}" data-action="${action}" data-id="${esc(id)}" ${attrs} title="${esc(title)}" aria-pressed="${(value || '') === id}"${dis}><span class="dot" style="--c:${dot};width:7px;height:7px"></span>${esc(text)}</button>`;
  return row(chip('', auto ? `Auto · ${shortName(auto)}` : 'Auto', "The project's own account, or the default" + (auto ? ` - now ${shortName(auto)}` : ''), 'var(--text)')
    + list.map(a => chip(a.id, shortName(a), 'Run on ' + (usedNote(a) || shortName(a)), healthColor(a))).join(''), ' role="group" aria-label="Which Claude account runs this"');
}

// Compact "account" pick for the inside of a model menu (chat reply / backlog box): the same choices as accountField.
export function accountMenuPart(c, { model, value, action, projectId = null }) {
  if (!usesClaudeAccount(c, model)) return '';
  const list = accountList(c);
  if (!list.length) return '';
  const auto = autoAccount(c, projectId);
  const opt = (id, main, sub, dot) => `<button class="menu__opt menu__opt--model" role="menuitemradio" aria-checked="${(value || '') === id}" data-action="${action}" data-id="${esc(id)}">
    <span class="dot" style="--c:${dot}"></span><span class="menu__two"><span class="menu__main">${esc(main)}</span>${sub ? `<span class="menu__sub">${esc(sub)}</span>` : ''}</span><span class="menu__check">${(value || '') === id ? '✓' : ''}</span></button>`;
  return `<span class="menu__label">ACCOUNT</span>${opt('', auto ? `Auto · ${shortName(auto)}` : 'Auto', "the project's own account, or the default", 'var(--text)')}${list.map(a => opt(a.id, shortName(a), usedNote(a), healthColor(a))).join('')}`;
}

// Short name of the account a form will run on, for a composer's model button ("Claude Code · LA").
export function accountShort(c, value, projectId) {
  const a = (value && accountById(c, value)) || autoAccount(c, projectId);
  return a ? shortName(a) : '';
}

// ---------------------------------------------------------------- Settings section
// The colors an account can take - must match accounts.PALETTE. Shown as dots / side lines on every queue item, chat and activity row.
const SWATCHES = ['#e8825a', '#4f9de8', '#4fbf8a', '#b583e8', '#e8c24f', '#e8627f', '#4fc6c9', '#9aa3b2'];
// color_set = the user picked it (otherwise it's the automatic one, which is only a default and isn't marked as chosen).
const colorPick = (a, busy) => {
  const custom = a.color_set && !SWATCHES.includes(a.color);
  return `<div class="acct-colors" role="group" aria-label="Color for ${esc(shortName(a))}" data-noswipe>
  <span class="meta">Color</span>${SWATCHES.map(col => { const on = a.color_set && a.color === col; return `<button class="acct-swatch${on ? ' is-on' : ''}" style="--c:${col}" data-action="acctColor" data-id="${esc(a.id)}" data-v="${col}" title="${col}" aria-label="Use ${col}" aria-pressed="${on}"${busy ? ' disabled' : ''}></button>`; }).join('')}
  <label class="acct-swatch acct-swatch--custom${custom ? ' is-on' : ''}"${custom ? ` style="--c:${esc(a.color)};background:var(--c)"` : ''} title="Pick any color"><input type="color" value="${esc(a.color || '#9aa3b2')}" data-acct-color="${esc(a.id)}" aria-label="Custom color"></label>
  <button class="btn--text" data-action="acctColor" data-id="${esc(a.id)}" data-v=""${a.color_set && !busy ? '' : ' disabled'}>${a.color_set ? 'Auto' : 'Auto ✓'}</button></div>`;
};

export function accountsSection(c) {
  const list = accountList(c), f = c.ui.octo.acct;
  if (!list.length) return '<div class="empty">Loading…</div>';
  return `<span class="meta prov-note">Which Claude subscription Octopus's Claude Code work uses. Give a project its own account in that project's Settings tab: its tasks always run on it, and if it hits its limit they wait for it to reset instead of moving to another account. Projects on different accounts run at the same time. The default account is for every project without its own. A change applies to the next task - one already running keeps its account. Chats, memory and settings are shared across accounts.</span>
    <div class="list">${list.map(a => `<div class="set-agent acct${a.active ? ' is-on' : ''}" data-key="acct-${esc(a.id)}">
      <div class="set-agent__top"><b>${esc(shortName(a))}</b>${a.alias ? `<span class="meta"> (${esc(a.kind === 'mac' ? (a.org || 'This Mac') : a.label)})</span>` : ''}${a.active ? '<span class="risk-chip risk-chip--ok">default</span>' : ''}</div>
      <span class="meta">${details(a)}</span>${problem(a)}${resting(a)}
      <span class="meta">${usedBy(a)}</span>
      ${colorPick(a, f.busy)}
      ${limits(a)}
      <div class="acct-actions">
        ${a.active ? '' : `<button class="btn btn--sm" data-action="acctSwitch" data-id="${esc(a.id)}"${f.busy ? ' disabled' : ''}>Make default</button>`}
        <button class="btn--text" data-action="acctEdit" data-id="${esc(a.id)}"${f.busy ? ' disabled' : ''}>Edit details</button>
        <button class="btn--text" data-action="acctRefresh" data-id="${esc(a.id)}"${f.busy ? ' disabled' : ''}>Check limits</button>
        ${a.kind === 'token' || list.length > 1 ? `<button class="btn--text acct-remove" data-action="acctRemove" data-id="${esc(a.id)}"${f.busy ? ' disabled' : ''}>Remove</button>` : ''}
      </div></div>`).join('')}</div>
    <div class="acct-add" data-key="acct-add">
      <span class="set-group__t">Add an account</span>
      <ol class="acct-steps meta">
        <li>In Terminal on this Mac, run <code>claude setup-token</code>.</li>
        <li>Sign in with the other Claude account in the browser that opens.</li>
        <li>Copy the token it prints (starts with <code>sk-ant-oat</code>) and paste it below.</li>
      </ol>
      <input class="input" placeholder="Name, e.g. Personal" maxlength="60" value="${esc(f.label)}" data-input="octo.acct.label" aria-label="Account name" autocomplete="off">
      <input class="input" type="email" placeholder="Its email (optional, just to show)" maxlength="120" value="${esc(f.email)}" data-input="octo.acct.email" aria-label="Account email" autocomplete="off" autocapitalize="off" spellcheck="false">
      <input class="input input--mono" type="password" placeholder="sk-ant-oat01-…" value="${esc(f.token)}" data-input="octo.acct.token" aria-label="Token" autocomplete="off" autocapitalize="off" spellcheck="false">
      ${f.error ? `<span class="acct-err">${esc(f.error)}</span>` : ''}
      <button class="btn btn--sm" data-action="acctAdd"${f.busy ? ' disabled' : ''}>${f.busy === 'add' ? 'Checking the token…' : 'Add account'}</button>
      ${c.d.settings.claude_mac_hidden ? `<button class="btn--text" data-action="acctShowMac"${f.busy ? ' disabled' : ''}>Bring back this Mac's login</button>` : ''}
    </div>`;
}
