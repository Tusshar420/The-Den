// Service worker: shows The Den's Web Push notifications (webpush.py) while the Dock / Home
// Screen app is closed. Served from /sw.js so its scope covers /dashboard. Caches nothing -
// the dashboard keeps revalidating every file, as before.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

const ICON = '/static/icon-512.png';

function setBadge(n) {
  const nav = self.navigator;
  if (typeof n !== 'number' || !nav || !nav.setAppBadge) return Promise.resolve();
  return (n > 0 ? nav.setAppBadge(n) : nav.clearAppBadge()).catch(() => {});
}

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: 'The Den', body: e.data ? e.data.text() : '' }; }
  const approval = d.kind === 'approval';
  const decide = approval && d.approveUrl && d.denyUrl;
  e.waitUntil(Promise.all([
    self.registration.showNotification(d.title || 'The Den', {
      body: d.body || '',
      icon: ICON,
      tag: d.tag || undefined,
      renotify: !!d.tag,
      requireInteraction: approval,          // an approval holds up the queue: stay until dealt with
      data: { url: d.url || '/dashboard', approveUrl: d.approveUrl, denyUrl: d.denyUrl },
      // Safari may not show buttons; tapping the notification still opens the approval.
      actions: decide ? [{ action: 'approve', title: 'Approve' }, { action: 'deny', title: 'Deny' }] : [],
    }),
    setBadge(d.pending),
  ]));
});

async function openDashboard(url) {
  const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const win = wins.find(w => new URL(w.url).pathname.startsWith('/dashboard')) || wins[0];
  if (win) {
    await win.focus();
    if (win.url !== new URL(url, self.location.origin).href && win.navigate) await win.navigate(url).catch(() => {});
    return;
  }
  await self.clients.openWindow(url);
}

async function decide(link, approved) {
  let title;
  try {
    const res = await fetch(link, { credentials: 'omit' });
    const page = await res.text();
    title = !res.ok ? "Couldn't decide - open The Den"
      : /Already decided/.test(page) ? 'Already decided'
      : approved ? 'Approved ✓ Octopus is carrying on' : "Denied - Octopus won't run it";
  } catch { title = "Couldn't reach The Den"; }
  await self.registration.showNotification(title, { icon: ICON, tag: 'den-decided' });
}

self.addEventListener('notificationclick', e => {
  const n = e.notification, d = n.data || {};
  n.close();
  if (e.action === 'approve' && d.approveUrl) return e.waitUntil(decide(d.approveUrl, true));
  if (e.action === 'deny' && d.denyUrl) return e.waitUntil(decide(d.denyUrl, false));
  e.waitUntil(openDashboard(d.url || '/dashboard'));
});
