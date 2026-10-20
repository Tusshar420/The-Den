// Glue between the CRM page (views/crm-dash.js) and app.js: the client filter, the summary tiles that
// scroll to their list, and the client / project report downloads (GET /crm/reports/{kind}/{id}?format=csv|md).

import { store, apiKey } from './api.js';
import { loadCrm } from './data.js';

const u = () => store.ui;
const report = () => ({ kind: 'client', id: '', busy: false, ...(u().crmReport || {}) });

// Fetched with the API key (a plain link can't send it), then saved under the name the server gives it.
async function download(kind, id, fmt) {
  const res = await fetch(`/crm/reports/${kind}/${id}?format=${fmt}`, { headers: { 'X-API-Key': apiKey() } });
  if (!res.ok) {
    let detail = res.status === 404 ? `That ${kind} is gone` : res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(res.status === 404 && detail === 'Not Found' ? 'Restart the Den to download reports' : detail);
  }
  const name = (/filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '') || [])[1] || `${kind}-report.${fmt}`;
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return name;
}

export function crmDashActions(h) {
  const { set, toast, fail } = h;
  return {
    crmClient: el => { set({ crmClient: el.value || '' }); loadCrm().catch(fail); },
    crmJump: el => {
      const target = document.getElementById(`cd-${el.dataset.k}`);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    },
    crmReportKind: el => { if (report().kind !== el.dataset.v) set({ crmReport: { ...report(), kind: el.dataset.v, id: '' } }); },
    crmReportPick: el => set({ crmReport: { ...report(), id: el.value } }),
    crmDownload: el => {
      const r = report();
      if (!r.id || r.busy) return;
      set({ crmReport: { ...r, busy: true } });
      download(r.kind, r.id, el.dataset.fmt)
        .then(name => toast(`Saved ${name}`), fail)
        .finally(() => set({ crmReport: { ...report(), busy: false } }));
    },
  };
}
