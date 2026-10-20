// Tushar's Workspace - dashboard entry point: API-key gate, hash router, render loop,
// actions (event delegation on data-action / data-submit / data-input), gestures,
// keyboard shortcuts and polling.

import { store, subscribe, apiKey, endpoints, uploadDocument, downloadDocument } from './api.js';
import { more, refresh, startPolling, loadThread, loadProject, loadClient, loadTeam, loadFeed, loadCrm, loadPersonal, loadTimeline, loadBriefing, loadMessages, loadMessagesSoon, loadBin, loadHidden, loadPad, loadChecklist, loadSearchSoon, dismissTask, projectById, taskById, generalProject,
  realProjects, pendingApprovals, queuedTasks, bulkPlan, isGeneral, rootId, projectStatus, crmOnly } from './data.js';
import { derive, commandOf } from './derive.js';
import { isAssistant } from './views/assistant-card.js';
import { morph } from './morph.js';
import { polish } from './polish.js';
import { pageSwipe, swipeDecision, dragReorder, dragSort, onDoubleTap, autoGrow } from './gestures.js';
import { plural, taskName, modeInfo } from './format.js';
import { modelLabel, clock, dayWord } from './ui.js';
import { NotificationCenter, parseToast, toastKind } from './notifications.js';
import { octopusSVG } from './octopus.js';
import { topbar, sidebar, tabbar, homeSkeleton, listSkeleton } from './views/shell.js';
import { splash, gate, markSeen } from './views/splash.js';
import { home, askOverlay } from './views/home.js';
import { sidePanels, queueRail, rightDrawer } from './views/queue-panel.js';
import { projects } from './views/projects.js';
import { project } from './views/project.js';
import { recipes } from './views/recipes.js';
import { activity } from './views/activity.js';
import { bin } from './views/bin.js';
import { hidden } from './views/hidden.js';
import { messages } from './views/messages.js';
import { personal, PERSONAL_TABS } from './views/personal.js';
import { chatPane, emptyPane, paneChip, tabMenu, MENTION_RE, lastReal, replyAccount } from './views/chat.js';
import { offlinePopup } from './views/offline.js';
import { statusBanners } from './views/status-banner.js';
import { voicePane } from './views/voice-pane.js';
import { dictate } from './voice.js';
import { approvalPane } from './views/approval.js';
import { sheet, WHENS } from './views/sheets.js';
import { usesClaudeAccount } from './views/accounts.js';
import { quickButtons } from './views/quick.js';
import { FEATURES } from './features.js';
import { searchItems, spotItems, spotlight } from './views/search.js';
import { routing } from './views/routing.js';
import { providers } from './views/providers.js';
import { settings } from './views/settings.js';
import { tourSeen, markTourSeen, TOUR_LENGTH } from './views/tour.js';
import { voiceSettings } from './views/voice-settings.js';
import { usage as tokenUsage } from './views/usage.js';
import { octopusActions, octoInput, octoSent } from './octopus-ui.js';
import { octoApi, startOctopusData, onStoreChange } from './octopus-data.js';
import { calendarPage, briefingPage } from './views/calendar.js';
import { timelineActions, timelineInput, calState } from './timeline-ui.js';
import { phaseActions, phaseInput } from './phases-ui.js';
import { overviewActions, overviewInput, newProjectBody, NEW_PROJECT } from './crm-ui.js';
import { crmTableActions, crmtInput, crmDragSetup } from './crm-table.js';
import { clientsPage, clientPage } from './views/clients.js';
import { clientActions, clientInput } from './clients-ui.js';
import { contactPage } from './views/contacts.js';
import { teamPage, employeePage } from './views/team.js';
import { teamActions, teamInput } from './team-ui.js';
import { crmActivityActions, crmActivityInput } from './crm-activity-ui.js';
import { crmPage } from './views/crm-dash.js';
import { crmDashActions } from './crm-dash-ui.js';
import { padActions, padInput, padFlush } from './pad-ui.js';
import { checklistActions, checklistInput, chkUndoKey } from './checklist-ui.js';
import { checklistFull } from './views/checklist.js';
import { notes } from './views/pad.js';
import { backupActions, backupPicked } from './views/backup.js';
import { sitesActions } from './views/sites.js';
import { previewActions } from './preview.js';
import { shortcutFor, comboOf, setBinding, resetBinding, resetAll as resetShortcuts } from './shortcuts.js';
import { setRedraw, addImages, removeImage, clearSent, readyIds, imageProblem, draftOf, MAX_IMAGES } from './attach.js';

const root = document.getElementById('app');
const KEY_STORAGE = 'cq_api_key';
const LAST_PROJECT = 'octopus-last-project';
const SKELETON_MS = 520;
const PANE_WIDTH = 'octopus-pane-w';
const QUEUE_WIDTH = 'octopus-queue-w';
const QUEUE_DEFAULT = 380, QUEUE_MIN = 280;
const SIDEBAR_PREF = 'octopus-sidebar';
const SIDEBAR_WIDE = 1280;   // from here the sidebar sits beside the page; below (down to 900) it's a slide-over drawer
const SIDEBAR_WIDTH = 'octopus-sidebar-w';
const PANE_DEFAULT = 460, PANE_MIN = 360, MAIN_MIN = 380;
const SIDEBAR_DEFAULT = 250, SIDEBAR_MIN = 200, SIDEBAR_MAX = 420;
const VIEWS = ['home', 'projects', 'recipes', 'activity', 'personal', 'messages', 'routing', 'providers', 'settings', 'voice', 'usage', 'calendar', 'briefing', 'bin', 'hidden', 'notes', 'clients', 'crm', 'team'];
const TAB_VIEWS = VIEWS.slice(0, 6);   // the phone tab bar, in order (shell.js tabbar)
const PROJECT_TABS = ['overview', 'work', 'backlog', 'phases', 'timeline', 'todos', 'notes', 'documents', 'activity', 'recipes', 'settings'];

// ---------------------------------------------------------------- UI state (on top of api.js's store.ui)
const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch {} return null; };
Object.assign(store.ui, {
  booted: false, splashLeaving: false, bootDate: new Date(), bootRand: Math.random(), gateError: '',
  palette: null, onboard: { name: '', path: '', category: 'work', error: '' }, todoText: '', blText: '', blModel: null, blAccount: '', blMode: null, blMenu: false, todoOpen: {}, qOpen: {}, todoEdit: null, runReview: null, bulk: null, qSel: {}, lazy: {}, actSel: {}, quickEdit: false, quickNew: null, noteText: '', noteKind: 'guideline', noteEdit: null, reply: '', replyMode: null, replyModel: null, replyAccount: null, replyMenu: false, acctMenu: false, denyNote: '',
  scope: ['all', 'work', 'personal'].includes(ls('octopus-scope')) ? ls('octopus-scope') : 'all',
  busy: {}, polishing: null, focus: null, tabs: [], drafts: {}, lastProjectId: Number(ls(LAST_PROJECT)) || null,
  personalTab: 'email', sidebarPref: ls(SIDEBAR_PREF) !== '0', navDrawer: false, rdrawer: null /* phone right drawer: 'queue' | 'messages' | 'calendar' | 'todos' */,
  // Sidebar rows that can fold open (Projects list, Activity filters, Calendar's upcoming items); remembered per device.
  // Sidebar sub-lists always start folded; opening one lasts only until the next reload.
  navExpand: { projects: false, clients: false, activity: false, calendar: false },
  secFold: (() => { try { return JSON.parse(ls('octopus-sec-fold') || '{}'); } catch { return {}; } })(),
  restart: null,   // { pid, waitingOn } while Restart Octopus is in progress
  offlineHidden: null, offlineChecking: false, refreshing: false,   // the offline popup: dismissed for this outage (its offlineSince), Try now in flight
  // Header search. open = results showing (on a phone: the header is the search box); sel = highlighted result;
  // chats = the server's message matches for chatsFor.
  search: { q: '', open: false, sel: 0, chats: [], chatsFor: '' },
  spot: { open: false, sel: 0, sending: false, error: '' },
  // Project calendar (views/calendar.js): the month on screen, the picked day and project; the add / edit sheet.
  docs: { q: '', sort: 'new', type: 'all', view: ['list', 'compact', 'grid'].includes(ls('doc-view')) ? ls('doc-view') : 'list', editId: null, editName: '' },
  cal: calState(), evEdit: null, tlShowDone: false, phEdit: null, phGen: null, phOpen: {},
});
store.ui.ask.touched = false;
const ui = () => store.ui;

let interacting = false, pending = false, frame = 0;
function requestRender() {
  if (interacting) { pending = true; return; }
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; render(); });
}
subscribe(() => { checkAutoOpen(); onStoreChange(); requestRender(); });
const set = patch => {
  const prev = store.ui.edit;
  Object.assign(store.ui, patch);
  // Editing a queued message holds it (see openEdit); closing the editor lets it go again.
  if (prev && prev.held && !(store.ui.edit && store.ui.edit.id === prev.id)) releaseEditHold(prev.id);
  requestRender();
};
// Quietly un-pause a task that openEdit paused. Doesn't touch one the user paused themselves.
function releaseEditHold(id) {
  const t = store.data.tasks.find(x => x.id === id);
  if (!t || !t.paused) return Promise.resolve();
  setData({ tasks: store.data.tasks.map(x => (x.id === id ? { ...x, paused: 0 } : x)) });
  return more.play(id).catch(fail).then(() => again());
}
const setData = patch => { store.data = { ...store.data, ...patch }; requestRender(); };

// ---------------------------------------------------------------- toasts + notification centre (notifications.js)
// notify.toast(text, { action, actionLabel, taskId }): top-right toast; taskId makes tapping it open that task.
let notify = null;
const toast = (text, opts) => { if (notify) notify.toast(text, opts); };
const fail = e => toast(e && e.message ? e.message : String(e));

// News about the chat that's open on screen (mode changed, proceeding, approved / denied) shows as a
// short note just above its reply box, not a top-right toast. The full line still lands in the bell's list.
// Anywhere else it falls back to the normal toast.
let noteTimer = 0, noteAction = null;
function chatToast(short, taskId, { full = short, action, actionLabel } = {}) {
  const p = ui().pane;
  if (taskId == null || document.hidden || !p || p.type !== 'chat' || paneRoot(p) !== rootOf(taskId)) return toast(full, { taskId, action, actionLabel });
  if (notify) notify.add({ ...parseToast(full), taskId, read: true }, { toast: false });
  clearTimeout(noteTimer);
  noteAction = action || null;
  const id = ((ui().chatNote && ui().chatNote.id) || 0) + 1;
  set({ chatNote: { id, root: rootOf(taskId), text: short, kind: toastKind(full), actionLabel: action ? actionLabel : null } });
  noteTimer = setTimeout(() => {
    set({ chatNote: { ...ui().chatNote, leaving: true } });     // fade out, then go
    noteTimer = setTimeout(() => { noteAction = null; set({ chatNote: null }); }, 200);
  }, action ? 6000 : 2400);
}

// Notifications point at a chat's root task. What counts is the chat as a whole: waiting
// on an approval anywhere in it, otherwise its newest message's status. null = gone.
function chatStatus(id) {
  const d = store.data, rid = rootOf(id);
  const msgs = d.tasks.filter(t => t.id === rid || t.thread_id === rid);
  if (!msgs.length) return null;
  if (pendingApprovals(d).some(a => msgs.some(t => t.id === a.task_id))) return 'approval';
  return msgs.reduce((a, b) => (b.id > a.id ? b : a)).status;
}
// Phone / tablet: the chat fills the screen, so news about it shows up right there instead of as a toast.
function viewingChat(id) {
  const p = ui().pane;
  return !isDesk() && !document.hidden && !!p && p.type === 'chat' && paneRoot(p) === rootOf(id);
}
function openFromNotification(id) {
  const rid = rootOf(id);
  const a = pendingApprovals().find(x => rootOf(x.task_id) === rid);
  a ? openApproval(a.id) : openChat(rid);
}

// The bell and .notif-root are rendered empty and keyed, so morph keeps the same elements;
// only a brand-new .app (after the key gate) needs a fresh NotificationCenter.
function mountNotifications() {
  const nroot = root.querySelector('.notif-root'), bell = document.getElementById('bell');
  if (!nroot || !bell || (notify && notify.root === nroot && notify.bell === bell)) return;
  notify = new NotificationCenter({
    root: nroot, bell,
    statusOf: chatStatus,
    onOpenTask: openFromNotification,
    emptyArt: () => octopusSVG('sleep'),
    modelLabel: t => modelLabel(t.model_id),
    // Approvals: show what it wants to run and let it be decided right in the toast / panel row.
    describeApproval: a => (isAssistant(a) ? '' : '$ ') + commandOf(a),
    approvalPending: id => pendingApprovals().some(a => a.id === id),
    onDecide: (id, ok) => (ok ? approve(id) : deny(id)),
    isViewing: viewingChat,
  });
  feedNotifications();
}

// Derive notifications from each fresh snapshot. Tasks carry a short generated title;
// show that rather than the whole prompt.
let notifySrc = null, notifyData = null, notifyProjects = null;
function feedNotifications() {
  const d = store.data;
  if (!notify || !d.loaded) return;
  if (d !== notifySrc) { notifySrc = d; notifyData = { ...d, tasks: d.tasks.map(t => (t.title ? { ...t, prompt: t.title } : t)) }; }
  // A different set of visible projects (the Work / Personal switch) brings older tasks in all
  // at once: take them in as a fresh, silent baseline instead of toasting each one as new.
  const ids = d.projects.map(p => p.id).join();
  if (ids !== notifyProjects) { notifyProjects = ids; notify.prevTasks = null; }
  notify.ingest(notifyData);
}
subscribe(feedNotifications);

const isDesk = () => window.innerWidth >= 900;
const isWideDesk = () => window.innerWidth >= SIDEBAR_WIDE;
function rememberProject(pid) {
  const p = projectById(pid);
  if (!p || isGeneral(p)) return;
  store.ui.lastProjectId = pid; ls(LAST_PROJECT, String(pid));
}
const defaultProjectId = () => {
  const u = ui();
  if (u.view === 'project' && projectById(u.projectId)) return u.projectId;
  if (u.lastProjectId && projectById(u.lastProjectId)) return u.lastProjectId;
  const real = realProjects()[0];
  return real ? real.id : (generalProject() || {}).id;
};

