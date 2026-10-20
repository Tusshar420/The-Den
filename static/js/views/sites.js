// Settings > Sites: paste a website link and Octopus learns it (the learn-website skill runs as a task),
// and the sites it has already learned are listed below. State lives in ui.sites: { busy, error, done }.

import { store } from '../api.js';
import { esc } from '../ui.js';
import { octoApi, putOcto } from '../octopus-data.js';

export function sitesSection(c) {
  const s = c.ui.sites || {}, list = c.d.octo.sites;
  const rows = !list ? '<div class="empty">Loading…</div>'
    : !list.length ? '<div class="empty">Nothing learned yet. Paste a link above to start.</div>'
    : `<div class="list">${list.map(x => `<div class="set-agent" data-key="site-${esc(x.id)}">
        <div class="set-agent__top"><b>${esc(x.name)}</b>${x.analyzed ? `<span class="meta">analyzed ${esc(x.analyzed)}</span>` : ''}</div>
        <span class="meta">${esc(x.description)}</span>
        ${x.url ? `<a class="meta" href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.url)}</a>` : ''}
      </div>`).join('')}</div>`;
  return `<span class="meta prov-note">Share a link - a 3D site, a web app, any public page. Octopus inspects the interface, works out how it is built, and saves what it learns as a skill Claude can use later.</span>
    <form class="gate__form" data-submit="siteLearn">
      <input id="siteUrl" class="input" type="url" inputmode="url" placeholder="https://example.com" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Website link"${s.busy ? ' disabled' : ''}>
      <button class="btn btn--primary" type="submit"${s.busy ? ' disabled' : ''}>${s.busy ? 'Starting…' : 'Learn this site'}</button>
    </form>
    ${s.error ? `<span class="meta bk-err" role="alert">${esc(s.error)}</span>` : ''}
    ${s.done ? `<span class="meta bk-ok">${esc(s.done)}</span>` : ''}
    <div class="set-group"><span class="set-group__t">Learned sites</span>${rows}</div>`;
}

export function sitesActions({ set, toast, fail }) {
  return {
    siteLearn: async () => {
      const box = document.getElementById('siteUrl');
      const url = ((box && box.value) || '').trim();
      if (!url || (store.ui.sites || {}).busy) return;
      set({ sites: { busy: true, error: '', done: '' } });
      try {
        const r = await octoApi.learnSite(url);
        set({ sites: { done: `Started task #${r.task.id} for ${r.url}. It appears under Activity; the new skill shows up here when it finishes.` } });
        toast('Octopus is learning the site');
      } catch (e) { set({ sites: { error: e.message } }); }
      octoApi.sites().then(v => putOcto({ sites: v }), () => {});
    },
  };
}
