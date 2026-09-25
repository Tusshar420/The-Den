// "Octopus is offline": this page lost the service on the Mac (it stopped, crashed, or the network dropped).
// Shown after two polls in a row fail - one blip stays quiet - and never during a Restart you asked for.
// Clears itself on the next poll that gets through.

import { esc, octo, clock, spinner } from '../ui.js';

export function offlinePopup(c) {
  const { d, ui } = c;
  if (!d.error || (d.offlineFails || 0) < 2 || ui.restart || ui.offlineHidden === d.offlineSince) return '';
  return `<div class="scrim offline-scrim" data-key="offline" role="alertdialog" aria-modal="true" aria-labelledby="offline-title" aria-describedby="offline-text">
    <div class="offline">
      ${octo('sleep', 72)}
      <span class="offline__eyebrow">CONNECTION LOST</span>
      <span class="offline__title" id="offline-title">Octopus is offline</span>
      <span class="offline__text" id="offline-text">This page can't reach Octopus on your Mac${d.offlineSince ? ` (since ${esc(clock(d.offlineSince))})` : ''}. Your queued tasks are safe - they start again once it's back, and this page reconnects on its own.</span>
      <div class="offline__hint"><span>On the Mac, check it's running:</span><code>launchctl list | grep claudequeue</code></div>
      <span class="offline__err">Last error: ${esc(d.error)}</span>
      <div class="offline__acts">
        <button class="btn btn--ghost btn--44" data-action="offlineDismiss">Dismiss</button>
        <button class="btn btn--primary btn--44 grow" data-action="offlineRetry"${ui.offlineChecking ? ' disabled' : ''}>${ui.offlineChecking ? spinner() + 'Checking…' : 'Try now'}</button>
      </div>
    </div>
  </div>`;
}