// ---------------------------------------------------------------- router (#/home, #/projects, #/project/12, #/recipes, #/activity, #/personal/email)
let skeletonTimer = 0;
const slugOf = p => String(p.name || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || String(p.id);
const projectHash = (p, tab) => `#/project/${slugOf(p)}/${tab || 'work'}`;
// The URL names the project, not its id: #/project/the-den/phases. Old #/project/12 links still resolve.
function projectFromSlug(key) {
  const list = store.data.projects || [];
  const k = decodeURIComponent(key || '').toLowerCase();
  return list.find(p => slugOf(p) === k) || (/^\d+$/.test(k) ? list.find(p => p.id === Number(k)) : null) || null;
}
let routePending = false;
function parseHash() {
  const [v, id, sub] = location.hash.replace(/^#\/?/, '').split('/');
  if (v === 'project' && id) {
    const p = projectFromSlug(id);
    if (!p && !store.data.loaded) { routePending = true; return { view: 'project', projectId: null, pending: true }; }
    return { view: 'project', projectId: p ? p.id : Number(id) || 0, projectTab: PROJECT_TABS.includes(sub) ? sub : null };
  }
  if (v === 'client' && /^\d+$/.test(id || '')) return { view: 'client', projectId: ui().projectId, clientId: Number(id) };
  if (v === 'contact' && /^\d+$/.test(id || '')) return { view: 'contact', projectId: ui().projectId, contactId: Number(id) };
  if (v === 'employee' && /^\d+$/.test(id || '')) return { view: 'employee', projectId: ui().projectId, employeeId: Number(id) };
  if (v === 'personal') return { view: 'personal', projectId: ui().projectId, personalTab: PERSONAL_TABS.some(([k]) => k === id) ? id : 'email' };
  if (v === 'task' && /^\d+$/.test(id || '')) return { view: 'home', projectId: ui().projectId, openTask: Number(id) };
  return { view: VIEWS.includes(v) ? v : 'home', projectId: ui().projectId };
}
function applyRoute() {
  const r = parseHash();
  if (r.pending) return;
  const u = ui();
  // #/task/12 (an "Open in new tab" link): land on Home with that chat open.
  if (r.openTask) {
    history.replaceState(null, '', '#/home');
    if (u.view !== 'home') set({ view: 'home', skeleton: false });
    return openChat(r.openTask);
  }
  const tabOnly = r.view === 'project' && r.view === u.view && r.projectId === u.projectId && r.projectTab && r.projectTab !== u.projectTab;
  const changed = r.view !== u.view || (r.view === 'project' && r.projectId !== u.projectId) || (r.view === 'personal' && r.personalTab !== u.personalTab)
    || (r.view === 'client' && r.clientId !== u.clientId) || (r.view === 'contact' && r.contactId !== u.contactId) || (r.view === 'employee' && r.employeeId !== u.employeeId);
  if (r.view === 'project') {
    // Keep the address canonical: project name + the tab that's open (a bare or old-style link gets filled in).
    const p = projectById(r.projectId);
    const tab = r.projectTab || (r.projectId === u.projectId && u.view === 'project' ? u.projectTab : 'work');
    r.projectTab = tab;
    if (p) { const h = projectHash(p, tab); if (location.hash !== h) history.replaceState(null, '', h); }
  }
  if (tabOnly) { if (r.projectTab === 'overview') loadFeed('project', r.projectId).catch(() => {}); return set({ projectTab: r.projectTab }); }
  if (!changed) return requestRender();
  const patch = { view: r.view, projectId: r.projectId, skeleton: true };
  if (r.view === 'personal') patch.personalTab = r.personalTab;
  if (r.view === 'client') { patch.clientId = r.clientId; patch.clEdit = null; patch.clDel = null; patch.cnNew = null; patch.cnEdit = null; }
  if (r.view === 'contact') { patch.contactId = r.contactId; patch.cnEdit = null; }
  if (r.view === 'employee') { patch.employeeId = r.employeeId; patch.tmEdit = null; patch.tmTask = null; patch.tmLeave = null; patch.tmDel = null; }
  if (r.view === 'client' || r.view === 'contact' || r.view === 'project') { patch.actNew = null; patch.fuNew = null; }
  // A CRM-only project has nothing to run, so it opens on its record.
  if (r.view === 'project') patch.projectTab = r.projectTab || (crmOnly(projectById(r.projectId)) ? 'overview' : 'work');
  if (!isDesk()) { patch.pane = null; patch.tabs = []; }
  if (r.view === 'project') {
    rememberProject(r.projectId);
    // The Ask box on a project page always sends to that project (none for a CRM-only one - it can't run anything).
    u.ask.projectId = crmOnly(projectById(r.projectId)) ? null : r.projectId;
    loadProject(r.projectId).catch(() => {});
  }
  set(patch);
  if (r.view === 'personal') loadPersonal().catch(() => {});
  if (r.view === 'client') loadClient(r.clientId).catch(() => {});
  if (r.view === 'client') loadFeed('client', r.clientId).catch(() => {});
  if (r.view === 'contact') loadFeed('contact', r.contactId).catch(() => {});
  if (r.view === 'team' || r.view === 'employee') loadTeam().catch(() => {});
  if (r.view === 'project' && patch.projectTab === 'overview') loadFeed('project', r.projectId).catch(() => {});
  if (r.view === 'crm' || r.view === 'home') loadCrm().catch(() => {});
  if (r.view === 'messages') loadMessages().catch(() => {});
  if (r.view === 'bin') loadBin().catch(() => {});
  if (r.view === 'notes') loadPad().catch(() => {});
  else padFlush();
  if (r.view === 'hidden') loadHidden().catch(() => {});
  if (['home', 'calendar', 'project', 'briefing', 'notes'].includes(r.view)) loadTimeline().catch(() => {});
  if (r.view === 'briefing') loadBriefing().catch(() => {});
  const main = document.getElementById('main');
  if (main) main.scrollTop = 0;
  clearTimeout(skeletonTimer);
  skeletonTimer = setTimeout(() => set({ skeleton: false }), SKELETON_MS);
}
// Where a clickable row leads, as an address a new browser tab can open (null when it isn't a plain destination).
function linkUrl(el) {
  const d = el.dataset, base = location.pathname + location.search;
  let hash = null;
  if (d.action === 'go' && d.view) hash = `#/${d.view}`;
  else if (d.action === 'goPersonal' && d.tab) hash = `#/personal/${d.tab}`;
  else if (d.action === 'openClient') hash = `#/client/${d.id}`;
  else if (d.action === 'openContact') hash = `#/contact/${d.id}`;
  else if (d.action === 'openEmployee') hash = `#/employee/${d.id}`;
  else if (d.action === 'openProject') { const p = projectById(Number(d.id)); hash = p ? projectHash(p, crmOnly(p) ? 'overview' : 'work') : null; }
  else if (d.action === 'openChat' || d.action === 'openApprovalForTask') hash = `#/task/${rootOf(Number(d.id))}`;
  return hash && base + hash;
}
function navigate(view, projectId, tab) {
  // for 'personal' the second argument is the tab (email | calendar | reminders | connections); for 'project', tab opens one of its tabs
  const proj = view === 'project' ? projectById(projectId) : null;
  const hash = proj ? projectHash(proj, tab) : view === 'client' ? `#/client/${projectId}` : view === 'contact' ? `#/contact/${projectId}` : view === 'employee' ? `#/employee/${projectId}` : view === 'project' ? `#/project/${projectId}${tab ? '/' + tab : ''}` : view === 'personal' ? `#/personal/${projectId || ui().personalTab || 'email'}` : `#/${view}`;
  if (location.hash !== hash) location.hash = hash; else applyRoute();
}
// A link opened before the project list arrived: finish routing once it has.
subscribe(() => { if (routePending && store.data.loaded) { routePending = false; applyRoute(); } });
window.addEventListener('hashchange', applyRoute);

function neighbors() {
  if (isDesk()) return { prev: null, next: null };
  const v = ui().view;
  if (v === 'project') return { prev: () => navigate('projects'), next: null };
  if (v === 'client' || v === 'contact') return { prev: () => navigate('clients'), next: null };
  if (v === 'employee') return { prev: () => navigate('team'), next: null };
  const i = TAB_VIEWS.indexOf(v);   // only across the tab bar: the drawer's pages (Routing, Settings…) aren't next to Messages
  return { prev: i > 0 ? () => navigate(TAB_VIEWS[i - 1]) : null, next: i >= 0 && i < TAB_VIEWS.length - 1 ? () => navigate(TAB_VIEWS[i + 1]) : null };
}

// ---------------------------------------------------------------- render
function context() {
  const d = store.data, u = ui(), now = Date.now();
  const w = derive(d, now);
  const desk = isDesk(), navOverlay = !isWideDesk();
  // Wide screens: open unless the user collapsed it (remembered). Narrower desktops and phones: closed until opened, then it floats over the page.
  const navOpen = navOverlay ? u.navDrawer : u.sidebarPref;
  // Desktop chats live inside the Messages sidebar (not a column of their own) unless floated or minimised.
  const docked = desk && !!u.pane && !u.paneMin && !u.paneFloat && (u.side === 'messages' || u.side === 'queue');   // u.side: the open sidebar that hosts the chat
  return { d, ui: u, w, now, desk, docked, navOverlay, navOpen, compact: u.density === 'compact', project: u.view === 'project' ? projectById(u.projectId) : null };
}

function page(c) {
  if (!c.d.loaded || c.ui.skeleton || !c.ui.booted) return c.ui.view === 'home' ? homeSkeleton(c) : listSkeleton(c);
  switch (c.ui.view) {
    case 'projects': return projects(c);
    case 'project': return project(c);
    case 'recipes': return recipes(c);
    case 'activity': return activity(c);
    case 'messages': return messages(c);
    case 'personal': return personal(c);
    case 'routing': return routing(c);
    case 'providers': return providers(c);
    case 'settings': return settings(c);
    case 'voice': return voiceSettings(c);
    case 'usage': return tokenUsage(c);
    case 'calendar': return calendarPage(c);
    case 'briefing': return briefingPage(c);
    case 'bin': return bin(c);
    case 'hidden': return hidden(c);
    case 'notes': return notes(c);
    case 'clients': return clientsPage(c);
    case 'client': return clientPage(c);
    case 'contact': return contactPage(c);
    case 'team': return teamPage(c);
    case 'employee': return employeePage(c);
    case 'crm': return crmPage(c);
    default: return home(c);
  }
}

function pane(c) {
  const p = c.ui.pane;
  if (!p) return '';
  if (p.type === 'empty') return emptyPane(c);
  if (p.type === 'voice') return voicePane(c);
  if (p.type === 'approval') {
    const a = pendingApprovals(c.d).find(x => x.id === p.approvalId);
    if (a) return approvalPane(c, a);
    // Decided (here or elsewhere): show the chat it belongs to instead.
    if (p.taskId != null) return chatPane(c, p.taskId);
    return '';
  }
  return chatPane(c, p.taskId);
}

function appHTML(c) {
  if (!apiKey()) return gate(c);
  const minimized = c.desk && c.ui.paneMin && c.ui.pane;      // chat window shrunk to a chip: the page gets the room back
  const floating = c.desk && c.ui.paneFloat && !minimized;
  const columnPane = c.ui.pane && !minimized && !floating && !c.docked;
  return `<div class="app${c.desk ? ' app--desk' : ''}${columnPane ? ' app--pane' : ''}${floating ? ' app--float' : ''}${c.desk && c.ui.sides.length ? ' app--queue' : ''}" data-density="${c.ui.density}" data-key="app">
    ${sidebar(c)}
    ${c.desk && c.navOpen ? '<div class="sidebar-resizer" data-key="sidebar-resizer" title="Drag to resize · double-click to reset" aria-hidden="true"></div>' : ''}
    <div class="app__work" data-key="work">
      ${c.desk ? topbar(c) : ''}
      ${c.desk ? statusBanners(c) : ''}
      <div class="app__row" data-key="row">
        <main class="app__main" id="main" data-key="main">
          ${c.desk ? '' : topbar(c)}
          ${c.desk ? '' : statusBanners(c)}
          <div class="pager" data-pager data-live-style>${page(c)}</div>
        </main>
        ${minimized ? paneChip(c) : c.docked ? '' : pane(c)}
        ${columnPane ? '<div class="pane-resizer" data-key="pane-resizer" title="Drag to resize · double-click to reset" aria-hidden="true"></div>' : ''}
      </div>
    </div>
    ${c.desk ? sidePanels(c, c.docked ? pane(c) : '') : ''}
    ${c.desk ? queueRail(c) : ''}
    ${c.desk ? '' : tabbar(c)}
    ${c.desk ? '' : rightDrawer(c)}
    ${askOverlay(c)}
    ${checklistFull(c)}
    ${sheet(c)}
    ${tabMenu(c)}
    ${spotlight(c)}
    ${offlinePopup(c)}
    <div class="notif-root" data-key="notif-root"></div>
    ${c.ui.booted ? '' : splash(c)}
  </div>`;
}

const bound = new WeakSet();
let lastPaneKey = null, searchScroll = false, firstPaneRender = true, lastChatItems = 0;
const seenReplies = new Set();
let reopen = null;                          // pane key of a chat the user just clicked open (even if it was already showing)
let stick = { key: null, until: 0 };       // a freshly opened chat keeps landing on the newest message while its thread loads in
for (const ev of ['wheel', 'touchstart', 'keydown']) root.addEventListener(ev, () => { stick.until = 0; }, { passive: true, capture: true });
const ARRIVALS = '.msg-octo, .msg-approval';     // what lands in a chat on its own: replies and requests for your OK
const offsetIn = (pane, el) => el.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
// The "New reply" button goes once that reply has scrolled up into view above the composer.
root.addEventListener('scroll', e => {
  const n = store.ui.chatNew, pane = e.target;
  if (!n || !(pane instanceof Element) || !pane.matches('.pane')) return;
  const el = pane.querySelector(`[data-key="${n.item}"]`), comp = pane.querySelector('.composer');
  if (!el || el.getBoundingClientRect().top < (comp ? comp.getBoundingClientRect().top : pane.getBoundingClientRect().bottom) - 24) set({ chatNew: null });
}, true);

function render() {
  const c = context();
  document.documentElement.classList.toggle('is-desk', c.desk);
  const paneEl = root.querySelector('.pane');
  const nearBottom = paneEl ? paneEl.scrollHeight - paneEl.scrollTop - paneEl.clientHeight < 80 : false;
  const had = document.activeElement;
  const hadId = had && had.id && root.contains(had) && had.matches('input, textarea') ? had.id : '';
  const sel = hadId ? [had.selectionStart, had.selectionEnd] : null;
  morph(root, appHTML(c));
  if (hadId && document.activeElement !== had) {       // the box was swapped out (e.g. a picture strip appeared above it): keep typing there
    const back = document.getElementById(hadId);
    if (back && !back.disabled) { back.focus(); try { back.setSelectionRange(sel[0], sel[1]); } catch {} }
  }
  bindGestures(c);
  mountNotifications();

  const newPane = root.querySelector('.pane');
  const key = newPane ? newPane.getAttribute('data-key') : null;
  const follows = key && (key.startsWith('pane-chat') || key === 'pane-voice');     // panes that read top to bottom, newest last
  const arrivals = newPane ? [...newPane.querySelectorAll(ARRIVALS)] : [];
  const mine = newPane ? [...newPane.querySelectorAll('.msg-you')] : [];
  const sent = mine.some(m => !seenReplies.has(m.dataset.key));        // you just sent one (or one came in from another device)
  const items = newPane ? newPane.querySelectorAll('.chat > *').length : 0;
  const filled = lastChatItems === 0 && items > 0;                      // the chat's thread has just loaded into an open pane
  if (key !== lastPaneKey && store.ui.chatNew) store.ui.chatNew = null;
  const reopened = !!follows && reopen === key;
  const pending = reopened && !(c.d.threads || {})[Number(key.slice(10))];   // chat opened but its thread is still loading
  if (newPane && (key !== lastPaneKey || reopened)) { newPane.scrollTop = follows ? newPane.scrollHeight : 0; if (follows) stick = { key, until: Date.now() + 3000 }; }
  else if (newPane && follows) {
    const reply = arrivals.find(m => !seenReplies.has(m.dataset.key));
    if (stick.key === key && Date.now() < stick.until) newPane.scrollTop = newPane.scrollHeight;
    else if (filled) newPane.scrollTop = newPane.scrollHeight;
    else if (sent && !firstPaneRender) newPane.scrollTop = newPane.scrollHeight;   // your own message always shows
    else if (nearBottom) {
      // A new reply lands: show where it starts, not its end, so a long answer can be read from the top.
      if (reply && !firstPaneRender) newPane.scrollTop = Math.min(newPane.scrollHeight, Math.max(0, offsetIn(newPane, reply) - 12));
      else newPane.scrollTop = newPane.scrollHeight;
    } else if (reply && !firstPaneRender && !store.ui.chatNew) {
      // Scrolled up reading: a "New reply" button above the composer, tap to jump to it.
      store.ui.chatNew = { pane: key, item: reply.dataset.key, approval: reply.matches('.msg-approval') };
      requestRender();
    }
  }
  if (reopen && !pending) reopen = null;
  if (pending) stick.until = Math.max(stick.until, Date.now() + 1500);     // keep following until the thread arrives
  for (const m of arrivals) seenReplies.add(m.dataset.key);
  for (const m of mine) seenReplies.add(m.dataset.key);
  lastChatItems = items;
  firstPaneRender = false;
  lastPaneKey = key;

  if (c.ui.focus) {
    const el = document.getElementById(c.ui.focus);
    // Date and number inputs have no text selection - setSelectionRange throws on them.
    if (el) { el.focus(); try { if (el.setSelectionRange) el.setSelectionRange(el.value.length, el.value.length); } catch {} }
    store.ui.focus = null;
  }
  if (searchScroll) {
    searchScroll = false;
    const sel = root.querySelector('.srow.is-sel');
    if (sel) sel.scrollIntoView({ block: 'nearest' });
  }
}

// Lazy lists (ui.js lazy()): its "Show more" button reveals the next batch.
function loadMore(el) {
  const k = el.dataset.lazy, first = Number(el.dataset.first) || 20, step = Number(el.dataset.step) || 20;
  const cur = Math.max(first, ui().lazy[k] || 0);
  set({ lazy: { ...ui().lazy, [k]: cur + step } });
}
// A drag only reports the rows on screen; the queue items not loaded yet keep their order after them.
const withHidden = ids => [...ids, ...queuedTasks().map(t => t.id).filter(id => !ids.includes(id))];

function bindGestures(c) {
  for (const el of root.querySelectorAll('[data-splash]')) {
    if (bound.has(el)) continue; bound.add(el);
    onDoubleTap(el, dismissSplash);
  }
  for (const el of root.querySelectorAll('[data-pager]')) {
    if (bound.has(el)) continue; bound.add(el);
    pageSwipe(el, neighbors);
  }
  for (const card of root.querySelectorAll('[data-swipe]')) {
    if (bound.has(card)) continue; bound.add(card);
    const id = Number(card.dataset.id);
    swipeDecision(card, card.previousElementSibling, { onApprove: () => approve(id), onDeny: () => deny(id) });
  }
  for (const list of root.querySelectorAll('[data-drag-list]')) {
    if (bound.has(list)) continue; bound.add(list);
    const queue = list.dataset.dragList === 'queue';
    dragReorder(list, {
      onDrop: ids => (queue ? reorderQueue(withHidden(ids.map(Number))) : reorderBacklog(Number(list.dataset.pid), ids.map(Number))),
      group: queue ? () => pickedIds().map(String) : undefined,   // dragging a ticked row brings the other ticked rows along
    });
  }
  for (const list of root.querySelectorAll('[data-chk-drag]')) {   // To-dos: rows of any height
    if (bound.has(list)) continue; bound.add(list);
    dragSort(list, { onDrop: ids => ACTIONS.chkReorder(ids.map(Number)) });
  }
  for (const ta of root.querySelectorAll('textarea[data-autogrow]')) {
    if (!bound.has(ta)) { bound.add(ta); ta._grow = autoGrow(ta, Number(ta.dataset.autogrow)); if ('fit' in ta.dataset) ta._grow.fit(); }   // data-fit: sized to its text from the start
    if (!ta.value) ta.style.height = '';
  }
}

// Hold renders while a finger is on a drag handle or the approval card, so a poll
// can't reset the transform mid-gesture.
document.addEventListener('pointerdown', e => { if (e.target.closest('.handle, [data-swipe]')) interacting = true; }, true);
const endInteract = () => setTimeout(() => { if (!interacting) return; interacting = false; if (pending) { pending = false; requestRender(); } }, 0);
window.addEventListener('pointerup', endInteract, true);
window.addEventListener('pointercancel', endInteract, true);
window.addEventListener('resize', () => { applyWidths(); if (isWideDesk()) store.ui.navDrawer = false; if (isDesk()) store.ui.rdrawer = null; requestRender(); });

// Phone keyboard: iOS keeps the page full height behind the keyboard, so the sticky composer and a sheet's buttons end up under it.
// While it's up, the app shrinks to what's visible (mobile.css html.kb-open) and the tab bar steps aside.
// On <html>, not .app: renders rewrite .app's classes. Android already resizes (interactive-widget in the viewport meta).
const vv = window.visualViewport;
if (vv) {
  const fitKeyboard = () => {
    const open = !isDesk() && Math.abs(vv.scale - 1) < 0.01 && window.innerHeight - vv.height > 120;   // not a pinch-zoom
    const html = document.documentElement;
    html.classList.toggle('kb-open', open);
    html.style.setProperty('--app-h', open ? Math.round(vv.height) + 'px' : '');
    if (open && vv.offsetTop) window.scrollTo(0, 0);
  };
  vv.addEventListener('resize', fitKeyboard);
  vv.addEventListener('scroll', fitKeyboard);
}

// Phone right drawer: swipe the edge grip left to open it, swipe the open panel right to close it.
let rdSwipe = null;
root.addEventListener('touchstart', e => {
  const t = e.touches[0], from = e.target.closest('.rgrip') ? 'grip' : e.target.closest('.rdrawer__panel') && !e.target.closest('.chip-row, .diff, .segmented') ? 'panel' : null;   // not inside a row that scrolls sideways
  rdSwipe = from && e.touches.length === 1 ? { from, x: t.clientX, y: t.clientY } : null;
}, { passive: true });
root.addEventListener('touchend', e => {
  if (!rdSwipe) return;
  const t = e.changedTouches[0], dx = t.clientX - rdSwipe.x, dy = Math.abs(t.clientY - rdSwipe.y), from = rdSwipe.from;
  rdSwipe = null;
  if (Math.abs(dx) < 50 || dy > Math.abs(dx)) return;
  if (from === 'grip' && dx < 0 && !ui().rdrawer) set({ rdrawer: 'queue', navDrawer: false });
  else if (from === 'panel' && dx > 0) set({ rdrawer: null });
}, { passive: true });

// Left sidebar drawer: swipe right from the left screen edge to open it, swipe the open drawer left to close it.
let sbSwipe = null;
root.addEventListener('touchstart', e => {
  const t = e.touches[0], inSide = e.target.closest('.sidebar');
  const from = ui().navDrawer ? (inSide && !e.target.closest('.chip-row') ? 'panel' : null) : t.clientX <= 24 && !ui().rdrawer ? 'edge' : null;
  sbSwipe = from && !isWideDesk() && e.touches.length === 1 ? { from, x: t.clientX, y: t.clientY } : null;
}, { passive: true });
root.addEventListener('touchend', e => {
  if (!sbSwipe) return;
  const t = e.changedTouches[0], dx = t.clientX - sbSwipe.x, dy = Math.abs(t.clientY - sbSwipe.y), from = sbSwipe.from;
  sbSwipe = null;
  if (Math.abs(dx) < 50 || dy > Math.abs(dx)) return;
  if (from === 'edge' && dx > 0) set({ navDrawer: true, rdrawer: null });
  else if (from === 'panel' && dx < 0) set({ navDrawer: false });
}, { passive: true });

// Touch screens have no hover, so details that live in a title (exact token counts, costs, which account a dot is) were unreachable.
// Press and hold anything with a title to see it as a toast. The click that follows the hold is swallowed.
const noHover = window.matchMedia('(hover: none)');
let holdTimer = 0, held = 0;
root.addEventListener('touchstart', e => {
  clearTimeout(holdTimer);
  const el = e.target.closest('[title]');
  if (!noHover.matches || !el || !el.title || e.touches.length > 1 || e.target.closest('.handle, [data-swipe], input, textarea, select')) return;
  holdTimer = setTimeout(() => { held = Date.now(); toast(el.title); }, 500);
}, { passive: true });
for (const ev of ['touchmove', 'touchend', 'touchcancel']) root.addEventListener(ev, () => clearTimeout(holdTimer), { passive: true });
root.addEventListener('click', e => { if (held && Date.now() - held < 800) { held = 0; e.preventDefault(); e.stopPropagation(); } }, true);
root.addEventListener('contextmenu', e => { if (held && Date.now() - held < 800) e.preventDefault(); });

// ---------------------------------------------------------------- resizable sidebar + side pane (desktop)
// The widths live in the --sidebar-w / --pane-w custom properties on <html>, so dragging
// never re-renders. At 1280px+ the pane sits beside the page, so leave the page MAIN_MIN;
// below that it floats over the page and may cover most of it. SIDEBAR_MAX keeps the pane
// above PANE_MIN even at 1280px.
const clampSidebar = w => Math.round(Math.max(SIDEBAR_MIN, Math.min(w, SIDEBAR_MAX)));
const clampPane = w => Math.round(Math.max(PANE_MIN, Math.min(w, window.innerWidth - clampSidebar(sidebarWidth) - (window.innerWidth >= 1280 ? MAIN_MIN : 60))));
let sidebarWidth = Number(ls(SIDEBAR_WIDTH)) || SIDEBAR_DEFAULT;
let paneWidth = Number(ls(PANE_WIDTH)) || PANE_DEFAULT;
const clampQueue = w => Math.round(Math.max(QUEUE_MIN, Math.min(w, (window.innerWidth - clampSidebar(sidebarWidth) - MAIN_MIN) / Math.max(1, ui().cols.length))));   // shared by every open column
let queueWidth = Number(ls(QUEUE_WIDTH)) || QUEUE_DEFAULT;
function applyWidths() {
  const s = document.documentElement.style;
  s.setProperty('--sidebar-w', clampSidebar(sidebarWidth) + 'px');
  s.setProperty('--pane-w', clampPane(paneWidth) + 'px');
  s.setProperty('--queue-w', clampQueue(queueWidth) + 'px');
}
applyWidths();
const RESIZERS = {
  '.sidebar-resizer': { move: x => { sidebarWidth = clampSidebar(x); }, save: () => ls(SIDEBAR_WIDTH, String(sidebarWidth)), reset: () => { sidebarWidth = SIDEBAR_DEFAULT; } },
  '.pane-resizer': { move: x => { paneWidth = clampPane(rowRight() - x); }, save: () => ls(PANE_WIDTH, String(paneWidth)), reset: () => { paneWidth = PANE_DEFAULT; } },
  '.queue-resizer': { move: (x, h) => { queueWidth = clampQueue(h.closest('.queue-panel').getBoundingClientRect().right - x); }, save: () => ls(QUEUE_WIDTH, String(queueWidth)), reset: () => { queueWidth = QUEUE_DEFAULT; } },
};
// The docked chat pane's right edge: the queue sidebar and the right rail sit past it.
const rowRight = () => { const r = root.querySelector('.app__row'); return r ? r.getBoundingClientRect().right : window.innerWidth; };
const resizerAt = el => { for (const sel in RESIZERS) { const h = el.closest(sel); if (h) return [h, RESIZERS[sel]]; } return []; };
document.addEventListener('pointerdown', e => {
  const [handle, r] = resizerAt(e.target);
  if (!handle || e.button) return;
  e.preventDefault();
  handle.setPointerCapture(e.pointerId);
  document.documentElement.classList.add('is-resizing');
  const move = ev => { r.move(ev.clientX, handle); applyWidths(); };
  const up = () => {
    handle.removeEventListener('pointermove', move);
    document.documentElement.classList.remove('is-resizing');
    r.save();
  };
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up, { once: true });
  handle.addEventListener('pointercancel', up, { once: true });
});
document.addEventListener('dblclick', e => {
  const [handle, r] = resizerAt(e.target);
  if (!handle) return;
  r.reset(); applyWidths(); r.save();
});

// Drag a sidebar's header (or a rail tab) onto another sidebar: the left / right quarter puts it in a column beside it,
// the middle splits that column (top half: above it, bottom half: below it).
let dragSide = null;
const dropSpot = e => {
  const panel = e.target.closest && e.target.closest('[data-side-panel]');
  if (!dragSide || !panel || panel.dataset.sidePanel === dragSide) return null;
  const r = panel.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
  const where = x < .25 ? 'before' : x > .75 ? 'after' : y < .5 ? 'above' : 'below';
  return { panel, target: panel.dataset.sidePanel, where };
};
const DROPS = ['before', 'after', 'above', 'below'].map(w => 'is-drop-' + w);
const clearDrop = () => document.querySelectorAll('.' + DROPS.join(', .')).forEach(el => el.classList.remove(...DROPS));
document.addEventListener('dragstart', e => {
  const h = e.target.closest && e.target.closest('[data-drag-side]');
  if (!h) return;
  dragSide = h.dataset.dragSide;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', dragSide);
  document.documentElement.classList.add('is-side-dragging');
});
document.addEventListener('dragover', e => {
  const spot = dropSpot(e);
  clearDrop();
  if (!spot) return;
  e.preventDefault();
  spot.panel.classList.add('is-drop-' + spot.where);
});
document.addEventListener('drop', e => {
  const spot = dropSpot(e);
  if (!spot) return;
  e.preventDefault();
  const key = dragSide;
  clearDrop();
  moveSide(key, spot.target, spot.where);
});
document.addEventListener('dragend', () => { dragSide = null; clearDrop(); document.documentElement.classList.remove('is-side-dragging'); });

function dismissSplash() {
  if (ui().booted || ui().splashLeaving) return;
  set({ splashLeaving: true, skeleton: false });
  markSeen();
  setTimeout(() => {
    set({ booted: true, splashLeaving: false });
    if (apiKey() && !tourSeen() && !ui().sheet) ACTIONS.tourOpen();
  }, 420);
}

// ---------------------------------------------------------------- actions
async function busy(key, fn) {
  if (ui().busy[key]) return;
  set({ busy: { ...ui().busy, [key]: true } });
  try { return await fn(); } finally { const b = { ...ui().busy }; delete b[key]; set({ busy: b }); }
}
const again = () => refresh().catch(() => {});

// Chats open as tabs in the desktop side pane: `tabs` are conversation (root task) ids, `pane` is what's showing.
// Opening another chat adds a tab instead of replacing the current one, and each tab keeps its half-typed reply.
const rootOf = id => { const t = taskById(id); return t ? (t.thread_id || t.id) : id; };
const paneRoot = p => (p && p.taskId != null ? rootOf(p.taskId) : null);

// The open sidebars (ui.sides, left to right) and which one hosts a docked chat (ui.side: a Messages or Queue panel if open).
const SIDE_KEYS = ['queue', 'messages', 'calendar', 'todos'];
const NO_HOST = ['calendar', 'todos'];   // panels that never carry a docked chat
const hostOf = sides => sides.find(k => !NO_HOST.includes(k)) || sides[0] || null;
// Takes the columns (arrays of keys, stacked top to bottom) or a flat list, where each key gets a column of its own.
function sidesPatch(list) {
  const cols = list.map(c => (Array.isArray(c) ? c : [c])).filter(c => c.length), sides = cols.flat();
  ls('octopus-sides', JSON.stringify(sides)); ls('octopus-cols', JSON.stringify(cols));
  const u = ui(), side = sides.includes(u.side) && !NO_HOST.includes(u.side) ? u.side : hostOf(sides);
  // The chat goes when the panel hosting it closes, as before.
  return { sides, cols, side, ...(u.pane && !u.paneFloat && !u.paneMin && side !== u.side && (u.side === 'messages' || u.side === 'queue') ? { pane: null } : {}) };
}
function setSide(key, open) {
  if (!SIDE_KEYS.includes(key)) return;
  const u = ui(), has = u.sides.includes(key);
  if (open === has) return;
  set(sidesPatch(open ? [...u.cols, [key]] : u.cols.map(c => c.filter(k => k !== key))));
  applyWidths();
  if (open && key === 'messages') loadMessages().catch(fail);
  if (open && key === 'calendar' && !store.data.timeline.loaded) loadTimeline().catch(fail);
  if (open && key === 'todos') loadChecklist().catch(fail);
}
// Drag-and-drop: move `key` (open or not, from the rail) next to `target`: `where` is 'before' / 'after' (a column of its own
// left / right of it) or 'above' / 'below' (split target's column, so both share its height).
function moveSide(key, target, where) {
  if (!SIDE_KEYS.includes(key) || key === target) return;
  const u = ui(), was = u.sides.includes(key);
  const rest = u.cols.map(c => c.filter(k => k !== key)).filter(c => c.length);
  const ci = rest.findIndex(c => c.includes(target));
  if (ci < 0) return;
  if (where === 'before' || where === 'after') rest.splice(where === 'after' ? ci + 1 : ci, 0, [key]);
  else rest[ci].splice(rest[ci].indexOf(target) + (where === 'below' ? 1 : 0), 0, key);
  set(sidesPatch(rest));
  applyWidths();
  if (!was && key === 'messages') loadMessages().catch(fail);
  if (!was && key === 'calendar' && !store.data.timeline.loaded) loadTimeline().catch(fail);
  if (!was && key === 'todos') loadChecklist().catch(fail);
}

function showPane(pane, extra) {
  const u = ui(), from = paneRoot(u.pane), to = paneRoot(pane);
  const drafts = { ...u.drafts };
  if (from != null) drafts[from] = u.reply;
  let tabs = isDesk() ? u.tabs : [];
  if (to != null && !tabs.includes(to)) tabs = [...tabs, to];
  set({ pane, tabs, drafts, reply: to != null ? drafts[to] || '' : '', replyMode: null, replyModel: null, replyAccount: null, replyMenu: false, sheet: null, paneMin: false, ...(isDesk() && pane && !u.paneFloat && !u.sides.some(k => !NO_HOST.includes(k)) ? sidesPatch([...u.cols, ['messages']]) : {}), ...extra });
  if (isDesk() && pane && !u.paneFloat && !store.data.messages.loaded) loadMessages().catch(() => {});
}

function openChat(taskId) {
  const rid = rootOf(taskId);
  reopen = 'pane-chat-' + rid;
  showPane({ type: 'chat', taskId: rid });
  loadThread(rid).catch(() => {});
}

function closeTab(id) {
  const u = ui(), tabs = u.tabs.filter(x => x !== id);
  if (u.pinned.includes(id)) { const pinned = u.pinned.filter(x => x !== id); ls('octopus-pinned-tabs', JSON.stringify(pinned)); store.ui.pinned = pinned; }
  const drafts = { ...u.drafts };
  delete drafts[id];
  if (paneRoot(u.pane) !== id) return set({ tabs, drafts });
  // Closing the chat you're looking at: the other tabs stay up with none picked (pane 'empty'), rather than jumping into one.
  set({ pane: tabs.length ? { type: 'empty' } : null, tabs, drafts, reply: '', replyMode: null, replyModel: null, replyAccount: null, replyMenu: false });
}

function closePane() {
  if ((ui().pane || {}).type === 'voice') ACTIONS.voiceStop();     // closing the voice pane ends the voice chat
  if (!isDesk()) return set({ pane: null, tabs: [], drafts: {} });
  const id = paneRoot(ui().pane);
  id != null ? closeTab(id) : set({ pane: null });
}

// Desktop "Close all": shut the side pane and every tab in it (half-typed replies go with them).
function closeAllPanes() {
  if ((ui().pane || {}).type === 'voice') ACTIONS.voiceStop();
  set({ sheet: null, pane: null, paneMin: false, tabs: [], drafts: {}, reply: '', replyMode: null, replyModel: null, replyAccount: null, replyMenu: false });
}

// A task just sent from the Ask box / New task sheet: if the worker picks it up right away, open its chat
// so the answer streams in front of you. Gives up after AUTO_OPEN_MS (it's waiting in line) or as soon as
// you open something else, so it never yanks the view later.
const AUTO_OPEN_MS = 45000;
let autoOpen = null, autoOpenPoll = 0;
function armAutoOpen(taskId) {
  autoOpen = { id: taskId, until: Date.now() + AUTO_OPEN_MS, pane: ui().pane };
  autoOpenPoll = 0;
  checkAutoOpen();
}
function checkAutoOpen() {
  if (!autoOpen) return;
  const u = ui(), t = taskById(autoOpen.id);
  if (Date.now() > autoOpen.until || u.pane !== autoOpen.pane) { autoOpen = null; return; }
  if (!t) return;
  if (t.status === 'queued') {
    if (Date.now() - autoOpenPoll > 2500) { autoOpenPoll = Date.now(); again(); }
    return;
  }
  autoOpen = null;
  if (t.status !== 'cancelled') openChat(t.id);
}
function openApproval(approvalId) {
  const a = pendingApprovals().find(x => x.id === approvalId);
  showPane({ type: 'approval', approvalId, taskId: a ? a.task_id : null }, { denyNote: '' });
  if (a) more.approvalContext(approvalId).then(v => setData({ approvalCtx: { ...store.data.approvalCtx, [approvalId]: v } })).catch(() => {});
}

function afterDecision(approvalId) {
  setData({ approvals: store.data.approvals.filter(a => a.id !== approvalId) });
  const p = ui().pane;
  if (p && p.type === 'approval' && p.approvalId === approvalId && p.taskId != null) openChat(p.taskId);
}
async function approve(id, always = false) {
  const a = pendingApprovals().find(x => x.id === id);
  if (!a) return;
  await busy('ap' + id, async () => {
    try {
      const res = await more.decide(id, true, always ? { always: true } : {});
      const ctx = store.data.approvalCtx[id];
      const p = projectById(a.project_id);
      if (res && res.assistant_result) {
        // Personal-assistant action: say what really happened (it ran, or it failed / expired / was blocked).
        const r = res.assistant_result;
        afterDecision(id);
        toast(r.ok ? (r.text || 'Done').split('\n')[0] : (r.text || 'Not done'), { taskId: a.task_id });
        loadPersonal().catch(() => {});
      } else {
        afterDecision(id);
        chatToast(always ? 'Approved · always allowed' : 'Approved', a.task_id,
          { full: always && ctx ? `Approved · ${ctx.allow_entry} is always allowed in ${p ? p.name : 'this project'}` : `Approved · resuming ${a.task_title || a.task_prompt}` });
      }
    } catch (e) { fail(e); }
  });
  again();
}
async function deny(id, note) {
  const a = pendingApprovals().find(x => x.id === id);
  try {
    const res = await more.decide(id, false, note ? { note } : {});
    set({ denyNote: '' });
    afterDecision(id);
    if (res && res.assistant_result) { toast('Cancelled · nothing was done', { taskId: a && a.task_id }); loadPersonal().catch(() => {}); }
    else chatToast('Denied', a && a.task_id, { full: 'Denied · Octopus will wrap up without it' });
  } catch (e) { fail(e); }
  again();
}

function reorderQueue(ids, { undo = true } = {}) {
  const prev = store.data.tasks.filter(t => ids.includes(t.id)).sort((a, b) => (a.queue_pos ?? 0) - (b.queue_pos ?? 0)).map(t => t.id);
  const pos = new Map(ids.map((id, i) => [id, i]));
  setData({ tasks: store.data.tasks.map(t => (pos.has(t.id) ? { ...t, queue_pos: pos.get(t.id) } : t)) });
  more.reorderQueue(ids).then(() => {
    if (undo) toast('Queue reordered', { action: () => reorderQueue(prev, { undo: false }), actionLabel: 'Undo' });
    again();
  }, fail);
}
function reorderBacklog(pid, ids) {
  const todos = store.data.todos[pid] || [];
  const rest = todos.filter(t => !ids.includes(t.id)).map(t => t.id);
  const order = [...ids, ...rest];
  const byId = new Map(todos.map(t => [t.id, t]));
  const prev = todos.map(t => t.id);
  setData({ todos: { ...store.data.todos, [pid]: order.map(id => byId.get(id)).filter(Boolean) } });
  endpoints.reorderTodos(pid, order).then(v => {
    setData({ todos: { ...store.data.todos, [pid]: v } });
    toast('Backlog reordered', { action: () => endpoints.reorderTodos(pid, prev).then(v2 => setData({ todos: { ...store.data.todos, [pid]: v2 } }), fail), actionLabel: 'Undo' });
  }, fail);
}

// Put deleted backlog items / notes back (they're re-created, so they get new ids), then restore their order.
async function undoDeleteTodos(pid, items, order) {
  const made = await Promise.all(items.map(t => endpoints.addTodo(pid, t.text, undefined, {
    model: t.model || undefined, permission_mode: t.permission_mode || undefined, priority: t.priority ?? undefined, only_account: t.only_account || undefined })));
  const fresh = new Map(items.map((t, i) => [t.id, made[i].id]));
  const ids = order.map(id => fresh.get(id) ?? id);
  const v = await endpoints.reorderTodos(pid, ids);
  setData({ todos: { ...store.data.todos, [pid]: v } });
  toast('Restored · ' + plural(items.length, 'item'));
}
async function undoDeleteNote(pid, n) {
  const made = await endpoints.addNote(pid, n.text, n.kind, n.images || undefined);
  setData({ notes: { ...store.data.notes, [pid]: [...(store.data.notes[pid] || []), made] } });
  toast('Restored');
}

// The pictures being attached to a prompt: 'ask' (home box), 'palette' (New task sheet), 'reply' (the open chat),
// 'todo' (the backlog's Add box), 'blog' (the backlog's chat-style box), 'todoEdit' (the Edit backlog item sheet),
// 'note' (the Notes tab's Add box) or 'noteEdit' (the Edit note sheet).
// Reply, backlog and note drafts are kept per chat / project, so a picture never follows you somewhere else.
function imageDraft(target) {
  if (target === 'ask') return ui().ask.images;
  if (target === 'todo') {
    const pid = ui().projectId;
    if (pid == null) return null;
    const all = ui().todoImages || (store.ui.todoImages = {});
    return all[pid] || (all[pid] = []);
  }
  if (target === 'blog') {
    const pid = ui().projectId;
    if (pid == null) return null;
    const all = ui().blogImages || (store.ui.blogImages = {});
    return all[pid] || (all[pid] = []);
  }
  if (target === 'note') {
    const pid = ui().projectId;
    if (pid == null) return null;
    const all = ui().noteImages || (store.ui.noteImages = {});
    return all[pid] || (all[pid] = []);
  }
  if (target === 'todoEdit') { const e = ui().todoEdit; return e ? e.images || (e.images = []) : null; }
  if (target === 'noteEdit') { const e = ui().noteEdit; return e ? e.images || (e.images = []) : null; }
  if (target === 'palette') { const p = ui().palette; if (!p) return null; return p.images || (p.images = []); }
  if (target === 'reply') {
    const id = paneRoot(ui().pane);
    if (id == null) return null;
    const all = ui().replyImages || (store.ui.replyImages = {});
    return all[id] || (all[id] = []);
  }
  return null;
}
function attachTo(target, files) {
  const list = imageDraft(target);
  if (!list || !files || !files.length) return;
  const left = addImages(list, files);
  if (left) toast(`Only pictures can be attached, up to ${MAX_IMAGES} per message`);
}

async function runAgain(id) {
  const t = taskById(id);
  if (!t) return;
  try {
    if (t.lane === 'api' || t.lane === 'assistant') {
      const r = await more.send(t.prompt, t.project_id, t.lane === 'assistant' ? 'auto' : t.model_id, t.priority, null, t.images);
      toast('Asked again · ' + modelLabel(t.model_id), { taskId: r.task.id });
    } else {
      const r = await endpoints.addTask(t.project_id, t.prompt, t.priority, null, null, t.images);
      toast(`Queued again · #${r.position || '?'}`, { taskId: r.id });
    }
  } catch (e) { fail(e); }
  again();
}

async function runRecipe(id, pid) {
  const r = (store.data.recipes || []).find(x => x.id === id);
  if (!r) return;
  await busy('rc' + id, async () => {
    try {
      if (r.target === 'all' && pid == null) {
        const out = await more.runRecipe(id, {});
        toast(`Queued ${r.name} for ${plural(out.tasks.length, 'project')}`);
      } else {
        const target = pid ?? (/^\d+$/.test(r.target) ? Number(r.target) : defaultProjectId());
        const out = await more.runRecipe(id, { project_id: target });
        const p = projectById(target);
        rememberProject(target);
        toast(`${r.name} · ${p ? p.name : ''} · #${out.tasks[0].position || '?'} in line`);
      }
      set({ sheet: null, palette: null });
    } catch (e) { fail(e); }
  });
  again();
}

function openPalette(pre = {}) {
  const pid = pre.projectId ?? defaultProjectId();
  const s = store.data.projectSettings[pid];
  set({ sheet: 'palette', palette: { text: '', model: 'auto', priority: s ? s.default_priority : 0, when: 'now', sending: false, error: '', projectId: pid, images: [], account: '', mode: '', ...pre }, focus: 'palInput' });
}

async function submitPalette() {
  const pal = ui().palette;
  if (!pal || !pal.text.trim() || pal.sending) return;
  const images = pal.images || [];
  const problem = imageProblem(images) || (pal.when !== 'now' && images.length ? "Scheduled tasks can't carry pictures yet - send it now, or remove them" : '');
  if (problem) { pal.error = problem; return requestRender(); }
  pal.sending = true; pal.error = ''; requestRender();
  const text = pal.text.trim();
  const p = projectById(pal.projectId);
  let sentId = null;
  try {
    if (pal.when !== 'now') {
      const w = WHENS.find(x => x.k === pal.when);
      const date = w.tomorrow ? (() => { const d = new Date(Date.now() + 86400000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; })() : undefined;
      await more.addSchedule({ prompt: text, project_id: pal.projectId, cadence: w.cadence, time: w.time, date, model: pal.model, priority: pal.priority });
      toast('Scheduled · ' + w.l);
    } else {
      const r = await octoApi.send(text, { project_id: p && !p.is_general ? p.id : null, model: pal.model, priority: pal.priority, account: pal.account || undefined, permission_mode: pal.mode || undefined,
        images: images.length ? readyIds(images) : undefined });
      clearSent(images);
      const out = octoSent(r);
      if (out.plan) { rememberProject(pal.projectId); return; }   // the plan card took over the sheet
      if (!r.task || r.status === 'done') { toast(out.answer || 'Done'); }
      else {
        sentId = r.task.id;
        toast(r.decision.lane === 'api'
          ? `→ ${r.decision.model_label} · answering · #${r.task.id}`
          : `Queued · ${p ? p.name : 'General'} · ${r.decision.model_label} · #${r.task.position || '?'} in line`, { taskId: r.task.id });
      }
    }
    rememberProject(pal.projectId);
    if (pal.keep && ui().palette === pal) {    // Keep: stay on the form with a blank prompt, ready for the next task
      Object.assign(pal, { text: '', images: [], sending: false, error: '' });
      const ta = document.getElementById('palInput');
      if (ta) { ta.value = ''; ta.style.height = ''; }
      set({ focus: 'palInput' });
      return again();
    }
    set({ sheet: null, palette: null });
    if (sentId != null) armAutoOpen(sentId);
  } catch (e) {
    if (ui().palette) { ui().palette.sending = false; ui().palette.error = 'Could not send: ' + e.message; requestRender(); }
    return;
  }
  again();
}

async function submitAsk() {
  const a = ui().ask;
  const text = a.text.trim();
  if (!text || a.sending) return;
  const problem = imageProblem(a.images);
  if (problem) return toast(problem);
  Object.assign(a, { sending: true, result: null, menu: null }); requestRender();
  try {
    const gen = generalProject();
    const pid = a.projectId != null && !(gen && a.projectId === gen.id) ? a.projectId : null;
    const agent = ui().octo.agent;
    const r = await octoApi.send(text, { project_id: pid, model: a.model, priority: pid ? (store.data.projectSettings[pid] || {}).default_priority || 0 : 0,
      permission_mode: a.mode || undefined, account: a.account || undefined, agent: agent === 'auto' ? undefined : agent,
      images: a.images.length ? readyIds(a.images) : undefined });
    clearSent(a.images);
    const out = octoSent(r);    // queued -> the line under the box; a plan to review -> the plan card; an instant command -> its answer
    Object.assign(a, { sending: false, text: '', result: out.result, open: false });
    const ta = document.getElementById('askInput');
    if (ta) { ta.value = ''; ta.style.height = ''; }
    if (pid) rememberProject(pid);
    if (out.taskId != null) armAutoOpen(out.taskId);
  } catch (e) {
    Object.assign(a, { sending: false, result: { error: e.message } });
  }
  requestRender();
  again();
}

const ACTIVE_STATUSES = ['queued', 'running', 'rate_limited'];

async function sendReply(text) {
  const p = ui().pane;
  const body = (text ?? ui().reply).trim();
  if (!p || !body) return;
  const th = store.data.threads[p.taskId];
  const msgs = th ? th.tasks : store.data.tasks.filter(t => (t.thread_id || t.id) === p.taskId);
  const rootTask = msgs.find(t => t.id === p.taskId) || taskById(p.taskId);
  if (!rootTask) return;
  const last = lastReal(msgs);
  const latest = taskById(last.id) || last;
  // "@octopus ..." goes to Octopus, not to this chat's model - so none of the model checks apply.
  const toOcto = MENTION_RE.test(body);
  // A chat can change models between messages, so what it's on now is its latest message's, not the first's.
  const onModel = latest.model_id || rootTask.model_id;
  const model = !toOcto && ui().replyModel && ui().replyModel !== onModel ? ui().replyModel : null;
  const lane = model ? (store.data.models.find(m => m.id === model) || {}).lane : latest.lane;
  if (!toOcto && lane === 'api' && latest.status === 'running') return toast('Wait for this answer to finish');
  // The new model is handed the conversation so far, so the last answer has to exist first.
  if (model && ACTIVE_STATUSES.includes(latest.status)) return toast('Wait for the last message to finish before switching models');
  // Attached pictures go with what was typed, not with a one-tap quick reply.
  const pics = text == null ? imageDraft('reply') || [] : [];
  const problem = imageProblem(pics);
  if (problem) return toast(problem);
  await busy('reply', async () => {
    try {
      const r = await endpoints.addTask(rootTask.project_id, body, rootTask.priority || 0, rootTask.id, lane === 'agent' ? ui().replyMode : null,
        pics.length ? readyIds(pics) : null, model, !toOcto && lane === 'agent' ? replyAccount({ ui: ui(), d: store.data }, latest) || null : null);
      clearSent(pics);
      // Octopus took it: a plan to look at opens its card; queued work gets a toast that opens its chat.
      const out = r && r.octopus ? octoSent(r.octopus) : null;
      if (out && out.taskId != null) toast(`Octopus is on it · #${out.taskId}`, { taskId: out.taskId });
      // Replies queue like everything else (next in line). If Claude Code is busy
      // elsewhere, offer to start this one alongside it.
      const busyElsewhere = store.data.tasks.some(t => t.status === 'running' && t.lane === 'agent' && rootId(t) !== rootTask.id);
      if (!out && lane === 'agent' && busyElsewhere && r) {
        chatToast('Reply queued · next in line', r.id, { action: () => runNow(r.id), actionLabel: 'Proceed now' });
      }
      set({ replyMode: null, replyModel: null, replyAccount: null, replyMenu: false }); // the next reply starts from the mode and model this one ran under
      if (text == null) {
        set({ reply: '' });
        const ta = document.getElementById('replyInput');
        if (ta) { ta.value = ''; ta.style.height = ''; }
      }
      await loadThread(rootTask.id);
    } catch (e) { fail(e); }
  });
  again();
}

const TEXT_TARGETS = {
  ask: { get: () => ui().ask.text, set: v => { ui().ask.text = v; ui().ask.result = null; }, id: 'askInput' },
  reply: { get: () => ui().reply, set: v => { store.ui.reply = v; }, id: 'replyInput' },
  palette: { get: () => (ui().palette || {}).text || '', set: v => { if (ui().palette) ui().palette.text = v; }, id: 'palInput' },
};
function putText(target, v) {
  const t = TEXT_TARGETS[target];
  if (!t) {
    // Any other text box: `target` is its id, and the usual input handler files the new value.
    const el = document.getElementById(target);
    if (el) { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); if (el._grow) el._grow.fit(); }
    return;
  }
  t.set(v);
  const el = document.getElementById(t.id);
  if (el) { el.value = v; if (el._grow) el._grow.fit(); }
  requestRender();
}
const textOf = target => TEXT_TARGETS[target] ? TEXT_TARGETS[target].get() : ((document.getElementById(target) || {}).value || '');
// ---------------------------------------------------------------- queue items: edit / proceed now
// Everything queues by default; any queued item (new task or reply) can be edited, moved,
// or started right away alongside whatever is running.
function openEdit(id) {
  const t = taskById(id);
  if (!t) return;
  const q = queuedTasks();
  const i = q.findIndex(x => x.id === id);
  const after = i > 0 ? String(q[i - 1].id) : 'first';
  const isReply = !!t.thread_id;
  const hasReplies = store.data.tasks.some(x => x.thread_id === t.id);
  // Hold it while it's being edited, so it can't start with the old text. Released on save / close.
  const held = !t.paused;
  if (held) { setData({ tasks: store.data.tasks.map(x => (x.id === id ? { ...x, paused: 1 } : x)) }); more.pause(id).catch(fail); }
  set({ sheet: 'editTask', edit: {
    held, id, isReply, canMoveProject: !isReply && !hasReplies, title: t.title || '', prompt: t.prompt, priority: t.priority,
    projectId: t.project_id, model: t.model_id || 'auto', account: t.pinned_account || '', mode: t.permission_mode || 'acceptEdits', after, origAfter: after, saving: false, error: '',
  } });
}

// Edit and retry: a failed or stopped task gets its prompt changed and goes back in line in one step.
function openRetryEdit(id) {
  const t = taskById(id);
  if (!t || !['error', 'cancelled', 'needs_review', 'rate_limited'].includes(t.status)) return;
  set({ sheet: 'retryEdit', retryEdit: { id, prompt: t.prompt, saving: false, error: '' }, focus: 'retryPromptInput' });
}
async function submitRetryEdit() {
  const e = ui().retryEdit;
  if (!e || e.saving) return;
  if (!e.prompt.trim()) { set({ retryEdit: { ...e, error: "The prompt can't be empty" } }); return; }
  set({ retryEdit: { ...e, saving: true, error: '' } });
  try {
    await more.requeue(e.id, true, e.prompt.trim());
    set({ sheet: null, retryEdit: null });
    toast('Edited and queued at the front', { taskId: e.id });
    again();
  } catch (err) {
    if (ui().retryEdit) set({ retryEdit: { ...ui().retryEdit, saving: false, error: err.message || String(err) } });
  }
}

// Model / Mode quick sheet: each tap saves right away.
function openRunOpts(id) {
  const t = taskById(id);
  if (!t) return;
  set({ sheet: 'runOpts', runOpts: { id, isReply: !!t.thread_id, model: t.model_id || 'auto', account: t.pinned_account || '', mode: t.permission_mode || 'acceptEdits', saving: false, error: '' } });
}
async function setRunOpt(field, value) {
  const o = ui().runOpts, t = o && taskById(o.id);
  if (!o || !t || o.saving || o[field] === value) return;
  const prev = o[field];
  set({ runOpts: { ...o, [field]: value, saving: true, error: '' } });
  try {
    await more.editTask(o.id, field === 'model' ? { model: value } : field === 'account' ? { claude_account: value } : { permission_mode: value });
    if (ui().runOpts) set({ runOpts: { ...ui().runOpts, saving: false } });
    toast('Saved', { taskId: o.id });
    again();
  } catch (err) {
    if (ui().runOpts) set({ runOpts: { ...ui().runOpts, [field]: prev, saving: false, error: err.message || String(err) } });
  }
}

// Inline model / mode on a queued message in the chat.
async function quickRunOpt(id, field, value) {
  try {
    await more.editTask(id, field === 'model' ? { model: value } : field === 'account' ? { claude_account: value } : { permission_mode: value });
    toast('Saved', { taskId: id });
    again();
  } catch (err) { toast(err.message || String(err)); again(); }
}

// Saves what changed; true if it all went through.
async function saveEdit({ close = true } = {}) {
  const e = ui().edit, t = e && taskById(e.id);
  if (!e || !t || e.saving) return false;
  const body = {};
  if (e.prompt.trim() && e.prompt.trim() !== t.prompt) body.prompt = e.prompt.trim();
  if (!e.isReply && e.title.trim() && e.title.trim() !== (t.title || '')) body.title = e.title.trim();
  if (e.priority !== t.priority) body.priority = e.priority;
  if (e.canMoveProject && e.projectId !== t.project_id) body.project_id = e.projectId;
  if (!e.isReply && e.model !== (t.model_id || 'auto')) body.model = e.model;
  if (e.mode !== (t.permission_mode || 'acceptEdits')) body.permission_mode = e.mode;
  if ((e.account || '') !== (t.pinned_account || '')) body.claude_account = e.account || '';
  set({ edit: { ...e, saving: true, error: '' } });
  try {
    if (Object.keys(body).length) await more.editTask(e.id, body);
    if (e.after !== e.origAfter) await more.moveTask(e.id, e.after === 'first' ? null : Number(e.after));
    if (close) { set({ sheet: null, edit: null }); toast('Saved', { taskId: e.id }); }
    else set({ edit: { ...ui().edit, saving: false } });
    again();
    return true;
  } catch (err) {
    if (ui().edit) set({ edit: { ...ui().edit, saving: false, error: err.message || String(err) } });
    return false;
  }
}

// Pause / play a task: flip the flag on screen straight away, then let the
// server confirm (or put it back if it refuses).
async function holdTask(id, paused) {
  const mark = v => setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, paused: v ? 1 : 0 } : t)) });
  mark(paused);
  try {
    await (paused ? more.pause(id) : more.play(id));
    chatToast(paused ? 'Paused' : 'Playing', id);
  } catch (e) { mark(!paused); fail(e); }
  again();
}

