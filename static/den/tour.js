// The Den - guided tours (Help › Guides & tours).
// A small spotlight engine that sits above the React pages: it opens the right page, dims the screen
// except one real element (found by its data-tour="..." attribute), and shows a card next to it with
// Back / Next / Skip. If the element isn't on screen (a phone, a closed panel, an empty list) the card
// sits in the middle instead. Tours you finished or skipped are remembered in the Den's prefs
// (toursDone), so they follow you to other devices.
// Also here: the "What's new" popup after an update, and small centred dialogs other parts can use.
// Plain script, no modules; loaded right after den.js. Nothing here may throw while the page boots.
(function () {
  'use strict';
  const D = () => window.Den;
  const CORAL = '#FF7B5C';

  // ------------------------------------------------------------------ the tours
  // t: the data-tour target. page: open this page first (default: the tour's page). panel: open this
  // right-hand panel first. Bodies are plain English, one or two short sentences.
  const TOURS = [
    { id: 'home', title: 'Den Home', page: 'home', blurb: 'What Octopus is doing, the Ask box, and the panels on the side.', steps: [
      { t: 'nav-home', title: 'Your home base', body: 'Den Home shows what Octopus is doing right now and anything that needs you. Press 1 to come back here.' },
      { t: 'home-hero', title: 'What Octopus is doing', body: 'This card shows the task running now and how far along it is. If Octopus needs your OK, the buttons appear right here.' },
      { t: 'ask', title: 'Ask Octopus', body: 'Type a request in plain words, like “fix the typo on the contact page”. Pick a project and send. Press N to jump here.' },
      { t: 'palette', title: 'Jump anywhere', body: 'Search pages, projects and actions, or type a command like “pause everything till 9am”. The shortcut is ⌘K.' },
      { t: 'rail', title: 'Panels on the side', body: 'The queue, notifications, calendar, to-dos and notes open here, next to your work.' },
      { t: 'usage', title: 'Your AI limits', body: 'How much of your Claude limits you have used. When a limit is reached, Octopus waits and carries on by itself.' },
    ] },
    { id: 'projects', title: 'Projects & chats', page: 'projects', blurb: 'Projects are folders on your Mac. Every task is a chat you can reply to.', steps: [
      { t: 'nav-projects', title: 'Projects', body: 'A project is a folder on your Mac. Tasks for a project run inside its folder.' },
      { t: 'projects-new', title: 'Add a project', body: 'Pick a folder and give it a name. You can also make a project with no folder, just to track a client deal.' },
      { t: 'projects-search', title: 'Find and filter', body: 'Search by name or folder, and filter to what is running, queued or failed.' },
      { t: 'projects-list', title: 'Open a project', body: 'Inside a project: its work, a backlog of to-dos that only run when you say so, phases, notes, documents and settings.' },
      { t: 'rail-messages', title: 'Every task is a chat', body: 'Open any task to see each step Octopus took. Reply to carry on in the same conversation.' },
    ] },
    { id: 'queue', title: 'Queue & approvals', page: 'home', blurb: 'What runs next, and how Octopus asks before risky steps.', steps: [
      { t: 'rail-queue', title: 'The queue', body: 'Octopus runs one task at a time per AI account. Everything waiting is listed in the Queue panel.' },
      { t: 'rpanel', panel: 'queue', title: 'Running now and up next', body: 'Drag tasks to change the order, move one to the top, pause it, or cancel it (with Undo).' },
      { t: 'home-needs', title: 'Things that need you', body: 'Risky steps, like deleting files or pushing code, wait here for Approve or Deny. Failed tasks show Retry.' },
      { t: 'rail-alerts', title: 'Notifications', body: 'Approvals, finished tasks and errors are listed here. With phone notifications on, approvals arrive with Approve / Deny buttons.' },
      { t: 'nav-review', title: 'Review runs', body: 'Tasks that changed files wait here, so you can look at the changes and mark them reviewed.' },
    ] },
    { id: 'crm', title: 'CRM', page: 'crm', blurb: 'Deals, clients, contacts and follow-ups.', steps: [
      { t: 'nav-crm', title: 'Your deals', body: 'The CRM shows your projects as deals, from lead to done, with what is overdue or at risk.' },
      { t: 'crm-tabs', title: 'Board or list', body: 'Switch between the pipeline board and the other views.' },
      { t: 'crm-add', title: 'Add a deal', body: 'Start a new deal. Dragging a deal to another stage updates its status.' },
      { t: 'crm-pipeline', title: 'The pipeline', body: 'The numbers at the top count what is open, due soon and at risk.' },
      { t: 'nav-clients', title: 'Clients and contacts', body: 'Clients hold their projects and people. Every call, meeting and note is kept with them.' },
    ] },
    { id: 'mail', title: 'Email & calendar', page: 'email', blurb: 'Your inbox, calendar and connected accounts.', steps: [
      { t: 'nav-email', title: 'Email', body: 'Mail, chats and approvals in one list. Octopus can search and draft, and sends only with your OK.' },
      { t: 'inbox-tabs', title: 'Sort what you see', body: 'Pick a tab to see only mail, chats or approvals.' },
      { t: 'cal-bar', page: 'calendar', title: 'Calendar', body: 'Project dates, follow-ups, meetings and your Google Calendar in one place. Add a meeting with the + button.' },
      { t: 'conn-list', page: 'connections', title: 'Connections', body: 'Connect your Google account here so email and calendar work. Sign-ins stay on your Mac.' },
    ] },
    { id: 'recipes', title: 'Recipes & schedules', page: 'recipes', blurb: 'Saved prompts that run with one tap or on a timetable.', steps: [
      { t: 'nav-recipes', title: 'Recipes', body: 'A recipe is a saved request you can run with one tap, or on a timetable.' },
      { t: 'recipes-list', title: 'Your recipes', body: 'Pick one to edit it. The switch turns a schedule on or off.' },
      { t: 'recipes-triggers', title: 'When it runs', body: 'Run it by hand, on a schedule (daily, weekdays, weekly), or when something happens.' },
      { t: 'recipes-test', title: 'Try it safely', body: 'Test shows what would happen without changing anything.' },
    ] },
    { id: 'settings', title: 'Settings', page: 'settings', blurb: 'Your choices for the whole Den.', steps: [
      { t: 'foot-settings', title: 'Settings', body: 'Settings live at the bottom of the menu.' },
      { t: 'settings-search', title: 'Search settings', body: 'Type a word like “timeout” or “sounds” to find a setting fast.' },
      { t: 'settings-groups', title: 'Grouped by topic', body: 'General, money, appearance, safety, notifications, briefing, email, budget, quick buttons and shortcuts.' },
      { t: 'foot-help', title: 'Help is here', body: 'This button opens Guides & tours: the checklist, all tours and the setup guide.' },
    ] },
    { id: 'updates', title: 'Updates', page: 'updates', blurb: 'Which version you have, and how to update.', steps: [
      { t: 'nav-updates', title: 'Updates', body: 'See which version of the Den you have and what changed.' },
      { t: 'upd-status', title: 'Check and update', body: 'Check for a new version. Updating restarts the Den; running tasks finish first.' },
      { t: 'upd-source', title: 'Update address', body: 'The Den’s owner gives you an address and a key. Paste them here once.' },
      { t: 'upd-news', title: 'What’s new', body: 'Every version with a short list of what changed.' },
    ] },
  ];
  // Which tour a page belongs to (for "Take a tour of this page").
  const PAGE_TOUR = { home: 'home', projects: 'projects', messages: 'projects', activity: 'queue', review: 'queue', crm: 'crm', crmx: 'crm', clients: 'crm', contacts: 'crm', team: 'crm',
    email: 'mail', calendar: 'mail', connections: 'mail', briefing: 'mail', notes: 'mail', recipes: 'recipes', settings: 'settings', bin: 'settings', updates: 'updates', about: 'updates' };

  // ------------------------------------------------------------------ small DOM helpers
  const el = (tag, css, kids) => { const e = document.createElement(tag); if (css) e.style.cssText = css; (kids || []).forEach(k => k != null && e.append(k)); return e; };
  const txt = (tag, css, t) => { const e = el(tag, css); e.textContent = t; return e; };
  const btn = (label, kind, run) => {
    const css = 'height:34px; display:inline-flex; align-items:center; justify-content:center; padding:0 14px; border-radius:10px; cursor:pointer; font-family:inherit; font-size:13px; white-space:nowrap; '
      + (kind === 'p' ? 'border:0; background:' + CORAL + '; color:#1A0A05; font-weight:700;' : kind === 'q' ? 'border:0; background:transparent; color:#93A1B0; font-weight:650; padding:0 8px;' : 'border:1px solid rgba(255,255,255,0.1); background:rgba(255,255,255,0.05); color:#DCE4EB; font-weight:650;');
    const b = txt('button', css, label); b.type = 'button'; b.addEventListener('click', e => { e.stopPropagation(); run && run(); }); return b;
  };
  const FONT = "font-family:'Bricolage Grotesque', system-ui, sans-serif; color:#E8EEF3;";
  const CARD = 'position:fixed; z-index:1202; box-sizing:border-box; display:flex; flex-direction:column; gap:10px; padding:16px 16px 14px; border-radius:18px; background:rgba(16,22,30,0.97); border:1px solid rgba(255,255,255,0.12); box-shadow:0 30px 80px -24px rgba(0,0,0,0.9), inset 0 1px 0 rgba(255,255,255,0.06); ' + FONT;
  const isPhone = () => innerWidth < 640;
  const prefs = () => { try { return D().prefs() || {}; } catch (e) { return {}; } };
  const done = () => prefs().toursDone || {};
  const mark = (id, how) => { try { D().setPref('toursDone', { ...done(), [id]: how }); } catch (e) {} };
  const hashPage = () => (location.hash.replace(/^#\/?/, '').split('/')[0] || 'home');

  // ------------------------------------------------------------------ the spotlight
  let cur = null;   // { tour, i, root, hole, card, timer, onKey, onMove }

  function visible(e) {
    if (!e || !e.isConnected) return null;
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    const cs = getComputedStyle(e); if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) return null;
    return r;
  }
  // The first element with this data-tour that is really showing.
  function find(t) {
    const pick = name => { const all = [...document.querySelectorAll('[data-tour="' + name + '"]')];
      return all.find(e => onScreen(visible(e))) || all.find(e => { const r = visible(e); return r && r.right > 8 && r.left < innerWidth - 8; }) || null; };   /* below the fold is fine (we scroll); off to the side (a closed drawer) is not */
    /* on a phone the menu is a closed drawer: nav-projects / rail-queue fall back to the bottom tab bar (tab-projects, tab-queue) */
    return pick(t) || (/^(nav|rail)-/.test(t) ? pick(t.replace(/^(nav|rail)-/, 'tab-')) : null);
  }
  // Is the box at least partly inside the window (not off in a closed drawer)?
  const onScreen = r => r && r.right > 8 && r.left < innerWidth - 8 && r.bottom > 8 && r.top < innerHeight - 8;

  function stop(how) {
    if (!cur) return;
    const c = cur; cur = null;
    clearInterval(c.timer); clearTimeout(c.wait);
    removeEventListener('keydown', c.onKey, true); removeEventListener('resize', c.onMove); removeEventListener('scroll', c.onMove, true);
    c.root.remove();
    if (how) mark(c.tour.id, how);
    if (how === 'done') { try { D().say('Tour finished', 'Replay it any time from Help › Guides & tours', '#6FCF97'); } catch (e) {} }
  }

  function start(id) {
    const tour = TOURS.find(x => x.id === id); if (!tour) return false;
    stop(null);
    const root = el('div', 'position:fixed; inset:0; z-index:1200; ' + FONT);
    root.setAttribute('data-den-tour', id);
    const shade = el('div', 'position:absolute; inset:0; background:rgba(3,5,8,0); transition:background .2s;');   /* catches clicks outside the card */
    const hole = el('div', 'position:fixed; z-index:1201; pointer-events:none; border-radius:14px; box-shadow:0 0 0 2px ' + CORAL + ', 0 0 0 6px rgba(255,123,92,0.22), 0 0 0 200vmax rgba(3,5,8,0.66); transition:top .25s, left .25s, width .25s, height .25s, opacity .2s; opacity:0;');
    const card = el('div', CARD + ' width:min(360px, calc(100vw - 24px)); opacity:0; transition:opacity .18s, top .25s, left .25s;');
    card.setAttribute('role', 'dialog'); card.setAttribute('aria-live', 'polite');
    card.addEventListener('click', e => e.stopPropagation());
    root.append(shade, hole, card);
    document.body.append(root);
    cur = { tour, i: 0, root, shade, hole, card, target: null };
    cur.onKey = e => {
      if (!cur) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); stop('skipped'); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); go(cur.i + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); go(cur.i - 1); }
      else if (!/^(Tab|Enter| )$/.test(e.key)) { e.stopPropagation(); }   /* the Den's own one-key shortcuts stay quiet during a tour */
    };
    cur.onMove = () => { if (cur && !cur.raf) cur.raf = requestAnimationFrame(() => { if (cur) { cur.raf = 0; place(); } }); };
    addEventListener('keydown', cur.onKey, true); addEventListener('resize', cur.onMove); addEventListener('scroll', cur.onMove, true);
    cur.timer = setInterval(() => cur && place(), 400);   /* follow the page as it loads and moves */
    go(0);
    return true;
  }

  function go(i) {
    if (!cur) return;
    const steps = cur.tour.steps;
    if (i < 0) return;
    if (i >= steps.length) return stop('done');
    cur.i = i; cur.target = null; clearTimeout(cur.wait);
    const st = steps[i]; const page = st.page || cur.tour.page;
    const den = D();
    if (page && hashPage() !== page && den && den.go) den.go(page);
    if (st.panel && den && den.openPanel) den.openPanel(st.panel);
    render(st, i, steps.length, true);
    // Wait for the page to draw the element (up to 3 seconds), then point at it - or stay centred.
    const t0 = Date.now();
    const look = () => {
      if (!cur || cur.i !== i) return;
      const e = find(st.t);
      if (e) {
        let r = visible(e);
        if (r && (r.top < 60 || r.bottom > innerHeight - 20)) { try { e.scrollIntoView({ block: r.height > innerHeight * 0.6 ? 'start' : 'center', behavior: 'smooth' }); } catch (x) { e.scrollIntoView(); } }
        cur.target = e; place(); return;
      }
      if (Date.now() - t0 < 3000) cur.wait = setTimeout(look, 120);
      else { cur.target = null; place(); }
    };
    cur.wait = setTimeout(look, page && hashPage() !== page ? 250 : 30);
  }

  function render(st, i, n, fresh) {
    const c = cur.card; c.innerHTML = '';
    const head = el('div', 'display:flex; align-items:center; gap:8px;', [
      txt('span', "flex:1; font-family:'JetBrains Mono', monospace; font-size:10px; letter-spacing:.12em; color:" + CORAL + ';', (cur.tour.title + ' · step ' + (i + 1) + ' of ' + n).toUpperCase()),
    ]);
    const x = btn('✕', 'q', () => stop('skipped')); x.title = 'Close (Esc)'; x.setAttribute('aria-label', 'Close the tour'); x.style.height = '26px'; head.append(x);
    const dots = el('div', 'flex:1; display:flex; gap:4px; align-items:center;');
    for (let k = 0; k < n; k++) dots.append(el('span', 'height:6px; border-radius:6px; width:' + (k === i ? '18px' : '6px') + '; background:' + (k === i ? CORAL : k < i ? 'rgba(255,255,255,0.35)' : 'rgba(255,255,255,0.14)') + ';'));
    const foot = el('div', 'display:flex; align-items:center; gap:6px; margin-top:2px;', [dots,
      i < n - 1 ? btn('Skip', 'q', () => stop('skipped')) : null,
      i > 0 ? btn('Back', '', () => go(i - 1)) : null,
      btn(i === n - 1 ? 'Done' : 'Next', 'p', () => go(i + 1))]);
    c.append(head, txt('div', 'font-size:16px; font-weight:750; line-height:1.3;', st.title), txt('div', 'font-size:13.5px; line-height:1.6; color:#C2CDD8;', st.body), foot);
    if (fresh) { const nb = foot.lastChild; setTimeout(() => { try { nb.focus({ preventScroll: true }); } catch (e) {} }, 60); }
    place();
  }

  // Put the hole over the target and the card beside it (below, above, right, left), inside the window.
  function place() {
    if (!cur) return;
    const { hole, card, shade } = cur; const W = innerWidth, H = innerHeight, M = 12;
    let r = cur.target ? visible(cur.target) : null;
    if (r && !onScreen(r)) r = null;
    const cw = card.offsetWidth || 340, ch = card.offsetHeight || 170;
    card.style.opacity = '1';
    if (!r) {   // nothing to point at: dim everything, card in the middle (or at the bottom on a phone)
      hole.style.opacity = '0'; shade.style.background = 'rgba(3,5,8,0.66)';
      card.style.left = Math.round((W - cw) / 2) + 'px';
      card.style.top = Math.round(isPhone() ? H - ch - M - 8 : (H - ch) / 2) + 'px';
      return;
    }
    shade.style.background = 'rgba(3,5,8,0)';
    const P = 6; const top = Math.max(4, r.top - P), left = Math.max(4, r.left - P);
    const bottom = Math.min(H - 4, r.bottom + P), right = Math.min(W - 4, r.right + P);
    Object.assign(hole.style, { opacity: '1', top: top + 'px', left: left + 'px', width: (right - left) + 'px', height: (bottom - top) + 'px' });
    const clampX = x => Math.max(M, Math.min(W - cw - M, x)), clampY = y => Math.max(M, Math.min(H - ch - M, y));
    let x, y;
    if (isPhone()) {   // phones: the card docks to the bottom, or the top if the target is down there
      x = M; card.style.width = (W - 2 * M) + 'px';
      y = bottom < H - ch - 24 ? H - ch - M - 8 : top > ch + 24 ? M + 8 : H - ch - M - 8;
    } else {
      card.style.width = 'min(360px, calc(100vw - 24px))';
      if (H - bottom >= ch + 14) { y = bottom + 10; x = clampX(left); }
      else if (top >= ch + 14) { y = top - ch - 10; x = clampX(left); }
      else if (W - right >= cw + 14) { x = right + 12; y = clampY(top); }
      else if (left >= cw + 14) { x = left - cw - 12; y = clampY(top); }
      else { x = clampX((W - cw) / 2); y = H - ch - M - 8; }
    }
    card.style.left = Math.round(x) + 'px'; card.style.top = Math.round(y) + 'px';
  }

  // ------------------------------------------------------------------ centred dialogs
  // dialog({ eyebrow, title, body, blocks:[{ title, sub, items:[...] }], actions:[[label, run, kind]] })
  function dialog(o) {
    const root = el('div', 'position:fixed; inset:0; z-index:1200; display:grid; place-items:center; padding:16px; background:rgba(3,5,8,0.66); backdrop-filter:blur(4px); ' + FONT);
    const close = () => { removeEventListener('keydown', onKey, true); root.remove(); o.onClose && o.onClose(); };
    const onKey = e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); } };
    const card = el('div', 'width:min(480px, 100%); max-height:calc(100vh - 32px); overflow:auto; box-sizing:border-box; display:flex; flex-direction:column; gap:12px; padding:20px; border-radius:22px; background:rgba(16,22,30,0.98); border:1px solid rgba(255,255,255,0.12); box-shadow:0 40px 90px -30px rgba(0,0,0,0.9);');
    card.setAttribute('role', 'dialog');
    card.addEventListener('click', e => e.stopPropagation());
    root.addEventListener('click', close);
    if (o.eyebrow) card.append(txt('div', "font-family:'JetBrains Mono', monospace; font-size:10px; letter-spacing:.12em; color:" + (o.tone || CORAL) + ';', o.eyebrow.toUpperCase()));
    card.append(txt('div', 'font-size:20px; font-weight:750; line-height:1.25;', o.title));
    if (o.body) card.append(txt('div', 'font-size:14px; line-height:1.6; color:#C2CDD8;', o.body));
    (o.blocks || []).forEach(b => {
      const box = el('div', 'display:flex; flex-direction:column; gap:6px; padding:12px 14px; border-radius:14px; background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.07);');
      box.append(el('div', 'display:flex; align-items:baseline; gap:8px; flex-wrap:wrap;', [txt('span', 'font-size:14.5px; font-weight:700;', b.title), b.sub ? txt('span', "font-family:'JetBrains Mono', monospace; font-size:11px; color:#6B7A8A;", b.sub) : null]));
      (b.items || []).forEach(it => box.append(el('div', 'display:flex; gap:8px; font-size:13px; line-height:1.55; color:#C2CDD8;', [txt('span', 'flex:none; color:' + CORAL + ';', '•'), txt('span', '', it)])));
      card.append(box);
    });
    const acts = el('div', 'display:flex; gap:8px; justify-content:flex-end; flex-wrap:wrap; margin-top:4px;');
    (o.actions || [['OK', null, 'p']]).forEach(([label, run, kind]) => acts.append(btn(label, kind, () => { close(); run && run(); })));
    card.append(acts); root.append(card); document.body.append(root);
    addEventListener('keydown', onKey, true);
    setTimeout(() => { try { acts.lastChild.focus({ preventScroll: true }); } catch (e) {} }, 60);
    return close;
  }

  // After the 5-step welcome: offer the Getting started checklist.
  function offerChecklist() {
    dialog({ eyebrow: 'Next step', title: 'Want a hand getting set up?', body: 'A short checklist shows what is left to do: your login, an AI account, your first project and more. It ticks itself off as you go.',
      actions: [['Not now', null, 'q'], ['Show me the checklist', () => { const d = D(); d && d.go && d.go('guides'); }, 'p']] });
  }

  // ------------------------------------------------------------------ "What's new" after an update
  // Asks the updates module (if it's switched on) for the version. When it differs from the last one
  // seen (prefs.seenVersion), shows the changelog entries in between, once. A first visit only remembers.
  const cmp = (a, b) => { const pa = String(a || '').split('.').map(n => parseInt(n, 10) || 0), pb = String(b || '').split('.').map(n => parseInt(n, 10) || 0);
    for (let k = 0; k < 3; k++) { if ((pa[k] || 0) !== (pb[k] || 0)) return (pa[k] || 0) - (pb[k] || 0); } return 0; };
  function cmd(name, payload) {
    return D().api('/modules/commands/' + name, { method: 'POST', body: { arg: payload == null ? '' : JSON.stringify(payload) } })
      .then(r => { let v = r && r.result !== undefined ? r.result : r; if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) {} } return v; });
  }
  let newsChecked = false;
  function checkNews() {
    const d = D(); if (newsChecked || !d || !d.store || !d.store.loaded || d.store.needKey || cur) return;
    if (d.shellIdle && !d.shellIdle()) return;
    newsChecked = true;
    cmd('den-version').then(v => {
      if (!v || !v.version) return;
      const seen = prefs().seenVersion;
      if (!seen) { d.setPref('seenVersion', v.version); return; }
      if (seen === v.version) return;
      return cmd('den-changelog').then(c => {
        d.setPref('seenVersion', v.version);
        const list = ((c && c.entries) || []).filter(e => cmp(e.version, seen) > 0 && cmp(e.version, v.version) <= 0).slice(0, 6);
        if (!list.length) return;
        dialog({ eyebrow: 'Updated to ' + v.version, title: 'What’s new in the Den', tone: '#6FCF97',
          blocks: list.map(e => ({ title: e.title || ('Version ' + e.version), sub: e.version + (e.date ? ' · ' + e.date : ''), items: e.items || [] })),
          actions: [['See all updates', () => d.go && d.go('updates'), ''], ['Got it', null, 'p']] });
      });
    }).catch(() => {});   /* the updates module isn't switched on here: nothing to show */
  }
  setInterval(() => { try { checkNews(); } catch (e) {} }, 3000);

  window.DenTour = {
    TOURS, list: () => TOURS.map(t => ({ id: t.id, title: t.title, blurb: t.blurb, steps: t.steps.length, page: t.page, done: done()[t.id] || '' })),
    start, stop: () => stop('skipped'), active: () => (cur ? cur.tour.id : null),
    forPage: page => PAGE_TOUR[page] || null,
    startFor: page => { const id = PAGE_TOUR[page]; if (id) return start(id); const d = D(); d && d.go && d.go('guides'); try { d.say('No tour for this page yet', 'Here are all the tours', '#7FB2FF'); } catch (e) {} return false; },
    reset: () => { try { D().setPref('toursDone', {}); } catch (e) {} },
    dialog, offerChecklist, cmd, cmpVersion: cmp,
  };
})();
