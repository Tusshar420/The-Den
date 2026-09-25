# Prompt for Claude Code: toasts + notification centre

Copy the updated `design_handoff_tushars_workspace/` folder over the one in the `claude-queue` repo root. Then paste everything below the line into Claude Code, run from the repo root.

---

You're adding a finished design update to the `claude-queue` dashboard: **new top-right toasts** and a **notification centre** (a bell in the topbar plus a panel). Build it **exactly as designed**. Don't restyle, simplify or change the copy.

## Read first
1. `design_handoff_tushars_workspace/README.md`, the section **Toasts & notification centre**. This is the spec.
2. `design_handoff_tushars_workspace/src/js/notifications.js`: `NotificationCenter`, `KINDS`, `parseToast()`. **Use it as-is.**
3. `design_handoff_tushars_workspace/src/css/notifications.css`. **Use it as-is.**
4. `src/js/icons.js` has new icons (`bell`, `close`, `nCheck` … `nInfo`). `src/css/components.css` no longer has `.toast` rules. In `src/js/api.js`, `store.ui.toast` has been removed.
5. The visual reference is `reference/Workspace Prototype.dc.html`. Open it in a browser, then:
   - finish a task, cancel a queued task (Undo), and send an Ask;
   - tap the bell;
   - switch the Tweaks panel between phone and responsive.

   In its logic, `notifVals()` and `mkNotif()` are the behaviour.

## Which case applies
- **If the redesign from `CLAUDE_CODE_PROMPT.md` is already built** (`static/js/views/shell.js` exists): integrate into it as described below.
- **If it isn't built yet:** do this as part of step 3 (App shell) of `CLAUDE_CODE_PROMPT.md`, not as the old bottom-centre toast.
- **If `dashboard.html` is still the legacy single file:** stop and ask me which of the two I want before writing code.

## Steps: commit after each
1. **Copy files.**
   - Copy `src/js/notifications.js` into `static/js/`.
   - Copy `src/css/notifications.css` into `static/css/`.
   - Copy the updated `src/js/icons.js`, `src/css/components.css` and `src/js/api.js` into `static/`. If the static copies were edited locally, merge them; don't overwrite those edits.
   - Link `notifications.css` in `dashboard.html` after `components.css`.
2. **Shell markup** (`views/shell.js`):
   - Add an empty `<button id="bell"></button>` to the topbar, between the usage pill and the density toggle.
   - Add an empty `<div class="notif-root"></div>` as a **direct child of `.app`**. `.app` must be `position: relative`.
   - Render both once, not on every re-render. `NotificationCenter` owns their contents.
3. **Mount it** once in `app.js`:
   ```js
   import { NotificationCenter } from './notifications.js';
   import { octopusSVG } from './octopus.js';
   import { store, subscribe, uiStatus } from './api.js';

   const taskById = id => store.data.tasks.find(t => t.id === id);
   export const notify = new NotificationCenter({
     root: document.querySelector('.notif-root'),
     bell: document.getElementById('bell'),
     statusOf: id => { const t = taskById(id); return t ? uiStatus(t) : null; },
     onOpenTask: id => store.set({ pane: { type: uiStatus(taskById(id)) === 'approval' ? 'approval' : 'chat', taskId: id } }),
     emptyArt: () => octopusSVG('sleep'),
     modelLabel: t => store.data.models.find(m => m.id === t.model_id)?.label || '',
   });
   subscribe(s => notify.ingest(s.data));   // no-op unless store.data is a new object (i.e. a fresh poll)
   ```
4. **Replace every old toast call.**
   - Replace each `store.set({ toast: … })` or `showToast(…)` with `notify.toast(text, { action, actionLabel, taskId })`. Keep the **exact existing strings** from the README's message list.
   - Pass `taskId` wherever the toast is about a task, so tapping it opens that task.
   - Delete the old toast renderer.
5. **Keyboard.** In the global keydown handler, Esc closes the panel first: `if (notify.isOpen()) return notify.close();`. Only after that does it close the sheet, then the pane.
6. **Backend: optional, ask me first.** The notification list is derived client-side from polls, so no backend change is required. Before building a server-side `notifications` table plus `GET /notifications` and `POST /notifications/read` (so read-state syncs across devices, alongside the existing ntfy pushes in `worker.py` and `approval_server.py`), **ask me** whether to do it now or leave it client-only.

## Constraints
- Vanilla JS ES modules, no build step, no framework. Escape every task or model string, which `notifications.js` already does with `escapeHTML`.
- Don't hard-code colours outside `KINDS`. The CSS reads `--k` and `--kt`.
- 44px targets on phone for the bell hit area. It's 40px visually inside the 44px topbar row, same as the density toggle.
- Respect `prefers-reduced-motion`. `tokens.css` already handles it.

## Done when
- At 375px and 1280px, the toasts, bell badge and panel match the prototype: position, stacking (newest at the bottom, maximum 3), slide-in, the countdown bar, and Undo.
- New approvals and done / failed / review / rate-limited transitions from a real poll produce a toast plus an unread row. Reloading doesn't duplicate them.
- "Needs you" CTAs disappear once the task is handled.
- Closing the panel clears the badge.
- There are no console errors and no horizontal overflow at 320px.

Start by telling me which case above applies and listing the files you'll touch. Then build.