// Pause all / Resume all / Dismiss all on a queue list: flip every task on
// screen straight away, ask the server for each, then reload the truth.
const idsOf = el => (el.dataset.ids || '').split(',').filter(Boolean).map(Number);
async function bulkTasks(ids, kind) {
  if (!ids.length) return;
  const call = { pause: more.pause, play: more.play, cancel: endpoints.cancel }[kind];
  const patch = { pause: { paused: 1 }, play: { paused: 0 }, cancel: { status: 'cancelled' } }[kind];
  setData({ tasks: store.data.tasks.map(t => (ids.includes(t.id) ? { ...t, ...patch } : t)) });
  const results = await Promise.allSettled(ids.map(id => call(id)));
  const ok = ids.filter((id, i) => results[i].status === 'fulfilled');
  const n = ok.length, word = n === 1 ? 'task' : 'tasks';
  const msg = { pause: `Paused ${n} ${word}`, play: `Resumed ${n} ${word}`, cancel: `Cancelled ${n} ${word}` }[kind];
  const failed = ids.length - n;
  const undo = kind === 'cancel' && n ? { action: () => Promise.allSettled(ok.map(id => more.requeue(id, false))).then(again), actionLabel: 'Undo' } : {};
  toast(failed ? `${msg} · ${failed} couldn't be changed` : msg, undo);
  again();
}

