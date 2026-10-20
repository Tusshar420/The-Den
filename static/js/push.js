// Web Push for the dashboard installed as a web app (Safari › Add to Dock on the Mac, Add to Home
// Screen on the iPhone): registers /sw.js and hands its push subscription to The Den (webpush.py),
// so approvals and finished tasks arrive as system notifications even with the app closed.
// Needs a secure context - open the dashboard over https (Tailscale Serve), not http://100.x:8787.
import { api } from './api.js';

export const canPush = () => window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window
  && typeof Notification !== 'undefined';

let active = false;
/** A subscription is live: the service worker shows the notifications, so the page doesn't repeat them. */
export const pushActive = () => active;

const keyBytes = b64 => {
  const s = atob((b64 + '='.repeat((4 - b64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(s, c => c.charCodeAt(0));
};

/** Register and (re)subscribe; only once notifications are allowed. Safe to call on every load:
 *  re-sending the same subscription just refreshes it server-side. */
export async function syncPush() {
  if (!canPush() || Notification.permission !== 'granted') return false;
  try {
    const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const { publicKey } = await api('/push/key');
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
    }
    await api('/push/subscribe', { method: 'POST', body: sub.toJSON() });
    active = true;
  } catch (e) {
    console.warn('Web Push not set up:', e);
    active = false;
  }
  return active;
}

/** The red count on the Dock / Home Screen icon: approvals waiting on you. */
export function setBadge(n) {
  if (!navigator.setAppBadge) return;
  (n > 0 ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => {});
}
