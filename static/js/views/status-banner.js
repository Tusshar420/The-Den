// Health and status banner: a strip under the top bar that says plainly what state the service is in
// and what happens next - this device offline, the Mac's service unreachable, a Claude limit reached
// (with the auto-resume time), or sleep mode running. Unlike the offline popup it can't be dismissed
// away for good: it stays while the state lasts and clears itself. Everything is derived from data
// the page already polls, so there's no extra request.

import { esc, clock, spinner } from '../ui.js';
import { waitFmt, plural } from '../format.js';
import { limitWindowWords } from '../derive.js';

const banner = (kind, title, text, act = '') =>
  `<div class="sbanner sbanner--${kind}" data-key="sbanner-${kind}" role="status" aria-live="polite">
    <span class="sbanner__dot"></span>
    <span class="sbanner__body"><b>${title}</b> <span>${text}</span></span>${act}
  </div>`;

export function statusBanners(c) {
  const { d, ui } = c;
  if (!d.loaded && !d.error) return '';
  const now = Date.now();
  const out = [];
  const since = d.offlineSince ? ` since ${esc(clock(d.offlineSince))}` : '';
  const retry = `<button class="sbanner__act" data-action="offlineRetry"${ui.offlineChecking ? ' disabled' : ''}>${ui.offlineChecking ? spinner() : 'Retry now'}</button>`;
  const deviceOffline = typeof navigator !== 'undefined' && navigator.onLine === false;

  if (deviceOffline) {
    out.push(banner('off', 'This device is offline',
      `No internet connection${since}. What you see may be out of date. This page reconnects by itself when the network is back; your queued tasks on the Mac keep running meanwhile.`, retry));
  } else if (d.error && (d.offlineFails || 0) >= 2 && !ui.restart) {
    out.push(banner('off', "Can't reach Octopus",
      `The service on your Mac isn't answering${since}. Queued tasks are safe and will start again once it's back. This page keeps retrying on its own.`, retry));
  } else if (ui.restart) {
    out.push(banner('sleep', 'Octopus is restarting', 'This page reloads when it is back. Nothing new starts meanwhile.'));
  }
  const reachable = out.length === 0 || ui.restart;

  if (reachable && !d.error) {
    const limited = d.tasks.filter(t => t.status === 'rate_limited' && !t.dismissed);
    if (limited.length) {
      const times = limited.map(t => t.resume_at && new Date(t.resume_at).getTime()).filter(Boolean).sort((a, b) => a - b);
      const accts = (d.settings && d.settings.claude_accounts) || [];
      const acctName = id => { const a = accts.find(x => x.id === id); return a ? (a.alias || (a.kind === 'mac' ? (a.org || a.email || 'Mac login') : a.label)) : id; };
      const ids = [...new Set(limited.map(t => t.claude_account).filter(Boolean))];
      const hit = limitWindowWords(d, ids.length === 1 ? ids[0] : null).hit;
      const who = ids.length ? ` on ${ids.map(id => `<b>${esc(acctName(id))}</b>`).join(', ')}` : '';
      const resume = times.length
        ? (times[0] <= now ? 'Resuming any moment now.' : `Resumes on its own at ${clock(new Date(times[0]).toISOString())} (in ${waitFmt((times[0] - now) / 1000)}).`)
        : 'It resumes on its own when the limit resets.';
      out.push(banner('sleep', 'Rate-limited',
        `Hit ${esc(hit)}${who}; ${plural(limited.length, 'task')} paused. ${resume}`));
    }
    // Code edited since the service started only takes effect after a restart (the Restart button asks first).
    const changed = (d.status && d.status.code_changed) || [];
    if (changed.length && !ui.restart) {
      const shown = changed.slice(0, 3).map(n => `<code>${esc(n)}</code>`).join(', ') + (changed.length > 3 ? ` and ${changed.length - 3} more` : '');
      out.push(banner('wait', 'Restart needed',
        `Octopus is running older code than what's on disk (${shown}). Restart to get the latest.`,
        `<button class="sbanner__act" data-action="restartOctopus">Restart</button>`));
    }
    const sm = d.settings && d.settings.sleep_mode;
    if (sm && sm.until && new Date(sm.until).getTime() > now) {
      out.push(banner('info', 'Sleep mode on',
        `Working through the queue and using the limits until ${clock(sm.until)} (${waitFmt((new Date(sm.until).getTime() - now) / 1000)} left), then back to normal.`,
        `<button class="sbanner__act" data-action="toggleSleep">I'm awake</button>`));
    }
  }
  return out.length ? `<div class="sbanners" data-key="sbanners">${out.join('')}</div>` : '';
}