// Delete queue items: off the screen at once, into the Recycle bin on the server, with Undo on the
// notification (in the toast and in the bell panel) that restores them to where they were.
async function deleteTasks(ids) {
  if (!ids.length) return;
  if (ui().sheet === 'editTask' && ids.includes(ui().edit && ui().edit.id)) set({ sheet: null, edit: null });
  const gone = ids.map(id => taskById(id)).filter(Boolean);
  setData({ tasks: store.data.tasks.filter(t => !ids.includes(t.id) && !ids.includes(t.thread_id)) });
  const results = await Promise.allSettled(ids.map(id => more.deleteTask(id)));
  const ok = ids.filter((id, i) => results[i].status === 'fulfilled');
  const failed = results.find(r => r.status === 'rejected');
  if (!ok.length) { again(); return fail(failed.reason); }
  const one = ok.length === 1 && gone.find(t => t.id === ok[0]);
  const msg = `Deleted · ${one ? taskName(one) : plural(ok.length, 'task')} · in the Recycle bin`;
  const undo = () => Promise.allSettled(ok.map(id => more.restoreTask(id))).then(rs => {
    const back = rs.filter(r => r.status === 'fulfilled').length;
    toast(back ? `Restored · ${back === 1 && one ? taskName(one) : plural(back, 'task')}` : 'Already gone from the Recycle bin');
    again(); if (ui().view === 'bin') loadBin().catch(() => {});
  });
  toast(failed && ids.length > 1 ? `${msg} · ${ids.length - ok.length} couldn't be deleted` : msg, { action: undo, actionLabel: 'Undo', keep: true });
  again(); if (ui().view === 'bin') loadBin().catch(() => {});
}

// Move or delete a whole chat that isn't under way (finished, or still in line) - the ⋯ in its header.
function openChatActions(id) {
  set({ sheet: 'chatActions', chatAct: { id: rootOf(id), saving: null, error: '' } });
}
// Ticked rows in a project's Activity tab -> their chats' first messages, once each.
const actRoots = () => [...new Set(Object.keys(ui().actSel).filter(k => ui().actSel[k]).map(k => rootOf(Number(k))))];
async function transferChats(pid) {
  const a = ui().chatAct;
  if (!a || a.saving) return;
  const to = projectById(pid), before = a.ids.map(id => [id, (taskById(id) || {}).project_id]);
  set({ chatAct: { ...a, saving: pid, error: '' } });
  const rs = await Promise.allSettled(before.filter(([, f]) => f !== pid).map(([id]) => more.transferTask(id, pid)));
  const moved = before.filter(([, f]) => f !== pid).filter((_, i) => rs[i].status === 'fulfilled');
  const bad = rs.find(r => r.status === 'rejected');
  if (!moved.length) { set({ chatAct: { ...ui().chatAct, saving: null, error: bad ? (bad.reason.message || String(bad.reason)) : "They're already in that project" } }); return; }
  set({ sheet: null, chatAct: null, actSel: {} });
  const undo = () => Promise.allSettled(moved.map(([id, f]) => more.transferTask(id, f))).then(() => { toast(`Moved back · ${plural(moved.length, 'chat')}`); again(); });
  toast(`Moved to ${to ? to.name : 'the other project'} · ${plural(moved.length, 'chat')}${bad ? ` · ${rs.filter(r => r.status === 'rejected').length} couldn't move` : ''}`, { action: undo, actionLabel: 'Undo' });
  again();
}
async function transferChat(pid) {
  if (ui().chatAct && ui().chatAct.ids) return transferChats(pid);
  const a = ui().chatAct, t = a && taskById(a.id);
  if (!a || a.saving) return;
  const from = t && t.project_id, to = projectById(pid), back = projectById(from);
  set({ chatAct: { ...a, saving: pid, error: '' } });
  try {
    await more.transferTask(a.id, pid);
    set({ sheet: null, chatAct: null });
    const undo = from != null ? { action: () => more.transferTask(a.id, from).then(() => { toast(`Moved back to ${back ? back.name : 'its project'}`); again(); loadThread(a.id).catch(() => {}); }, fail), actionLabel: 'Undo' } : {};
    toast(`Moved to ${to ? to.name : 'the other project'} · ${t ? taskName(t) : 'chat'}`, { ...undo, taskId: a.id });
    again(); loadThread(a.id).catch(() => {});
  } catch (err) {
    if (ui().chatAct) set({ chatAct: { ...ui().chatAct, saving: null, error: err.message || String(err) } });
  }
}
function deleteChat(id) {
  set({ sheet: null, chatAct: null });
  closeTab(id);
  deleteTasks([id]);   // to the Recycle bin, with Undo
}

// The Edit schedule sheet's fields. `retime`: a change to when it runs, which turns an off schedule on.
const localDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
function schedPatch(patch, retime = false) {
  const e = ui().schedEdit;
  if (e) set({ schedEdit: { ...e, ...patch, ...(retime && !('enabled' in patch) ? { enabled: true } : {}), error: '' } });
}

// Bulk change on ticked tasks: the sheet confirms in plain words, then each task is changed on the server
// (one failing doesn't stop the rest), with Undo where the change can be reversed.
function openBulk(kind, src) {
  const ids = Object.keys(ui()[src]).filter(k => ui()[src][k]).map(Number);
  if (ids.length) set({ sheet: 'bulk', bulk: { kind, src, ids, pid: null, prio: null, busy: false, error: '' } });
}
async function runBulk() {
  const b = ui().bulk;
  if (!b || b.busy) return;
  const { ok } = bulkPlan(b.kind, b.ids);
  if (!ok.length) return;
  set({ bulk: { ...b, busy: true, error: '' } });
  const ids = ok.map(t => t.id), was = Object.fromEntries(ok.map(t => [t.id, t]));
  const call = { cancel: id => endpoints.cancel(id), rerun: id => more.requeue(id, false),
    move: id => more.transferTask(id, b.pid), priority: id => more.setPriority(id, b.prio) }[b.kind];
  const rs = await Promise.allSettled(ids.map(id => call(id)));
  const done = ids.filter((_, i) => rs[i].status === 'fulfilled'), failed = ids.length - done.length;
  const bad = rs.find(r => r.status === 'rejected');
  if (!done.length) { set({ bulk: { ...ui().bulk, busy: false, error: bad.reason.message || String(bad.reason) } }); return again(); }
  set({ sheet: null, bulk: null, [b.src]: {} });
  const word = b.kind === 'move' ? 'chat' : 'task';
  const msg = { cancel: 'Cancelled', rerun: 'Re-running', move: `Moved to ${(projectById(b.pid) || {}).name || 'the other project'} ·`, priority: `Priority set to ${(['Low', 'Normal', 'Urgent'])[b.prio + 1]} ·` }[b.kind];
  const back = { cancel: id => more.requeue(id, false), move: id => more.transferTask(id, was[id].project_id), priority: id => more.setPriority(id, was[id].priority) }[b.kind];
  const undo = back ? { action: () => Promise.allSettled(done.map(id => back(id))).then(again), actionLabel: 'Undo' } : {};
  toast(`${msg} ${plural(done.length, word)}${failed ? ` · ${failed} couldn't be changed` : ''}`, undo);
  again();
}

// Ticked rows in Up next, in queue order (ids that have since left the queue drop out).
const pickedIds = () => queuedTasks().filter(t => ui().qSel[t.id]).map(t => t.id);
function moveQueued(dir) {
  const sel = new Set(pickedIds());
  if (!sel.size) return;
  let ids = queuedTasks().map(t => t.id);
  if (dir === 'top' || dir === 'bottom') {
    const moving = ids.filter(id => sel.has(id)), rest = ids.filter(id => !sel.has(id));
    ids = dir === 'top' ? [...moving, ...rest] : [...rest, ...moving];
  } else {
    // One step: each ticked row swaps with the unticked neighbour it's moving past.
    const up = dir === 'up';
    for (let k = 0; k < ids.length; k++) {
      const i = up ? k : ids.length - 1 - k, j = up ? i - 1 : i + 1;
      if (sel.has(ids[i]) && j >= 0 && j < ids.length && !sel.has(ids[j])) [ids[i], ids[j]] = [ids[j], ids[i]];
    }
  }
  if (ids.join() !== queuedTasks().map(t => t.id).join()) reorderQueue(ids);
}

// A small OK / Cancel popup just above `anchor` (instead of the browser's confirm), with OK
// focused so Space or Enter says yes. Esc, Cancel or a click outside says no. It lives outside
// #app so re-renders don't wipe it. Falls back to confirm() when there's nothing to anchor to.
let closeNear = null;
function confirmNear(anchor, text, okLabel = 'OK') {
  if (!anchor || !anchor.isConnected) return Promise.resolve(confirm(text));
  if (closeNear) closeNear(false);
  const back = document.activeElement;
  return new Promise(resolve => {
    const pop = document.createElement('div');
    pop.className = 'confirm-pop';
    pop.setAttribute('role', 'alertdialog');
    pop.innerHTML = `<p class="confirm-pop__text"></p><div class="confirm-pop__acts"><button class="btn btn--ghost btn--sm" data-v="0">Cancel</button><button class="btn btn--primary btn--sm" data-v="1">${okLabel}</button></div>`;
    pop.firstChild.textContent = text;
    document.body.appendChild(pop);
    const place = () => {
      if (!anchor.isConnected) return;
      const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
      const above = r.top - h - 8 >= 8;
      pop.style.left = `${Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8))}px`;
      pop.style.top = `${above ? r.top - h - 8 : r.bottom + 8}px`;
      pop.classList.toggle('confirm-pop--below', !above);
    };
    place();
    const done = v => {
      closeNear = null;
      pop.remove();
      document.removeEventListener('pointerdown', outside, true);
      removeEventListener('scroll', place, true);
      removeEventListener('resize', place);
      if (back && back.isConnected) back.focus({ preventScroll: true });
      resolve(v);
    };
    const outside = e => { if (!pop.contains(e.target)) done(false); };
    closeNear = done;
    pop.addEventListener('click', e => { const b = e.target.closest('button'); if (b) done(b.dataset.v === '1'); });
    pop.addEventListener('keydown', e => {
      e.stopPropagation();   // keep page shortcuts (Esc, etc.) out of it
      if (e.key === 'Escape') { e.preventDefault(); done(false); }
      else if (e.key === 'Tab') { e.preventDefault(); const bs = pop.querySelectorAll('button'); (document.activeElement === bs[1] ? bs[0] : bs[1]).focus(); }
    });
    document.addEventListener('pointerdown', outside, true);
    addEventListener('scroll', place, true);
    addEventListener('resize', place);
    pop.querySelector('[data-v="1"]').focus({ preventScroll: true });
  });
}

async function runNow(id, anchor) {
  const t = taskById(id);
  if (!t) return;
  // From the edit sheet: keep whatever was just changed.
  if (ui().sheet === 'editTask' && ui().edit && ui().edit.id === id) {
    if (!(await saveEdit({ close: false }))) return;
    if (ui().edit.held) { await releaseEditHold(id); ui().edit.held = false; }
  }
  const p = projectById(t.project_id);
  const sameProject = t.lane === 'agent' && store.data.tasks.some(x => x.status === 'running' && x.lane === 'agent'
    && x.project_id === t.project_id && rootId(x) !== rootId(t));
  if (sameProject && !(await confirmNear(anchor, `Something else is already running in ${p ? p.name : 'this project'}. Running both at once means two Claude Code sessions changing the same files. Start anyway?`, 'Start anyway'))) return;
  try {
    const r = await more.runNow(id);
    if (ui().sheet === 'editTask') set({ sheet: null, edit: null });
    if (r.mode === 'now') chatToast('Proceeding now', id, { full: 'Starting now' });
    else chatToast(r.mode === 'after_chat' ? 'Starts once the earlier message finishes' : 'Moved to the front', id,
      { full: r.mode === 'after_chat' ? 'Starts as soon as the earlier message in this chat finishes' : 'Moved to the front' });
    again();
  } catch (e) { fail(e); }
}

// Summarise button: one catch-up on the whole chat, in a sheet. The backend caches it
// until the chat changes, so reopening is instant.
async function openSummary(taskId, refresh = false) {
  const t = taskById(taskId) || (store.data.threads[taskId] || { tasks: [] }).tasks[0];
  set({ sheet: 'summary', summary: { taskId, title: t ? (t.title || t.prompt) : '', loading: true } });
  try {
    const r = await more.summarise(taskId, refresh);
    if ((ui().summary || {}).taskId !== taskId) return;  // closed or switched meanwhile
    set({ summary: { taskId, title: r.title || ui().summary.title, text: r.summary, cached: r.cached, modelId: r.model_id, loading: false } });
  } catch (e) {
    if ((ui().summary || {}).taskId !== taskId) return;
    set({ summary: { ...ui().summary, loading: false, error: e.message || String(e) } });
  }
}

async function doPolish(target, kind) {
  if (!textOf(target).trim() || ui().polishing) return;
  set({ polishing: target });
  const r = await polish(textOf(target), text => endpoints.polish(text, kind));
  set({ polishing: null });
  if (r.changed) {
    putText(target, r.text);
    toast('Text polished', { action: () => putText(target, r.original), actionLabel: 'Undo' });
  } else toast('Already reads well');
}

// The New project sheet: a CRM record, with or without a folder (no folder = CRM only, opened on its Overview).
async function addProjectSubmit() {
  const ob = ui().onboard, category = ob.category || 'work';
  const body = newProjectBody(ob);
  if (typeof body === 'string') { ob.error = body; return requestRender(); }
  await busy('addProject', async () => {
    try {
      const p = await endpoints.createProject(body);
      set({ onboard: { ...NEW_PROJECT, category }, sheet: null });
      rememberProject(p.id);
      if (p.path) toast(`Added ${p.name} · ready when you are`);
      else {
        setData({ projects: [...store.data.projects, p] });
        navigate('project', p.id, 'overview');
        toast(`Created ${p.name} · CRM only`);
      }
    } catch (e) { ui().onboard.error = e.message; }
  });
  again();
}

async function patchProjectSettings(pid, patch) {
  const cur = store.data.projectSettings[pid];
  if (cur) setData({ projectSettings: { ...store.data.projectSettings, [pid]: { ...cur, ...patch } } });
  try {
    const v = await more.setProjectSettings(pid, patch);
    setData({ projectSettings: { ...store.data.projectSettings, [pid]: v } });
  } catch (e) { fail(e); }
}

// Upload each picked file to the project's Documents, one after another, adding it to the list as it lands.
async function docsPicked(pid, files) {
  let n = 0;
  for (const f of files) {
    try {
      const doc = await uploadDocument(pid, f);
      setData({ documents: { ...store.data.documents, [pid]: [doc, ...(store.data.documents[pid] || [])] } });
      n++;
    } catch (e) { toast(`${f.name}: ${e.message || e}`); }
  }
  if (n) toast(n === 1 ? 'Document added' : `${n} documents added`);
}

function patchNotes(pid, fn) {
  setData({ notes: { ...store.data.notes, [pid]: (store.data.notes[pid] || []).map(fn) } });
}

function patchTodos(pid, fn) {
  const todos = store.data.todos[pid] || [];
  setData({ todos: { ...store.data.todos, [pid]: todos.map(fn) } });
}

// How backlog items will run once queued (model / permission_mode / priority), for one item or many at once.
// Shows straight away and undoes an item whose save fails. Saves still going are kept so Queue can wait for them.
const TODO_FIELDS = { model: 'model_id', permission_mode: 'permission_mode', priority: 'priority', only_account: 'only_account', important: 'important' };
const todoSaves = new Set();
const trackSave = p => { todoSaves.add(p); p.finally(() => todoSaves.delete(p)); return p; };
function setTodos(pid, ids, k, v) {
  const field = TODO_FIELDS[k];
  const before = new Map((store.data.todos[pid] || []).filter(x => ids.includes(x.id)).map(x => [x.id, x[field]]));
  if (!field || !before.size) return;
  patchTodos(pid, x => (before.has(x.id) ? { ...x, [field]: v } : x));
  trackSave(Promise.all([...before.keys()].map(id => endpoints.updateTodo(id, { [k]: v })
    .catch(e => { patchTodos(pid, x => (x.id === id ? { ...x, [field]: before.get(id) } : x)); throw e; }))).catch(fail));
}
const checkedTodoIds = pid => (store.data.todos[pid] || []).filter(t => t.status === 'pending' && t.checked).map(t => t.id);
function tickTodos(pid, ids, on) {
  const change = (store.data.todos[pid] || []).filter(t => ids.includes(t.id) && !!t.checked !== on);
  if (!change.length) return;
  patchTodos(pid, x => (change.includes(x) ? { ...x, checked: on ? 1 : 0 } : x));
  trackSave(Promise.all(change.map(t => endpoints.updateTodo(t.id, { checked: on }))).catch(fail));
}
// Queue sheet: an item's reworded text, saved when you leave its box (or press Enter).
function saveReviewText(id) {
  const r = ui().runReview;
  if (!r || r.text[id] == null) return;
  const t = (store.data.todos[r.pid] || []).find(x => x.id === id), text = r.text[id].trim();
  const left = { ...r.text }; delete left[id];
  if (t && text && text !== t.text) {
    const old = t.text;
    patchTodos(r.pid, x => (x.id === id ? { ...x, text } : x));
    trackSave(endpoints.updateTodo(id, { text }).catch(e => { patchTodos(r.pid, x => (x.id === id ? { ...x, text: old } : x)); fail(e); }));
  }
  set({ runReview: { ...ui().runReview, text: left } });
}

const num = v => (v === '' || v == null ? null : Number(v));

// ---------------------------------------------------------------- header search (results: views/search.js)
const searchInput = () => document.getElementById('topSearch');
const searchIdle = { q: '', open: false, sel: 0, chats: [], chatsFor: '' };

// Spotlight (Alt+Space): search anything, or send the words to Octopus. It shares the query and chat lookup with the header search.
const spotOpen = () => set({ spot: { open: true, sel: 0, sending: false, error: '' }, search: searchIdle, focus: 'spotInput' });
function spotClose() {
  set({ spot: { ...ui().spot, open: false, sending: false, error: '' }, search: searchIdle });
  loadSearchSoon();
}
async function spotAsk(text) {
  set({ spot: { ...ui().spot, sending: true, error: '' } });
  try {
    const r = await octoApi.send(text, { model: 'auto', priority: 0 });
    const out = octoSent(r);
    spotClose();
    if (out.plan) return;
    if (!r.task || r.status === 'done') toast(out.answer || 'Done');
    else { toast(`Sent to Octopus · ${r.decision.model_label} · #${r.task.id}`, { taskId: r.task.id }); armAutoOpen(r.task.id); }
    again();
  } catch (e) { set({ spot: { ...ui().spot, sending: false, error: 'Could not send: ' + e.message }, focus: 'spotInput' }); }
}
function spotPick(it) {
  if (!it) return;
  if (it.kind === 'ask') return spotAsk(ui().search.q.trim());
  spotClose();
  pickSearch(it.kind, it.id);
}
function spotKey(e) {
  if (e.isComposing) return;
  const sp = ui().spot, n = spotItems(context()).length;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (n) { searchScroll = true; set({ spot: { ...sp, sel: (sp.sel + (e.key === 'ArrowDown' ? 1 : n - 1)) % n } }); }
  } else if (e.key === 'Enter' && !sp.sending) { e.preventDefault(); spotPick(spotItems(context())[sp.sel]); }
}
const focusSearch = () => set({ search: { ...ui().search, open: true }, focus: 'topSearch' });

// Closes the results and drops the keyboard. On a phone the header stops being the search box, so the words go too.
function closeSearch() {
  const s = ui().search, el = searchInput();
  if (el) el.blur();
  set({ search: { ...s, open: false, sel: 0, ...(isDesk() ? {} : { q: '' }) } });
}

function pickSearch(kind, id) {
  const el = searchInput();
  if (el) el.blur();
  set({ search: searchIdle });
  loadSearchSoon();   // nothing typed: cancels a request still on its way
  if (kind === 'view') navigate(id);
  else if (kind === 'personal') navigate('personal', id);
  else if (kind === 'project') navigate('project', Number(id));
  else if (kind === 'client') navigate('client', Number(id));
  else if (kind === 'contact') navigate('contact', Number(id));
  else if (kind === 'chat') openChat(Number(id));
  else if (kind === 'recipe' || kind === 'schedule') navigate('recipes');
  else if (id === 'palette') openPalette();
  else ACTIONS.openSheet({ dataset: { sheet: id } });
}

function searchKey(e) {
  if (e.isComposing) return;
  const s = ui().search;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const n = searchItems(context()).length;
    if (!n) return;
    searchScroll = true;
    set({ search: { ...s, open: true, sel: (s.sel + (e.key === 'ArrowDown' ? 1 : n - 1)) % n } });
  } else if (e.key === 'Enter') {
    const it = searchItems(context())[s.sel];
    if (it) { e.preventDefault(); pickSearch(it.kind, it.id); }
  }
}

const ACTIONS = {
  chatJump: () => {
    const n = ui().chatNew, pane = root.querySelector('.pane'), el = n && pane && pane.querySelector(`[data-key="${n.item}"]`);
    if (el) pane.scrollTo({ top: Math.max(0, offsetIn(pane, el) - 12), behavior: 'smooth' });
    set({ chatNew: null });
  },
  spotClose,
  spotPick: el => spotPick(spotItems(context()).find(it => it.kind === el.dataset.kind && String(it.id) === el.dataset.id)),
  searchOpen: focusSearch,
  searchClose: closeSearch,
  searchClear: () => {
    const el = searchInput();
    if (el) el.value = '';
    set({ search: { ...ui().search, q: '', sel: 0, chats: [], chatsFor: '' }, focus: 'topSearch' });
    loadSearchSoon();
  },
  searchPick: el => pickSearch(el.dataset.kind, el.dataset.id),
  go: el => {
    const v = el.dataset.view;
    if (el.dataset.filter) store.ui.activityFilter = el.dataset.filter;
    if (el.dataset.quick) store.ui.projectsList = { ...ui().projectsList, quick: el.dataset.quick, open: {} };
    if (el.closest('.sheet')) store.ui.sheet = null;           // a link inside a sheet (Usage → Token usage) leaves it
    store.ui.acctMenu = false;                                 // leaving the page closes the header's account menu
    navigate(v);
  },
  goBack: () => navigate(ui().view === 'client' || ui().view === 'contact' ? 'clients' : 'projects'),
  tourOpen: () => { markTourSeen(); set({ sheet: 'tour', tour: { step: 0 } }); },
  tourNext: () => set({ tour: { step: Math.min(((ui().tour || {}).step || 0) + 1, TOUR_LENGTH - 1) } }),
  tourBack: () => set({ tour: { step: Math.max(((ui().tour || {}).step || 0) - 1, 0) } }),
  tourDone: () => { markTourSeen(); set({ sheet: null }); },
  tourGo: el => {
    markTourSeen(); set({ sheet: null });
    const to = el.dataset.do;
    if (to === 'addProject') ACTIONS.openSheet({ dataset: { sheet: 'addProject' } });
    else if (to === 'newTask') openPalette();
    else navigate('activity');
  },
  helpOpen: () => { markTourSeen(); set({ sheet: 'help' }); },
  goPersonal: el => navigate('personal', el.dataset.tab),
  assistConnect: el => busy('conn-' + (el.dataset.account || el.dataset.id), async () => {
    try {
      const r = await more.assistConnect(el.dataset.id, el.dataset.account);
      // Google's consent page opens in a new tab; the redirect lands back on this dashboard.
      window.open(r.auth_url, '_blank', 'noopener');
      toast('Finish signing in on the new tab · nothing is saved until you approve there');
    } catch (e) { fail(e); }
  }),
  assistDisconnect: el => busy('conn-' + el.dataset.id, async () => {
    if (!confirm(`Disconnect ${el.dataset.name || 'this account'}? Octopus stops using it and its sign-in is revoked.`)) return;
    try { await more.assistDisconnect(el.dataset.id); toast('Disconnected · the sign-in was revoked and forgotten'); await loadPersonal(); } catch (e) { fail(e); }
  }),
  assistDefault: el => busy('conn-' + el.dataset.id, async () => {
    try { await more.assistDefault(el.dataset.id, el.dataset.service); toast('Default account changed'); await loadPersonal(); } catch (e) { fail(e); }
  }),
  setPolicy: el => {
    const cap = el.dataset.cap, policy = el.dataset.policy, st = store.data.personal.status;
    if (st) setData({ personal: { ...store.data.personal, status: { ...st, policies: st.policies.map(p => (p.capability === cap ? { ...p, policy } : p)) } } });
    more.setPolicy(cap, policy).then(() => loadPersonal(), e => { fail(e); loadPersonal().catch(() => {}); });
  },
  cancelReminder: el => more.cancelReminder(Number(el.dataset.id)).then(() => { toast('Reminder cancelled'); return loadPersonal(); }, fail),
  openProject: el => navigate('project', Number(el.dataset.id)),
  openPalette: el => openPalette({ ...(el.dataset.pid ? { projectId: Number(el.dataset.pid) } : {}), ...(el.dataset.when ? { when: el.dataset.when } : {}) }),
  openSheet: el => set({ sheet: el.dataset.sheet, onboard: { ...ui().onboard, error: '', ...(el.dataset.sheet === 'addProject' && ui().scope !== 'all' ? { category: ui().scope } : {}) }, focus: el.dataset.sheet === 'addProject' ? 'addName' : null }),
  closeSheet: () => set({ sheet: null, retryEdit: null, palette: null, summary: null, edit: null, todoEdit: null, noteEdit: null, quickNew: null, evEdit: null, phEdit: null, phGen: null, phReport: null, runReview: null, runOpts: null, bulk: null, schedEdit: null }),
  scrimClose: (el, e) => { if (e.target === el) set({ sheet: null, palette: null, summary: null, edit: null, todoEdit: null, noteEdit: null, quickNew: null, evEdit: null, phEdit: null, phGen: null, phReport: null, runReview: null, schedEdit: null }); },
  editTask: el => openEdit(Number(el.dataset.id)),
  editProject: el => { const e = ui().edit; if (e) set({ edit: { ...e, projectId: Number(el.dataset.id), error: '' } }); },
  runOpts: el => openRunOpts(Number(el.dataset.id)),
  queuedMode: el => quickRunOpt(Number(el.dataset.id), 'mode', el.dataset.v),
  runOptsSet: el => setRunOpt(el.dataset.field, el.dataset.id),
  editMode: el => { const e = ui().edit; if (e) set({ edit: { ...e, mode: el.dataset.id, error: '' } }); },
  editAccount: el => { const e = ui().edit; if (e) set({ edit: { ...e, account: el.dataset.id, error: '' } }); },
  editModel: el => { const e = ui().edit; if (e) set({ edit: { ...e, model: el.dataset.id, error: '' } }); },
  editPriority: el => { const e = ui().edit; if (e) set({ edit: { ...e, priority: Number(el.dataset.v) } }); },
  saveEdit: () => saveEdit(),
  runNow: el => runNow(Number(el.dataset.id), el),
  summarise: el => openSummary(Number(el.dataset.id), el.dataset.refresh === '1'),
  secFold: el => {
    const secFold = { ...ui().secFold, [el.dataset.k]: !ui().secFold[el.dataset.k] };
    ls('octopus-sec-fold', JSON.stringify(secFold));
    set({ secFold });
  },
  navExpand: el => {
    const k = el.dataset.k;
    const navExpand = { ...ui().navExpand, [k]: !ui().navExpand[k] };
    set({ navExpand });
    if (k === 'calendar' && navExpand.calendar && !store.data.timeline.loaded) loadTimeline().catch(() => {});
  },
  toggleSidebar: () => {
    if (isWideDesk()) { const open = !ui().sidebarPref; ls(SIDEBAR_PREF, open ? '1' : '0'); set({ sidebarPref: open }); }
    else set({ navDrawer: !ui().navDrawer, rdrawer: null });
  },
  settingsTab: el => set({ settingsTab: el.dataset.tab, shortcutRec: null, shortcutMsg: '' }),
  shortcutRecord: el => set({ shortcutRec: ui().shortcutRec === el.dataset.id ? null : el.dataset.id, shortcutMsg: '' }),
  shortcutReset: el => { resetBinding(el.dataset.id); set({ shortcutRec: null, shortcutMsg: '' }); },
  shortcutResetAll: () => { resetShortcuts(); set({ shortcutRec: null, shortcutMsg: '' }); toast('Shortcuts back to the defaults'); },
  lazyMore: el => loadMore(el),
  homeTab: el => { const tab = el.dataset.tab === 'done' ? 'done' : 'queue'; ls('octopus-home-tab', tab); set({ homeTab: tab }); },
  toggleDensity: () => {
    const compact = ui().density !== 'compact';
    ls('octopus-compact', compact ? '1' : '0');
    set({ density: compact ? 'compact' : 'comfortable' });
  },
  openChat: el => openChat(Number(el.dataset.id)),
  // Up next label: on a phone it unfolds the row's buttons (play / pause / edit / delete); elsewhere it opens the chat as before.
  qRowTap: el => { const id = Number(el.dataset.id); if (matchMedia('(max-width: 520px)').matches) set({ qOpen: { ...ui().qOpen, [id]: !ui().qOpen[id] } }); else openChat(id); },
  openApproval: el => openApproval(Number(el.dataset.id)),
  openApprovalForTask: el => { const a = pendingApprovals().find(x => x.task_id === Number(el.dataset.id)); a ? openApproval(a.id) : openChat(Number(el.dataset.id)); },
  closePane,
  closeAllPanes,
  // Right-edge rail: each tab opens its sidebar beside any already open (so Messages and Calendar can sit together);
  // clicking an open one (or its ✕) closes just that one.
  toggleSide: el => setSide(el.dataset.side, !ui().sides.includes(el.dataset.side)),
  closeSide: el => (isDesk() ? setSide(el.closest('[data-side-panel]')?.dataset.sidePanel, false) : set({ rdrawer: null })),
  sideTab: el => set({ sideTab: el.dataset.tab === 'done' ? 'done' : 'queue' }),

  sideProject: el => set({ sideProject: el.dataset.project === 'all' || ui().sideProject == el.dataset.project ? 'all' : Number(el.dataset.project) }),
  messageList: () => showPane({ type: 'empty' }),
  // Pin: dock this chat in the side pane (Messages sidebar), un-floating / un-minimising it.
  pinTab: el => {
    const id = Number(el.dataset.id), u = ui();
    const pinned = u.pinned.includes(id) ? u.pinned.filter(x => x !== id) : [...u.pinned, id];
    ls('octopus-pinned-tabs', JSON.stringify(pinned));
    set({ pinned });
  },
  closeTabMenu: () => set({ tabMenu: null, linkMenu: null }),
  linkNewTab: el => { set({ linkMenu: null, tabMenu: null }); window.open(el.dataset.url, '_blank', 'noopener'); },
  // Close others / Close all leave pinned tabs open; Close shuts just the one you right-clicked.
  tabMenuPick: el => {
    const id = Number(el.dataset.id), op = el.dataset.op, u = ui();
    set({ tabMenu: null });
    if (op === 'one') return closeTab(id);
    const keep = u.tabs.filter(x => u.pinned.includes(x) || (op === 'others' && x === id));
    const drop = u.tabs.filter(x => !keep.includes(x));
    const drafts = { ...u.drafts };
    drop.forEach(x => delete drafts[x]);
    const cur = paneRoot(u.pane);
    if (cur == null || keep.includes(cur)) return set({ tabs: keep, drafts });
    keep.length ? showPane({ type: 'chat', taskId: keep[0] }, { tabs: keep, drafts }) : closeAllPanes();
  },
  minimizePane: () => set({ paneMin: true }),
  restorePane: () => set({ paneMin: false }),
  togglePaneFloat: () => { const f = !ui().paneFloat; ls('octopus-pane-float', f ? '1' : '0'); set({ paneFloat: f }); },
  switchTab: el => openChat(Number(el.dataset.id)),
  closeTab: el => closeTab(Number(el.dataset.id)),
  approve: el => approve(Number(el.dataset.id)),
  approveAlways: el => approve(Number(el.dataset.id), true),
  deny: el => deny(Number(el.dataset.id)),
  editRetry: el => openRetryEdit(Number(el.dataset.id)),
  submitRetryEdit: () => submitRetryEdit(),
  retry: el => busy('rt' + el.dataset.id, async () => {
    try { await more.requeue(Number(el.dataset.id), true); toast('Retry queued at the front', { taskId: Number(el.dataset.id) }); } catch (e) { fail(e); }
  }).then(again),
  dismiss: el => {
    const id = Number(el.dataset.id);
    dismissTask(id);
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, dismissed: true } : t)) });
  },
  reviewed: el => more.reviewed(Number(el.dataset.id)).then(() => { toast('Marked as reviewed', { taskId: Number(el.dataset.id) }); again(); }, fail),
  runAgain: el => runAgain(Number(el.dataset.id)),
  toTop: el => {
    const id = Number(el.dataset.id);
    const min = Math.min(0, ...queuedTasks().map(t => t.queue_pos ?? 0));
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, queue_pos: min - 1 } : t)) });
    const prev = queuedTasks().map(t => t.id);
    more.toTop(id).then(() => { toast('Moved to the top', { taskId: id, action: () => reorderQueue(prev, { undo: false }), actionLabel: 'Undo' }); again(); }, fail);
  },
  cancelQueued: el => {
    const id = Number(el.dataset.id);
    if (ui().sheet === 'editTask') set({ sheet: null, edit: null });
    setData({ tasks: store.data.tasks.map(t => (t.id === id ? { ...t, status: 'cancelled' } : t)) });
    endpoints.cancel(id).then(() => {
      toast('Cancelled', { action: () => more.requeue(id, false).then(again, fail), actionLabel: 'Undo', taskId: id });
      again();
    }, fail);
  },
  qCheck: el => { const id = Number(el.dataset.id); set({ qSel: { ...ui().qSel, [id]: !ui().qSel[id] } }); },
  qSelectAll: el => set({ qSel: Object.fromEntries(idsOf(el).map(id => [id, true])) }),
  qClear: () => set({ qSel: {} }),
  qCancel: () => openBulk('cancel', 'qSel'),
  qMoveTo: () => openBulk('move', 'qSel'),
  qPriority: () => openBulk('priority', 'qSel'),
  actCancel: () => openBulk('cancel', 'actSel'),
  actMoveTo: () => openBulk('move', 'actSel'),
  actPriority: () => openBulk('priority', 'actSel'),
  actRerun: () => openBulk('rerun', 'actSel'),
  actClear: () => set({ actSel: {} }),
  bulkPick: el => { const b = ui().bulk; if (b && !b.busy) set({ bulk: { ...b, [el.dataset.k]: Number(el.dataset.v), error: '' } }); },
  bulkConfirm: () => runBulk(),
  qMove: el => moveQueued(el.dataset.dir),
  qPause: () => bulkTasks(queuedTasks().filter(t => ui().qSel[t.id] && !t.paused).map(t => t.id), 'pause'),
  qResume: () => bulkTasks(queuedTasks().filter(t => ui().qSel[t.id] && t.paused).map(t => t.id), 'play'),
  deleteQueued: el => deleteTasks([Number(el.dataset.id)]),
  chatActions: el => openChatActions(Number(el.dataset.id)),
  transferChat: el => transferChat(Number(el.dataset.id)),
  deleteChat: el => deleteChat(Number(el.dataset.id)),
  hideProject: () => {
    const p = projectById(ui().projectId);
    if (!p || p.is_general) return;
    endpoints.setHidden(p.id, true).then(() => {
      toast(`Hid ${p.name} · find it under Hidden`, { action: () => endpoints.setHidden(p.id, false).then(again, fail), actionLabel: 'Undo', keep: true });
      navigate('projects'); again();
    }, fail);
  },
  unhideProject: el => endpoints.setHidden(Number(el.dataset.id), false).then(() => { toast('Project unhidden'); again(); }, fail),
  hideChat: el => {
    const id = rootOf(Number(el.dataset.id));
    more.setHidden([id], true).then(() => {
      toast('Hidden · find it under Hidden', { action: () => more.setHidden([id], false).then(again, fail), actionLabel: 'Undo', keep: true });
      again();
    }, fail);
  },
  unhideTask: el => more.setHidden([Number(el.dataset.id)], false).then(() => { toast('Unhidden'); again(); loadHidden().catch(() => {}); }, fail),
  restoreBin: el => {
    const id = Number(el.dataset.id), t = (store.data.bin.items || []).find(x => x.id === id);
    busy('bin' + id, () => more.restoreTask(id).then(r => {
      const where = r.task && r.task.status === 'queued' ? ` · #${r.task.position || '?'} in line` : '';
      toast(`Restored · ${t ? taskName(t) : 'task'}${where}`, { taskId: id });
    }, fail)).then(() => { again(); return loadBin(); }).catch(() => {});
  },
  eraseBin: el => {
    const id = Number(el.dataset.id), t = (store.data.bin.items || []).find(x => x.id === id);
    if (!confirm(`Erase "${t ? taskName(t) : 'this task'}" for good? This can't be undone.`)) return;
    busy('bin' + id, () => more.eraseFromBin(id).then(() => toast('Erased for good'), fail)).then(() => loadBin()).catch(() => {});
  },
  emptyBin: () => {
    const n = (store.data.bin.items || []).length;
    if (!n || !confirm(`Erase ${n === 1 ? 'the item' : `all ${n} items`} in the Recycle bin for good? This can't be undone.`)) return;
    more.emptyBin().then(() => { toast('Recycle bin emptied'); return loadBin(); }, fail);
  },
  actCheck: el => { const id = Number(el.dataset.id); set({ actSel: { ...ui().actSel, [id]: !ui().actSel[id] } }); },
  actSelectAll: el => set({ actSel: Object.fromEntries(idsOf(el).map(id => [id, true])) }),
  actMove: () => { const ids = actRoots(); if (ids.length) set({ sheet: 'chatActions', chatAct: { ids, saving: null, error: '' } }); },
  actDelete: () => {
    const ids = actRoots();
    if (!ids.length || !confirm(`Delete ${ids.length === 1 ? 'this chat' : `these ${ids.length} chats`}? ${ids.length === 1 ? 'It goes' : 'They go'} to the Recycle bin.`)) return;
    set({ actSel: {} }); deleteTasks(ids);
  },
  qDelete: () => {
    const ids = pickedIds();
    if (ids.length) { set({ qSel: {} }); deleteTasks(ids); }   // to the Recycle bin, with Undo
  },
  pauseAll: el => bulkTasks(idsOf(el), 'pause'),
  resumeAll: el => bulkTasks(idsOf(el), 'play'),
  dismissAll: el => {
    const ids = idsOf(el);
    if (ids.length && confirm(`Cancel ${ids.length === 1 ? 'the queued task' : `all ${ids.length} queued tasks`}?`)) bulkTasks(ids, 'cancel');
  },
  toggleSleep: () => {
    const sm = store.data.settings && store.data.settings.sleep_mode;
    const asleep = !!(sm && sm.until && new Date(sm.until).getTime() > Date.now());
    (asleep ? more.endSleep() : more.startSleep()).then(v => {
      setData({ settings: v });
      toast(asleep ? 'Welcome back' : `Sleep mode on until ${clock(v.sleep_mode.until)}. I'll keep the queue going.`);
    }, fail);
  },
  togglePingBack: () => {
    const on = !(store.data.settings && store.data.settings.notify_on_resume);
    setData({ settings: { ...store.data.settings, notify_on_resume: on } });
    more.setSettings({ notify_on_resume: on }).then(v => setData({ settings: v }), fail);
  },
  runRecipe: el => runRecipe(Number(el.dataset.id), num(el.dataset.pid)),
  scheduleRecipe: el => {
    const r = (store.data.recipes || []).find(x => x.id === Number(el.dataset.id));
    if (!r) return;
    const pid = num(el.dataset.pid) ?? (/^\d+$/.test(r.target) ? Number(r.target) : defaultProjectId());
    openPalette({ text: r.prompt, when: 'daily', projectId: pid });
  },
  toggleSchedule: el => {
    const id = Number(el.dataset.id);
    const s = (store.data.schedules || []).find(x => x.id === id);
    if (!s) return;
    setData({ schedules: store.data.schedules.map(x => (x.id === id ? { ...x, enabled: s.enabled ? 0 : 1 } : x)) });
    more.setSchedule(id, !s.enabled).then(again, fail);
  },
  // Edit schedule sheet (views/sheets.js). Changing when it runs turns an off schedule back on
  // (shown in the sheet, so it can be switched off again before saving).
  editSchedule: el => {
    const s = (store.data.schedules || []).find(x => x.id === Number(el.dataset.id));
    if (!s) return;
    set({ sheet: 'editSchedule', focus: 'schedPrompt', schedEdit: {
      id: s.id, prompt: s.prompt, label: s.label === s.prompt.slice(0, 80) ? '' : s.label, pid: s.project_id ?? null,
      cadence: s.cadence, time: s.time_of_day, weekday: s.weekday ?? null, date: localDay(s.next_run_at ? new Date(s.next_run_at) : new Date()),
      model: s.model || 'auto', priority: s.priority || 0, enabled: !!s.enabled, saving: false, error: '' } });
  },
  schedProject: el => schedPatch({ pid: num(el.dataset.id) ?? null }),
  schedCadence: el => schedPatch({ cadence: el.dataset.k, ...(el.dataset.k === 'weekly' && ui().schedEdit.weekday == null ? { weekday: 0 } : {}) }, true),
  schedWeekday: el => schedPatch({ weekday: Number(el.dataset.d) }, true),
  schedModel: el => schedPatch({ model: el.dataset.id }),
  schedPriority: el => schedPatch({ priority: Number(el.dataset.v) }),
  schedEnabled: () => schedPatch({ enabled: !ui().schedEdit.enabled }),
  saveSchedule: async () => {
    const e = ui().schedEdit;
    if (!e || e.saving) return;
    set({ schedEdit: { ...e, saving: true, error: '' } });
    try {
      const patch = { prompt: e.prompt.trim(), label: e.label.trim(), project_id: e.pid, cadence: e.cadence, time: e.time,
        weekday: e.cadence === 'weekly' ? e.weekday : null, model: e.model, priority: e.priority, enabled: e.enabled,
        ...(e.cadence === 'once' ? { date: e.date } : {}) };
      const sc = await more.editSchedule(e.id, patch);
      setData({ schedules: store.data.schedules.map(x => (x.id === sc.id ? sc : x)) });
      set({ sheet: null, schedEdit: null });
      toast(sc.enabled && sc.next_run_at ? `Saved · next run ${dayWord(sc.next_run_at)} at ${clock(sc.next_run_at)}` : 'Saved · off');
      again();
    } catch (err) {
      if (ui().schedEdit) set({ schedEdit: { ...ui().schedEdit, saving: false, error: err.message || String(err) } });
    }
  },
  deleteSchedule: async el => {
    const e = ui().schedEdit;
    const s = e && (store.data.schedules || []).find(x => x.id === e.id);
    if (!s || !(await confirmNear(el, `Delete the schedule "${s.label}"?`, 'Delete'))) return;
    set({ sheet: null, schedEdit: null });
    setData({ schedules: store.data.schedules.filter(x => x.id !== s.id) });
    // Undo puts it back as it was (as a new schedule - same prompt, project, time and on/off).
    const undo = async () => {
      const back = await more.addSchedule({ prompt: s.prompt, label: s.label, project_id: s.project_id, cadence: s.cadence, time: s.time_of_day,
        weekday: s.weekday, model: s.model, priority: s.priority, ...(s.cadence === 'once' && s.next_run_at ? { date: localDay(new Date(s.next_run_at)) } : {}) });
      if (!s.enabled) await more.setSchedule(back.id, false);
      again();
    };
    more.deleteSchedule(s.id).then(() => { toast('Schedule deleted', { action: () => undo().catch(fail), actionLabel: 'Undo' }); again(); }, err => { fail(err); again(); });
  },
  askMenu: el => {
    const a = ui().ask, wrap = el.closest('.ask__wrap');
    a.menu = a.menu === el.dataset.menu ? null : el.dataset.menu;
    // Where the tapped button sits in the box, so its menu can open right above it rather than at the far left.
    a.menuX = wrap ? Math.round(el.getBoundingClientRect().left - wrap.getBoundingClientRect().left) : 0;
    requestRender();
  },
  askPick: el => { Object.assign(ui().ask, { model: el.dataset.id, menu: null, result: null, touched: true }); requestRender(); },
  askTarget: el => { Object.assign(ui().ask, { projectId: num(el.dataset.id), menu: null, result: null, touched: true }); requestRender(); },
  askMode: el => { Object.assign(ui().ask, { mode: el.dataset.id === 'acceptEdits' ? null : el.dataset.id, menu: null, result: null }); requestRender(); },
  replyMode: el => {
    const mode = el.dataset.id;
    const same = el.getAttribute('aria-pressed') === 'true';   // tapped the one already on
    set({ replyMode: mode, replyMenu: false });
    // A task running right now keeps its launch mode unless told - switch it live too.
    const p = ui().pane;
    const th = p && store.data.threads[p.taskId];
    const last = th && th.tasks[th.tasks.length - 1];
    const live = last && (taskById(last.id) || last);
    if (live && live.status === 'running' && live.lane === 'agent') {
      more.switchMode(live.id, mode).then(() => { chatToast(`Mode changed · ${modeInfo(mode).short}`, live.id, { full: `Mode changed · ${modeInfo(mode).label} · continuing the same task` }); again(); }, fail);
    } else if (p && p.taskId != null && !same) chatToast(`Mode changed · ${modeInfo(mode).short}`, p.taskId, { full: `Mode changed · ${modeInfo(mode).label} from the next reply` });
  },
  blModelMenu: () => set({ blMenu: ui().blMenu === 'model' ? false : 'model' }),
  blModel: el => { if (el.getAttribute('aria-disabled') !== 'true') set({ blModel: el.dataset.id, blMenu: false }); },
  blMode: el => set({ blMode: el.dataset.id, blMenu: false }),
  blAccount: el => set({ blAccount: el.dataset.id, blMenu: false }),
  submitBlog: () => SUBMITS.addBlog(),
  replyModelMenu: () => set({ replyMenu: ui().replyMenu === 'model' ? false : 'model' }),
  replyModel: el => { if (el.getAttribute('aria-disabled') !== 'true') set({ replyModel: el.dataset.id, replyMenu: false }); },
  replyAccount: el => set({ replyAccount: el.dataset.id, replyMenu: false }),
  acctMenu: () => set({ acctMenu: !ui().acctMenu }),   // the header's Claude account switcher
  closeMenus: () => { ui().ask.menu = null; store.ui.replyMenu = false; store.ui.blMenu = false; store.ui.acctMenu = false; store.ui.projectsList = { ...ui().projectsList, menu: null }; store.ui.messages = { ...ui().messages, menu: false }; requestRender(); },
  // Message centre: every change to the search or filters asks the server again.
  msgStatus: el => { set({ messages: { ...ui().messages, status: el.dataset.status, limit: 40 } }); loadMessages().catch(fail); },
  msgProject: el => { const id = el.dataset.id; set({ messages: { ...ui().messages, project: id === 'all' ? 'all' : Number(id), limit: 40, menu: false } }); loadMessages().catch(fail); },
  msgKind: el => { set({ messages: { ...ui().messages, kind: el.dataset.kind, limit: 40, menu: false } }); loadMessages().catch(fail); },
  msgLane: el => { set({ messages: { ...ui().messages, lane: el.dataset.lane, limit: 40, menu: false } }); loadMessages().catch(fail); },
  msgMenu: el => { const where = el.dataset.where || 'page'; set({ messages: { ...ui().messages, menu: ui().messages.menu === where ? false : where } }); },
  msgFull: () => { set({ messages: { ...ui().messages, full: !ui().messages.full } }); },
  msgMore: () => { set({ messages: { ...ui().messages, limit: ui().messages.limit + 40 } }); loadMessages().catch(fail); },
  msgSearchClear: el => { set({ messages: { ...ui().messages, q: '', limit: 40 }, focus: el.dataset.focus || 'msgSearch' }); loadMessages().catch(fail); },
  msgClear: () => { set({ messages: { ...ui().messages, q: '', status: 'all', project: 'all', lane: 'all', kind: 'all', limit: 40, menu: false } }); loadMessages().catch(fail); },
  submitAsk: () => submitAsk(),
  // Phone Ask bar -> the full box at the top. Rendered right away (not on the next frame) so the focus lands
  // inside the tap: iOS only brings the keyboard up for a focus that comes from the tap itself.
  askOpen: () => {
    ui().ask.open = true; render();
    const ta = document.getElementById('askInput');
    if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  },
  askClose: () => { const a = ui().ask; a.open = false; a.menu = null; if (document.activeElement) document.activeElement.blur(); requestRender(); },
  rdrawerOpen: el => set({ rdrawer: el.dataset.side || ui().rdrawer || 'queue', navDrawer: false }),
  rdrawerClose: () => set({ rdrawer: null }),
  polish: el => doPolish(el.dataset.target, el.dataset.kind),
  palDictate: () => {
    if (!ui().palette) return;
    if (ui().palListening) return dictate(() => {});   // second tap: stop, the first call's callback delivers the text
    set({ palListening: true });
    dictate(text => {
      set({ palListening: false });
      if (text && ui().palette) { const cur = ui().palette.text.trimEnd(); putText('palette', cur ? cur + ' ' + text : text); }
      else if (!text) toast(store.ui.octo.voice.error || 'Didn\'t catch that');
    });
  },
  plQuick: el => set({ projectsList: { ...ui().projectsList, quick: el.dataset.q, open: {} } }),
  plMenu: el => { const pl = ui().projectsList; set({ projectsList: { ...pl, menu: pl.menu === el.dataset.menu ? null : el.dataset.menu } }); },
  plSort: el => set({ projectsList: { ...ui().projectsList, sort: el.dataset.sort, menu: null } }),
  plFilter: el => { const pl = ui().projectsList, k = el.dataset.f; set({ projectsList: { ...pl, [k]: !pl[k] } }); },
  plSearch: () => { const pl = ui().projectsList; set({ projectsList: { ...pl, searchOpen: !pl.searchOpen, q: pl.searchOpen ? '' : pl.q, menu: null }, focus: pl.searchOpen ? null : 'plSearch' }); },
  plSearchClose: () => set({ projectsList: { ...ui().projectsList, q: '', searchOpen: false } }),
  plToggle: el => { const pl = ui().projectsList; set({ projectsList: { ...pl, open: { ...pl.open, [el.dataset.id]: el.dataset.open !== 'true' } } }); },
  setScope: el => {
    const scope = el.dataset.scope;
    ls('octopus-scope', scope);
    const p = ui().view === 'project' ? projectById(ui().projectId) : null;
    set({ scope, projectsList: { ...ui().projectsList, open: {} } });
    if (p && scope !== 'all' && !p.is_general && p.category !== scope) navigate('projects');
    again();
  },
  setNewCategory: el => set({ onboard: { ...ui().onboard, category: el.dataset.scope } }),
  setCategory: el => {
    const p = projectById(ui().projectId);
    if (!p || p.is_general) return;
    const category = el.dataset.scope;
    if (category === p.category) return;
    endpoints.setCategory(p.id, category).then(() => {
      toast(`Moved ${p.name} to ${category === 'work' ? 'Work' : 'Personal'}`);
      if (ui().scope !== 'all' && ui().scope !== category) navigate('projects');
      again();
    }, fail);
  },
  setProjectStatus: el => {
    const p = projectById(ui().projectId);
    if (!p || p.is_general || el.dataset.status === p.status) return;
    const status = el.dataset.status;
    setData({ projects: store.data.projects.map(x => (x.id === p.id ? { ...x, status } : x)) });
    endpoints.setProjectStatus(p.id, status).then(() => { toast(`${p.name} · ${projectStatus({ status })[1]}`); again(); }, e => { fail(e); again(); });
  },
  plClear: () => set({ projectsList: { ...ui().projectsList, quick: 'all', q: '', searchOpen: false, showPaused: true, onlyActive: false, open: {} } }),
  projectTab: el => { navigate('project', ui().projectId, el.dataset.tab); const m = document.getElementById('main'); const tabs = root.querySelector('.tabs'); if (m && tabs && m.scrollTop > tabs.offsetTop) m.scrollTop = Math.max(0, tabs.offsetTop - 80); },
  togglePause: () => {
    const p = projectById(ui().projectId);
    if (!p) return;
    setData({ projects: store.data.projects.map(x => (x.id === p.id ? { ...x, active: p.active ? 0 : 1 } : x)) });
    endpoints.setActive(p.id, !p.active).then(() => { toast(p.active ? `Paused ${p.name} · its tasks will be skipped` : `Resumed ${p.name}`); again(); }, fail);
  },
  suggestImprovements: async () => {
    const p = projectById(ui().projectId);
    if (!p || p.is_general) return;
    await busy('suggest', async () => {
      try {
        const t = await endpoints.suggest(p.id);
        toast(`Reviewing ${p.name} · #${t.position || '?'} in line`, { taskId: t.id });
      } catch (e) { fail(e); }
    });
    again();
  },
  todoCheck: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const t = (store.data.todos[pid] || []).find(x => x.id === id);
    if (!t) return;
    patchTodos(pid, x => (x.id === id ? { ...x, checked: t.checked ? 0 : 1 } : x));
    endpoints.updateTodo(id, { checked: !t.checked }).catch(fail);
  },
  todoToggle: el => { const id = Number(el.dataset.id); set({ todoOpen: { ...ui().todoOpen, [id]: !ui().todoOpen[id] } }); },
  // Star / unstar a whole chat (its first message holds the mark) so it stands out in its project.
  chatStar: el => {
    const root = rootOf(Number(el.dataset.id)), t = taskById(root), on = !(t && t.important);
    const mark = v => setData({ tasks: store.data.tasks.map(x => (x.id === root ? { ...x, important: v ? 1 : 0 } : x)) });
    mark(on);
    more.chatImportant(root, on).then(() => toast(on ? 'Marked as important' : 'No longer important'), e => { mark(!on); fail(e); });
  },
  // Star / unstar a backlog item so it stands out. Doesn't move it or change how it runs.
  todoStar: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const t = (store.data.todos[pid] || []).find(x => x.id === id);
    if (t) setTodos(pid, [id], 'important', !t.important);
  },
  // How a backlog item will run once queued: model / permission_mode / priority.
  todoAccount: el => setTodos(ui().projectId, [Number(el.closest('[data-todo]').dataset.todo)], 'only_account', el.dataset.id),
  todoSet: el => setTodos(ui().projectId, [Number(el.dataset.id)], el.dataset.k, el.dataset.k === 'priority' ? Number(el.dataset.v) : el.dataset.v),
  submitTodo: () => SUBMITS.addTodo(),
  removeImage: el => { const list = imageDraft(el.dataset.target); if (list) { removeImage(list, Number(el.dataset.img)); requestRender(); } },
  submitNote: () => SUBMITS.addNote(),
  setNoteKind: el => set({ noteKind: el.dataset.kind }),
  setNoteEditKind: el => { const e = ui().noteEdit; if (e) set({ noteEdit: { ...e, kind: el.dataset.kind } }); },
  openNoteEdit: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const n = (store.data.notes[pid] || []).find(x => x.id === id);
    if (n) set({ sheet: 'editNote', noteEdit: { id, pid, text: n.text, kind: n.kind, images: draftOf(n.images), saving: false, error: '' }, focus: 'noteEditInput' });
  },
  saveNoteEdit: async () => {
    const e = ui().noteEdit;
    if (!e || e.saving || (!e.text.trim() && !(e.images || []).length)) return;
    const problem = imageProblem(e.images || []);
    if (problem) { set({ noteEdit: { ...e, error: problem } }); return; }
    set({ noteEdit: { ...e, saving: true, error: '' } });
    try {
      const v = await endpoints.updateNote(e.id, { text: e.text.trim(), kind: e.kind, images: readyIds(e.images || []) });
      clearSent(e.images || []);
      patchNotes(e.pid, x => (x.id === e.id ? { ...x, ...v } : x));
      set({ sheet: null, noteEdit: null });
      toast('Saved');
    } catch (err) {
      if (ui().noteEdit) set({ noteEdit: { ...ui().noteEdit, saving: false, error: err.message || String(err) } });
    }
  },
  docPick: () => { const f = root.querySelector('input[data-doc-file]'); if (f) f.click(); },
  docSort: el => set({ docs: { ...ui().docs, sort: el.value } }),
  docView: el => { ls('doc-view', el.dataset.v); set({ docs: { ...ui().docs, view: el.dataset.v } }); },
  docType: el => set({ docs: { ...ui().docs, type: el.dataset.v } }),
  docSearchClear: () => set({ docs: { ...ui().docs, q: '' }, focus: 'docSearch' }),
  docRefresh: async () => {
    const pid = ui().projectId;
    try { const v = await endpoints.documents(pid); setData({ documents: { ...store.data.documents, [pid]: v } }); toast('Documents refreshed'); } catch (e) { fail(e); }
  },
  docRename: el => {
    const doc = (store.data.documents[ui().projectId] || []).find(x => x.id === Number(el.dataset.id));
    if (doc) set({ docs: { ...ui().docs, editId: doc.id, editName: doc.name }, focus: 'docRenameInput' });
  },
  docRenameCancel: () => set({ docs: { ...ui().docs, editId: null, editName: '' } }),
  docRenameSave: async () => {
    const pid = ui().projectId, d = ui().docs, name = d.editName.trim();
    if (d.editId == null) return;
    if (!name) return toast('Give the document a name');
    try {
      const v = await endpoints.renameDocument(d.editId, name);
      setData({ documents: { ...store.data.documents, [pid]: (store.data.documents[pid] || []).map(x => (x.id === v.id ? { ...x, ...v } : x)) } });
      set({ docs: { ...ui().docs, editId: null, editName: '' } });
      toast('Renamed');
    } catch (e) { fail(e); }
  },
  docDownload: el => {
    const doc = (store.data.documents[ui().projectId] || []).find(x => x.id === Number(el.dataset.id));
    if (doc) downloadDocument(doc).catch(fail);
  },
  docDelete: async el => {
    const pid = ui().projectId, doc = (store.data.documents[pid] || []).find(x => x.id === Number(el.dataset.id));
    if (!doc || !(await confirmNear(el, `Delete "${doc.name}"? This can't be undone.`, 'Delete'))) return;
    endpoints.deleteDocument(doc.id).then(() => {
      setData({ documents: { ...store.data.documents, [pid]: (store.data.documents[pid] || []).filter(x => x.id !== doc.id) } });
      if (ui().sheet === 'preview' && ui().preview && ui().preview.docId === doc.id) set({ sheet: null, preview: null });
      toast('Document deleted');
    }, fail);
  },
  deleteNoteEdit: () => {
    const e = ui().noteEdit;
    if (!e) return;
    const note = (store.data.notes[e.pid] || []).find(x => x.id === e.id);
    setData({ notes: { ...store.data.notes, [e.pid]: (store.data.notes[e.pid] || []).filter(x => x.id !== e.id) } });
    set({ sheet: null, noteEdit: null });
    endpoints.deleteNote(e.id).then(() => toast('Deleted', note ? { action: () => undoDeleteNote(e.pid, note).catch(fail), actionLabel: 'Undo' } : {}), fail);
  },
  // Quick buttons (Home): tap fills the Ask box (and opens the picture picker if the button needs one).
  quickUse: el => {
    const b = quickButtons({ d: store.data }).find(x => x.id === el.dataset.id);
    if (!b) return;
    putText('ask', b.prompt);
    set({ focus: 'askInput' });
    if (b.pic && FEATURES.images) { const f = document.querySelector('input[data-attach="ask"]'); if (f && !f.disabled) f.click(); }
  },
  quickEditToggle: () => set({ quickEdit: !ui().quickEdit }),
  quickNew: () => set({ sheet: 'quick', quickNew: { icon: '', label: '', prompt: '', pic: false, saving: false, error: '' }, focus: 'quickLabel' }),
  quickNewPic: () => { const q = ui().quickNew; if (q) set({ quickNew: { ...q, pic: !q.pic } }); },
  quickSave: async () => {
    const q = ui().quickNew;
    if (!q || q.saving || !q.label.trim() || !q.prompt.trim()) return;
    set({ quickNew: { ...q, saving: true, error: '' } });
    const btn = { id: 'q' + Date.now().toString(36), icon: q.icon.trim() || '✨', label: q.label.trim(), prompt: q.prompt.trim(), pic: !!q.pic };
    try {
      const v = await more.setSettings({ quick_buttons: [...quickButtons({ d: store.data }), btn] });
      setData({ settings: v });
      set({ sheet: null, quickNew: null });
      toast('Added ' + btn.label);
    } catch (err) {
      if (ui().quickNew) set({ quickNew: { ...ui().quickNew, saving: false, error: err.message || String(err) } });
    }
  },
  quickRemove: el => {
    const list = quickButtons({ d: store.data });
    const gone = list.find(x => x.id === el.dataset.id);
    if (!gone) return;
    const left = list.filter(x => x !== gone);
    setData({ settings: { ...store.data.settings, quick_buttons: left } });
    if (!left.length) set({ quickEdit: false });
    more.setSettings({ quick_buttons: left }).then(v => { setData({ settings: v }); toast('Removed ' + gone.label); },
      e => { fail(e); more.settings().then(v => setData({ settings: v }), () => {}); });
  },
  openTodoEdit: el => {
    const pid = ui().projectId, id = Number(el.dataset.id);
    const t = (store.data.todos[pid] || []).find(x => x.id === id);
    if (t) set({ sheet: 'editTodo', todoEdit: { id, pid, text: t.text, images: draftOf(t.images), saving: false, error: '' }, focus: 'todoEditInput' });
  },
  saveTodoEdit: async () => {
    const e = ui().todoEdit;
    if (!e || e.saving || !e.text.trim()) return;
    const problem = imageProblem(e.images || []);
    if (problem) { set({ todoEdit: { ...e, error: problem } }); return; }
    set({ todoEdit: { ...e, saving: true, error: '' } });
    try {
      const v = await endpoints.updateTodo(e.id, { text: e.text.trim(), images: readyIds(e.images || []) });
      clearSent(e.images || []);
      patchTodos(e.pid, x => (x.id === e.id ? { ...x, ...v } : x));
      set({ sheet: null, todoEdit: null });
      toast('Saved');
    } catch (err) {
      if (ui().todoEdit) set({ todoEdit: { ...ui().todoEdit, saving: false, error: err.message || String(err) } });
    }
  },
  deleteTodoEdit: () => {
    const e = ui().todoEdit;
    if (!e) return;
    const all = store.data.todos[e.pid] || [], item = all.find(x => x.id === e.id), order = all.map(x => x.id);
    setData({ todos: { ...store.data.todos, [e.pid]: all.filter(x => x.id !== e.id) } });
    set({ sheet: null, todoEdit: null });
    endpoints.deleteTodo(e.id).then(() => toast('Deleted', item ? { action: () => undoDeleteTodos(e.pid, [item], order).catch(fail), actionLabel: 'Undo' } : {}), fail);
  },
  backlogSelectAll: el => {
    const pid = ui().projectId, on = el.dataset.all !== '1';
    const todos = (store.data.todos[pid] || []).filter(t => t.status === 'pending' && !!t.checked !== on);
    patchTodos(pid, x => (x.status === 'pending' ? { ...x, checked: on ? 1 : 0 } : x));
    Promise.all(todos.map(t => endpoints.updateTodo(t.id, { checked: on }))).catch(fail);
  },
  backlogDelete: () => {
    const pid = ui().projectId;
    const sel = (store.data.todos[pid] || []).filter(t => t.status === 'pending' && t.checked);
    const order = (store.data.todos[pid] || []).map(t => t.id);
    setData({ todos: { ...store.data.todos, [pid]: (store.data.todos[pid] || []).filter(t => !sel.includes(t)) } });
    Promise.all(sel.map(t => endpoints.deleteTodo(t.id))).then(() => toast('Deleted · ' + plural(sel.length, 'item'), sel.length ? { action: () => undoDeleteTodos(pid, sel, order).catch(fail), actionLabel: 'Undo' } : {}), fail);
  },
  // Run selected / Queue…: first the Queue sheet, to check every item before it joins the queue.
  // Opened with nothing ticked, it ticks everything - you untick what should wait.
  backlogRun: () => {
    const pid = ui().projectId;
    if (!checkedTodoIds(pid).length) tickTodos(pid, (store.data.todos[pid] || []).filter(t => t.status === 'pending').map(t => t.id), true);
    set({ sheet: 'runReview', runReview: { pid, bulk: false, text: {} } });
  },
  rqCheck: el => {
    const r = ui().runReview, id = Number(el.dataset.id);
    if (r) tickTodos(r.pid, [id], !checkedTodoIds(r.pid).includes(id));
  },
  rqAll: el => {
    const r = ui().runReview;
    if (r) tickTodos(r.pid, (store.data.todos[r.pid] || []).filter(t => t.status === 'pending').map(t => t.id), el.dataset.all !== '1');
  },
  rqBulkToggle: () => { const r = ui().runReview; if (r) set({ runReview: { ...r, bulk: !r.bulk } }); },
  rqSet: el => { const r = ui().runReview; if (r) setTodos(r.pid, [Number(el.dataset.id)], el.dataset.k, el.dataset.k === 'priority' ? Number(el.dataset.v) : el.dataset.v); },
  rqBulk: el => {
    const r = ui().runReview, ids = r ? checkedTodoIds(r.pid) : [];
    if (ids.length) setTodos(r.pid, ids, el.dataset.k, el.dataset.k === 'priority' ? Number(el.dataset.v) : el.dataset.v);
  },
  rqSaveText: el => el.blur(),            // Enter: leaving the box saves it (focusout below)
  rqQueue: () => busy('backlogRun', async () => {
    const r = ui().runReview;
    if (!r) return;
    const box = document.activeElement;
    if (box && box.dataset && (box.dataset.input || '').startsWith('rq.text.')) box.blur();
    await Promise.allSettled([...todoSaves]);          // ticks, settings and rewording reach the server first
    const n = checkedTodoIds(r.pid).length;
    try {
      const v = await endpoints.runTodos(r.pid);
      setData({ todos: { ...store.data.todos, [r.pid]: v } });
      set({ sheet: null, runReview: null });
      toast(`Queued ${plural(n, 'task')} in your order`);
    } catch (e) { fail(e); }
  }).then(again),
  setProjectSetting: el => {
    const k = el.dataset.k, raw = el.dataset.v;
    patchProjectSettings(ui().projectId, { [k]: raw === 'true' ? true : raw === 'false' ? false : Number(raw) });
  },
  setProjectAccount: el => {
    const pid = ui().projectId, id = el.dataset.v;   // "" = the default account
    patchProjectSettings(pid, { claude_account: id }).then(() => {
      setData({ projects: store.data.projects.map(x => (x.id === pid ? { ...x, claude_account: (store.data.projectSettings[pid] || {}).claude_account_in_use } : x)) });
      more.settings().then(v => setData({ settings: v }), () => {});
    });
  },
  deploy: async el => {
    const pid = Number(el.dataset.pid);
    let s = store.data.projectSettings[pid];
    try { s = await more.projectSettings(pid); setData({ projectSettings: { ...store.data.projectSettings, [pid]: s } }); } catch (e) { if (!s) return fail(e); }
    const targets = s.deploy_targets || [];
    if (!targets.length) { toast('No deploy targets yet - add them in this project\'s Settings'); return navigate('project', pid, 'settings'); }
    const list = targets.map((t, i) => `${i + 1}. ${t.name}${t.instructions ? ' - ' + t.instructions : ''}`).join('\n');
    sendReply(`Deploy this project to each of these, in order, and report the outcome of each. Ask before any step that can't be undone.\n${list}`);
  },
  addDeployTarget: () => {
    const pid = ui().projectId, s = store.data.projectSettings[pid];
    const n = document.getElementById('deployName'), i = document.getElementById('deployHow');
    const name = n && n.value.trim();
    if (!s || !name) return toast('Give the target a name, like GitHub or Production org');
    patchProjectSettings(pid, { deploy_targets: [...(s.deploy_targets || []), { name, instructions: (i && i.value.trim()) || '' }] });
    n.value = ''; if (i) i.value = '';
  },
  removeDeployTarget: el => {
    const pid = ui().projectId, s = store.data.projectSettings[pid];
    if (s) patchProjectSettings(pid, { deploy_targets: (s.deploy_targets || []).filter((_, i) => i !== Number(el.dataset.i)) });
  },
  removeAllowed: el => {
    const pid = ui().projectId, s = store.data.projectSettings[pid];
    if (!s) return;
    patchProjectSettings(pid, { allow_commands: s.allow_commands.filter((_, i) => i !== Number(el.dataset.i)) });
  },
  budgetStep: el => {
    const pid = ui().projectId, s = store.data.projectSettings[pid];
    if (s) patchProjectSettings(pid, { budget_usd: Math.max(0, s.budget_usd + Number(el.dataset.d)) });
  },
  activityFilter: el => set({ activityFilter: el.dataset.f }),
  fillReply: el => { putText('reply', el.dataset.text); set({ focus: 'replyInput' }); },
  quickReply: el => sendReply(el.dataset.text),
  sendReply: () => sendReply(),
  stopTask: el => more.stop(Number(el.dataset.id)).then(() => { chatToast('Stopped', Number(el.dataset.id), { full: 'Stopped · moving to the next task' }); again(); }, fail),
  chatNoteAct: () => { const a = noteAction; noteAction = null; clearTimeout(noteTimer); set({ chatNote: null }); if (a) a(); },
  pauseTask: el => holdTask(Number(el.dataset.id), true),
  playTask: el => holdTask(Number(el.dataset.id), false),
  palProject: el => { const p = ui().palette; if (p) { p.projectId = Number(el.dataset.id); p.error = ''; requestRender(); } },
  palMode: el => { const p = ui().palette; if (p) { p.mode = el.dataset.id; p.error = ''; requestRender(); } },
  palAccount: el => { const p = ui().palette; if (p) { p.account = el.dataset.id; p.error = ''; requestRender(); } },
  askAccount: el => { Object.assign(ui().ask, { account: el.dataset.id, result: null }); requestRender(); },
  palModel: el => { const p = ui().palette; if (p) { p.model = el.dataset.id; p.error = ''; requestRender(); } },
  palPriority: el => { const p = ui().palette; if (p) { p.priority = Number(el.dataset.v); requestRender(); } },
  palKeep: () => { const p = ui().palette; if (p) { p.keep = !p.keep; requestRender(); } },
  palWhen: el => { const p = ui().palette; if (p) { p.when = el.dataset.k; p.error = ''; requestRender(); } },
  palRecipe: el => {
    const p = ui().palette, r = (store.data.recipes || []).find(x => x.id === Number(el.dataset.id));
    if (!p || !r) return;
    if (p.when === 'now') runRecipe(r.id, r.target === 'all' ? null : p.projectId);
    else putText('palette', r.prompt);
  },
  palRecent: el => {
    const p = ui().palette, t = taskById(Number(el.dataset.id));
    if (!p || !t) return;
    p.projectId = t.project_id;
    putText('palette', t.prompt);
  },
  submitPalette: () => submitPalette(),
  toggleHoldLow: () => {
    const on = !(store.data.settings && store.data.settings.hold_low_when_busy);
    setData({ settings: { ...store.data.settings, hold_low_when_busy: on } });
    more.setSettings({ hold_low_when_busy: on }).then(v => setData({ settings: v }), fail);
  },
  toggleWeeklyPace: () => {
    const on = !(store.data.settings && store.data.settings.notify_weekly_pace !== false);
    setData({ settings: { ...store.data.settings, notify_weekly_pace: on } });
    more.setSettings({ notify_weekly_pace: on }).then(v => setData({ settings: v }), fail);
  },
  testPush: () => more.testPush().then(r => toast(r.sent ? 'Test push sent · check your phone' : "Couldn't reach ActivitySmith"), fail),
  restartOctopus: () => restartOctopus(),
  hardRestartOctopus: () => restartOctopus(true),
  offlineDismiss: () => set({ offlineHidden: store.data.offlineSince }),
  refreshNow: () => { set({ refreshing: true }); refresh().catch(onPollError).finally(() => set({ refreshing: false })); },
  offlineRetry: () => { set({ offlineChecking: true }); refresh().catch(onPollError).finally(() => set({ offlineChecking: false })); },
};

// Octopus: plans, routing history, providers, settings, voice, task details (octopus-ui.js).
Object.assign(ACTIONS, octopusActions({ set, toast, fail, again, armAutoOpen, requestRender, navigate, openVoicePane: () => showPane({ type: 'voice' }) }));
// Project calendar: timeline items, the Calendar page, the Good morning briefing (timeline-ui.js).
Object.assign(ACTIONS, timelineActions({ set, toast, fail, navigate, openPalette }));
// Project phases and autopilot: the Phases tab (phases-ui.js).
Object.assign(ACTIONS, phaseActions({ set, toast, fail }));
Object.assign(ACTIONS, overviewActions({ set, toast, fail, navigate, again, submitNewProject: addProjectSubmit }));
Object.assign(ACTIONS, crmTableActions({ set, toast, fail }));
Object.assign(ACTIONS, clientActions({ set, toast, fail, navigate, confirmNear }));
Object.assign(ACTIONS, teamActions({ set, toast, fail, navigate, confirmNear }));
Object.assign(ACTIONS, crmActivityActions({ set, toast, fail, navigate, confirmNear }));
Object.assign(ACTIONS, crmDashActions({ set, toast, fail }));
crmDragSetup((id, stage) => ACTIONS.ctDrop(id, stage));
Object.assign(ACTIONS, padActions({ set, toast, fail, navigate }));
Object.assign(ACTIONS, checklistActions({ set, toast, fail }));
Object.assign(ACTIONS, backupActions({ set, toast, fail }));
Object.assign(ACTIONS, sitesActions({ set, toast, fail }));
Object.assign(ACTIONS, previewActions({ set, fail }));

const SUBMITS = {
  saveKey: () => {
    const v = (document.getElementById('keyInput') || {}).value || '';
    if (!v.trim()) return;
    ls(KEY_STORAGE, v.trim());
    set({ gateError: '' });
    boot();
  },
  addProject: () => addProjectSubmit(),
  siteLearn: () => ACTIONS.siteLearn(),
  addTodo: () => {
    const pid = ui().projectId, text = ui().todoText.trim();
    if (!text) return;
    const images = imageDraft('todo') || [];
    const problem = imageProblem(images);
    if (problem) { toast(problem); return; }
    const ids = readyIds(images);
    clearSent(images);
    set({ todoText: '' });
    const el = document.getElementById('todoInput'); if (el) { el.value = ''; if (el._grow) el._grow.reset(); }
    endpoints.addTodo(pid, text, ids.length ? ids : undefined).then(t => setData({ todos: { ...store.data.todos, [pid]: [...(store.data.todos[pid] || []), t] } }), fail);
  },
  // The backlog's chat-style box: parks the text with the picked model / mode, nothing runs.
  addBlog: () => {
    const pid = ui().projectId, text = ui().blText.trim();
    if (!text) return;
    const images = imageDraft('blog') || [];
    const problem = imageProblem(images);
    if (problem) { toast(problem); return; }
    const ids = readyIds(images);
    const opts = {};
    if (ui().blModel && ui().blModel !== 'auto') opts.model = ui().blModel;
    if (ui().blMode) opts.permission_mode = ui().blMode;
    if (ui().blAccount && usesClaudeAccount({ d: store.data }, ui().blModel)) opts.only_account = ui().blAccount;
    clearSent(images);
    set({ blText: '' });
    const el = document.getElementById('blogInput'); if (el) { el.value = ''; if (el._grow) el._grow.reset(); }
    endpoints.addTodo(pid, text, ids.length ? ids : undefined, opts).then(t => { setData({ todos: { ...store.data.todos, [pid]: [...(store.data.todos[pid] || []), t] } }); toast('Saved to backlog'); }, fail);
  },
  addNote: () => {
    const pid = ui().projectId, text = ui().noteText.trim(), kind = ui().noteKind;
    const images = imageDraft('note') || [];
    if (!text && !images.length) return;
    const problem = imageProblem(images);
    if (problem) { toast(problem); return; }
    const ids = readyIds(images);
    clearSent(images);
    set({ noteText: '' });
    const el = document.getElementById('noteInput'); if (el) { el.value = ''; if (el._grow) el._grow.reset(); }
    endpoints.addNote(pid, text, kind, ids.length ? ids : undefined).then(n => setData({ notes: { ...store.data.notes, [pid]: [...(store.data.notes[pid] || []), n] } }), fail);
  },
  denyWithNote: el => deny(Number(el.dataset.id), ui().denyNote.trim()),
};

root.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el || !root.contains(el) || el.disabled) return;
  const fn = ACTIONS[el.dataset.action];
  if (!fn) return;
  // Native controls inside a sheet (the color picker's <input>/<label>) reach here via the scrim's data-action; cancelling their click stops the picker opening.
  if (el.tagName !== 'INPUT' && !e.target.closest('input, label, select')) e.preventDefault();
  if (ui().navDrawer && el.dataset.action !== 'toggleSidebar' && el.closest('.sidebar')) store.ui.navDrawer = false;
  const fromDrawer = ui().rdrawer && el.closest('.rdrawer__panel'), was = [ui().view, ui().pane];
  fn(el, e);
  if (fromDrawer && (ui().view !== was[0] || ui().pane !== was[1])) set({ rdrawer: null });
});
root.addEventListener('contextmenu', e => {
  if (e.target.closest('input, textarea, select, [contenteditable]')) return;
  const tab = e.target.closest('.ptab[data-tabid]');
  const at = { x: Math.min(e.clientX, window.innerWidth - 170), y: Math.min(e.clientY, window.innerHeight - 130) };
  if (tab && isDesk()) {
    e.preventDefault();
    return set({ linkMenu: null, tabMenu: { id: Number(tab.dataset.tabid), url: linkUrl({ dataset: { action: 'openChat', id: tab.dataset.tabid } }), ...at } });
  }
  // Projects, navigation items and tasks: right-click offers "Open in new tab".
  const el = e.target.closest('[data-action="go"],[data-action="goPersonal"],[data-action="openProject"],[data-action="openChat"],[data-action="openApprovalForTask"]');
  const url = el && root.contains(el) && linkUrl(el);
  if (!url) return;
  e.preventDefault();
  set({ tabMenu: null, linkMenu: { url, ...at } });
});
root.addEventListener('submit', e => {
  const form = e.target.closest('[data-submit]');
  if (!form) return;
  e.preventDefault();
  const fn = SUBMITS[form.dataset.submit];
  if (fn) fn(form, e);
});
root.addEventListener('input', e => {
  const el = e.target;
  const path = el.dataset && el.dataset.input;
  if (!path) return;
  const v = el.value;
  if (path === 'ask') { ui().ask.text = v; if (ui().ask.result && ui().ask.result.error) ui().ask.result = null; }
  else if (path === 'reply') store.ui.reply = v;
  else if (path === 'denyNote') store.ui.denyNote = v;
  else if (path === 'todoText') store.ui.todoText = v;
  else if (path === 'blogText') store.ui.blText = v;
  else if (path === 'todoEdit.text' && ui().todoEdit) { ui().todoEdit.text = v; ui().todoEdit.error = ''; }
  else if (path.startsWith('quickNew.') && ui().quickNew) { ui().quickNew[path.slice(9)] = v; ui().quickNew.error = ''; }
  else if (path === 'noteText') store.ui.noteText = v;
  else if (path === 'noteEdit.text' && ui().noteEdit) { ui().noteEdit.text = v; ui().noteEdit.error = ''; }
  else if (path.startsWith('sched.') && ui().schedEdit) { const e = ui().schedEdit, k = path.slice(6); if ((k === 'time' || k === 'date') && v !== e[k]) e.enabled = true; e[k] = v; e.error = ''; }
  else if (path === 'palette.text') { if (ui().palette) { ui().palette.text = v; ui().palette.error = ''; } }
  else if (path.startsWith('octo.')) octoInput(path, v, { fail });
  else if (path.startsWith('ev.') || path.startsWith('set.')) timelineInput(path, v, { fail });
  else if (path.startsWith('ph.')) phaseInput(path, v);
  else if (path.startsWith('ov.') || path.startsWith('ovLink.')) overviewInput(path, v);
  else if (path.startsWith('pad.')) padInput(path, v);
  else if (path.startsWith('chk.')) checklistInput(path, v);
  else if (path.startsWith('onboard.')) overviewInput(path, v);
  else if (path === 'doc.q') store.ui.docs = { ...ui().docs, q: v };
  else if (path === 'doc.name') store.ui.docs = { ...ui().docs, editName: v };
  else if (path.startsWith('ct.')) crmtInput(path, v);
  else if (path.startsWith('cl.') || path.startsWith('cn.')) clientInput(path, v);
  else if (path.startsWith('tm.')) teamInput(path, v);
  else if (path.startsWith('act.') || path.startsWith('fu.')) crmActivityInput(path, v);
  else if (path === 'pl.q') store.ui.projectsList = { ...ui().projectsList, q: v };
  else if (path === 'msg.q') { store.ui.messages = { ...ui().messages, q: v, limit: 40 }; loadMessagesSoon(); }
  else if (path === 'spot') { store.ui.search = { ...ui().search, q: v }; store.ui.spot = { ...ui().spot, sel: 0, error: '' }; loadSearchSoon(); }
  else if (path === 'search') { store.ui.search = { ...ui().search, q: v, open: true, sel: 0 }; loadSearchSoon(); }
  else if (path === 'retryEdit.prompt' && ui().retryEdit) { ui().retryEdit.prompt = v; ui().retryEdit.error = ''; }
  else if (path.startsWith('edit.') && ui().edit) { ui().edit[path.slice(5)] = v; ui().edit.error = ''; }
  else if (path.startsWith('rq.') && ui().runReview) {
    const [, kind, id] = path.split('.'), r = ui().runReview;
    if (kind === 'text') r.text[id] = v;
    else if (kind === 'model') setTodos(r.pid, [Number(id)], 'model', v);
    else if (kind === 'acct') setTodos(r.pid, [Number(id)], 'only_account', v === 'auto' ? '' : v);
    else if (kind === 'bulk') { if (v) setTodos(r.pid, checkedTodoIds(r.pid), id, id === 'only_account' && v === 'auto' ? '' : v); }
  }
  requestRender();
});
// Attaching pictures: the 📎 buttons (a hidden file input each) and pasting a picture into a prompt box.
setRedraw(requestRender);
root.addEventListener('change', e => {
  const el = e.target;
  if (el.dataset && el.dataset.qopt) return quickRunOpt(Number(el.dataset.id), el.dataset.qopt, el.value);
  if (el.dataset && el.dataset.acctColor) return ACTIONS.acctColor({ dataset: { id: el.dataset.acctColor, v: el.value } });
  if (el.dataset && 'restoreFile' in el.dataset) { const f = el.files[0]; el.value = ''; return backupPicked(f); }
  if (el.dataset && 'ctF' in el.dataset && el.tagName !== 'BUTTON') return ACTIONS.ctFilter(el);
  if (el.dataset && 'ctMove' in el.dataset) return ACTIONS.ctMove(el);
  if (el.dataset && el.dataset.clChange && ACTIONS[el.dataset.clChange]) return ACTIONS[el.dataset.clChange](el);
  if (el.dataset && 'ovColor' in el.dataset) return ACTIONS.ovColor({ dataset: { v: el.value } });
  if (el.dataset && 'ovLogobg' in el.dataset) return ACTIONS.ovLogoBg({ dataset: { v: el.value } });
  if (el.dataset && 'ovImage' in el.dataset) { const f = el.files[0]; el.value = ''; return ACTIONS.ovImagePicked(f); }
  if (el.dataset && 'docSort' in el.dataset) return ACTIONS.docSort(el);
  if (el.dataset && 'docFile' in el.dataset) { const files = [...el.files]; el.value = ''; return docsPicked(ui().projectId, files); }
  if (!el.dataset || !el.dataset.attach) return;
  attachTo(el.dataset.attach, el.files);
  el.value = '';                 // so picking the same file again still fires
});
const PASTE_TARGETS = { ask: 'ask', reply: 'reply', 'palette.text': 'palette', todoText: 'todo', blogText: 'blog', 'todoEdit.text': 'todoEdit',
  noteText: 'note', 'noteEdit.text': 'noteEdit' };
root.addEventListener('paste', e => {
  const target = PASTE_TARGETS[e.target.dataset && e.target.dataset.input];
  const cd = e.clipboardData;
  let files = cd ? [...cd.files].filter(f => f.type.startsWith('image/')) : [];
  if (!files.length && cd && cd.items) {        // some browsers/apps only expose a copied picture as an item
    files = [...cd.items].filter(i => i.kind === 'file' && i.type.startsWith('image/')).map(i => i.getAsFile()).filter(Boolean);
  }
  if (!target || !files.length) return;
  if (!e.clipboardData.getData('text/plain')) e.preventDefault();      // a copied picture, not copied text
  attachTo(target, files);
});
root.addEventListener('focusout', e => {
  const m = /^rq\.text\.(\d+)$/.exec((e.target.dataset && e.target.dataset.input) || '');
  if (m) saveReviewText(Number(m[1]));
});
// Focusing the box (click, tab, "/") shows the results; anywhere else closes them.
root.addEventListener('focusin', e => {
  if (e.target.id === 'topSearch' && !ui().search.open) set({ search: { ...ui().search, open: true } });
});
document.addEventListener('pointerdown', e => {
  if (ui().search.open && !e.target.closest('.tsearch')) closeSearch();
}, true);
root.addEventListener('keydown', e => {
  const el = e.target;
  if (el.id === 'topSearch') return searchKey(e);
  if (el.id === 'spotInput') return spotKey(e);
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && el.dataset && el.dataset.enter) {
    e.preventDefault();
    ACTIONS[el.dataset.enter](el, e);
  }
});

// ---------------------------------------------------------------- keyboard shortcuts
window.addEventListener('keydown', e => {
  if (!apiKey()) return;
  const u = ui();
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea';
  const mod = e.metaKey || e.ctrlKey;
  if (!u.booted) return;
  if (u.shortcutRec && u.view === 'settings') return recordShortcut(e);   // left the page mid-change: ignore
  if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); u.sheet === 'palette' ? set({ sheet: null, palette: null }) : openPalette(); return; }
  if (e.key === 'Escape') {
    if (u.linkMenu || u.tabMenu) return set({ linkMenu: null, tabMenu: null });
    if (u.spot.open) return spotClose();
    if (u.search.open) return closeSearch();
    if (notify && notify.isOpen()) return notify.close();
    if (u.ask.menu || u.projectsList.menu || u.replyMenu || u.blMenu || u.messages.menu || u.acctMenu) return ACTIONS.closeMenus();
    if (u.sheet) { if (u.sheet === 'runReview' && document.activeElement) document.activeElement.blur(); return set({ sheet: null, palette: null, runReview: null }); }
    if (u.chk && u.chk.full != null) return ACTIONS.chkFull({ dataset: {} });   // full-screen To-dos
    if (u.navDrawer) return set({ navDrawer: false });
    if (u.ovEdit) return set({ ovEdit: null });             // a project Overview field open for editing
    if (u.ask.open) return ACTIONS.askClose();
    if (u.rdrawer) return set({ rdrawer: null });
    if (u.pane) return closePane();
    return;
  }
  const chkAct = !typing && !e.target.isContentEditable && chkUndoKey(e);   // To-dos undo / redo; text boxes keep their own
  if (chkAct) { e.preventDefault(); ACTIONS[chkAct](); return; }
  const act = shortcutFor(e, typing || tag === 'select' || e.target.isContentEditable);
  if (act && SHORTCUT_RUN[act]() !== false) { e.preventDefault(); return; }
  if (typing || mod || e.altKey) return;
  if (e.key === 'n') { e.preventDefault(); openPalette(); }
  else if (e.key === '[' && isDesk()) ACTIONS.toggleSidebar();
  else if (['1', '2', '3', '4', '5', '6'].includes(e.key)) navigate(VIEWS[Number(e.key) - 1]);
  else if (e.key === '7') navigate('calendar');
  else if (e.key === 'g') navigate('briefing');
});

// What each changeable shortcut (shortcuts.js, Settings > Shortcuts) does. Return false to let the
// key through - e.g. Approve with nothing waiting.
const SHORTCUT_RUN = {
  newTask: () => { if (ui().sheet !== 'palette') openPalette(); else set({ focus: 'palInput' }); },   // never wipe a half-typed task
  toggleSidebar: () => (isDesk() ? ACTIONS.toggleSidebar() : false),
  // Each press shows the next right-side panel on its own (rail order): none -> Queue -> Messages -> Calendar -> none.
  cycleSides: () => {
    if (!isDesk()) return false;
    const open = ui().sides, next = SIDE_KEYS[Math.max(-1, ...open.map(k => SIDE_KEYS.indexOf(k))) + 1];
    for (const k of open) if (k !== next) setSide(k, false);
    if (next) setSide(next, true);
  },
  spotlight: () => (ui().spot.open ? spotClose() : spotOpen()),
  search: () => focusSearch(),
  approve: () => { const first = pendingApprovals()[0]; return first ? approve(first.id) : false; },
  deny: () => { const first = pendingApprovals()[0]; return first ? deny(first.id) : false; },
  briefing: () => navigate('briefing'),
  keys: () => set({ sheet: 'keys' }),
};

// Settings > Shortcuts > Change: the next key press becomes the shortcut. Esc cancels, Backspace turns it off.
function recordShortcut(e) {
  const combo = comboOf(e);
  if (!combo) return;                        // a lone ⌘ / ⇧ - wait for the real key
  e.preventDefault(); e.stopPropagation();
  const id = ui().shortcutRec;
  if (combo === 'Escape') return set({ shortcutRec: null, shortcutMsg: '' });
  if (combo === 'Backspace' || combo === 'Delete') { setBinding(id, ''); return set({ shortcutRec: null, shortcutMsg: '' }); }
  const msg = setBinding(id, combo);
  set(msg.includes('built in') ? { shortcutMsg: msg } : { shortcutRec: null, shortcutMsg: msg });
}

// ---------------------------------------------------------------- clock
setInterval(() => {
  const u = ui(), d = store.data;
  checkAutoOpen();
  const live = d.tasks.some(t => t.status === 'running' || t.status === 'rate_limited') || pendingApprovals(d).length;
  if (live || !u.booted) requestRender();
}, 1000);
window.addEventListener('online', requestRender);
window.addEventListener('offline', requestRender);
setInterval(requestRender, 30000);   // "2m ago" labels

// Den restarted (new boot id from /health) -> reload this page so it runs the new code
let bootId = null;
setInterval(async () => {
  if (document.hidden) return;
  try {
    const r = await fetch('/health', { cache: 'no-store' });
    const b = (await r.json()).boot;
    if (!b) return;
    if (bootId && b !== bootId) return location.reload();
    bootId = b;
  } catch { /* server is down mid-restart; try again next tick */ }
}, 3000);

// ---------------------------------------------------------------- restart Octopus (reload the service's code, then this page)
// Restart: the service holds the waiting tasks, drains (finishes what's running, starts nothing
// new), exits, and launchd starts it again - which lets go of the tasks it held. Hard restart:
// the same, but what's running is stopped instead of waited for (Retry continues it).
// A new pid means the new code is up, so reload to pick up its dashboard too.
let restartTimer = 0;
async function restartOctopus(hard = false) {
  const cur = ui().restart;
  if (cur && (!hard || cur.hard)) return;      // a hard restart may overtake a normal one still waiting
  const ids = list => list.map(t => '#' + t.id).join(', ');
  const busy = store.data.tasks.filter(t => t.status === 'running');
  const waiting = store.data.tasks.filter(t => (t.status === 'queued' || t.status === 'rate_limited') && !t.paused).length;
  const held = waiting ? `\n\n${plural(waiting, 'waiting task')} ${waiting > 1 ? 'are' : 'is'} paused meanwhile and resumed once it's back. Tasks you paused yourself stay paused.` : '';
  const ask = hard
    ? `Hard restart Octopus now?${busy.length ? `\n\nThis stops ${ids(busy)} right away. ${busy.length > 1 ? "They're" : "It's"} marked "Stopped by hard restart" - Retry continues ${busy.length > 1 ? 'them' : 'it'}.` : ''}${held}\n\nIt takes a few seconds, then this page reloads.`
    : `Restart Octopus to load the latest code?${busy.length ? `\n\nIt's working on ${ids(busy)} right now and finishes ${busy.length > 1 ? 'those' : 'that'} first, starting nothing new meanwhile.` : ''}${held}\n\nThis page reloads when it's back.`;
  if (!confirm(ask)) return;
  try {
    const pid = cur ? cur.pid : (await more.service()).pid;
    const r = await more.restartService(hard);
    set({ restart: { pid, waitingOn: r.waiting_on, hard: !!r.hard } });
    toast(hard
      ? (r.stopped.length ? `Hard restart · stopped ${r.stopped.map(id => '#' + id).join(', ')}` : 'Restarting Octopus · back in a few seconds')
      : (r.waiting_on.length ? `Restart queued · after ${r.waiting_on.map(id => '#' + id).join(', ')}` : 'Restarting Octopus · back in a few seconds'));
    watchRestart();
  } catch (e) { fail(e); }
}
function watchRestart() {
  clearTimeout(restartTimer);
  restartTimer = setTimeout(async () => {
    const r = ui().restart;
    if (!r) return;
    try {
      const s = await more.service();
      if (s.pid !== r.pid) return location.reload();
      if (String(s.waiting_on) !== String(r.waitingOn) || !!s.hard !== !!r.hard) set({ restart: { ...r, waitingOn: s.waiting_on, hard: !!s.hard } });
    } catch {}   // down while it restarts
    watchRestart();
  }, 2000);
}
// A restart asked from another device: show it here too, and reload when it's done.
function checkRestarting() {
  more.service().then(s => {
    if (s.restarting && !ui().restart) { set({ restart: { pid: s.pid, waitingOn: s.waiting_on, hard: !!s.hard } }); watchRestart(); }
  }, () => {});
}

// ---------------------------------------------------------------- boot
let stopPolling = null;
function onPollError(e) {
  if (/X-API-Key/i.test(e.message)) {
    try { localStorage.removeItem(KEY_STORAGE); } catch {}
    if (stopPolling) { stopPolling(); stopPolling = null; }
    set({ gateError: "That key didn't work. Check API_KEY in the launchd plist." });
    return;
  }
  // Count failures in a row and note when this outage began - the offline popup waits for a second miss.
  const d = store.data;
  setData({ error: e.message, offlineSince: d.offlineSince || new Date().toISOString(), offlineFails: (d.offlineFails || 0) + 1 });
}

function boot() {
  // SwiftBar links here as /dashboard?status=<status>: open Activity on that filter.
  const status = new URLSearchParams(location.search).get('status');
  if (status && ['done', 'error', 'needs_review', 'cancelled'].includes(status)) {
    store.ui.activityFilter = status;
    history.replaceState(null, '', location.pathname + '#/activity');
  }
  if (!apiKey()) { requestRender(); return; }
  if (!stopPolling) stopPolling = startPolling(onPollError);
  checkRestarting();
  applyRoute();
  requestRender();
}

startOctopusData();
boot();
