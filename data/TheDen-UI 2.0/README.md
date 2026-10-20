# Handoff: The Den — full redesign (Home + all pages)

## Overview
A redesign of **The Den**, a self-hosted workspace where the agent "Octopus" runs a task queue across projects (Claude Code agent + API models), with approvals, routing, usage tracking, CRM, inbox, calendar and notes. This bundle covers the app shell, Home, and every page reachable from the sidebar.

## How to use this bundle (give this to Claude Code)
1. Put this folder inside the Den repo (e.g. `/design/den_redesign/`).
2. Open `Den Home.dc.html` in a browser (with `support.js` next to it) and click through everything. Or open `Den - standalone.html`, which works offline with nothing else.
3. Prompt for Claude Code: *"Recreate the UI in /design/den_redesign pixel-for-pixel in our frontend. Read README.md first. Match every color, size, radius, spacing, font and interaction exactly. Use the .dc.html files as the source of truth for markup and styles, since every style is inline on the element. Wire the toasts to our real APIs."*
4. Each `.dc.html` has two parts: the **markup** (between `<x-dc>` tags, with `{{ holes }}` and `<sc-for>` / `<sc-if>` loops and conditions) and the **logic class** (`renderVals()`, which computes everything the markup shows). Treat them like JSX plus a component class.

## About the design files
The `.dc.html` files are **design references built in HTML** — working prototypes that show the intended look and behavior. They are **not production code to copy**. Recreate them in the Den's existing frontend stack (React / Vue / etc.) using its established patterns, state management and API layer. Open `Den Home.dc.html` in a browser (keep `support.js` next to it) to click through everything; pages are loaded as child components.

All data in the prototypes is **mock data** generated inline. Every "toast" (small pop-up confirmation) marks a spot where a real API call goes.

## Fidelity
**High-fidelity.** Final colors, type, spacing, radii, states and interactions. Recreate pixel-accurately.

## Global layout (app shell — `Den Home.dc.html`)
Three columns on desktop, full height (`100vh`), with the page area scrolling on its own.

1. **Left sidebar**: 256px wide, or a 72px icon-only strip when collapsed (`[` or ⌘B).
   - **Top**: workspace switcher (logo + name, dropdown with workspaces and "Add workspace"), a "Working" status chip and a "Jump to… ⌘K" button.
   - **Groups** (each collapsible): Pinned, Workspace (Den Home ⌘1, Projects ⌘2, Clients, CRM), Automation (Recipes ⌘3, Activity ⌘4), Personal assistant (Email ⌘5, Connections), Octopus (Token usage, AI providers, Routing, Voice).
   - **Footer**: Settings and Voice.
2. **Main column**
   - **Top bar** (blurred glass), left to right:
     - Page eyebrow and title.
     - ⌘K search: a full bar when the main area is at least 1040px wide, an icon below that.
     - View-mode dropdown: Standard / Compact / Focus, `F` toggles Focus.
     - Usage capsule: per account, a session ring and a weekly ring with reset times. The weekly ring hides below 1000px of main width, the reset labels below 1320px. It opens a dropdown with accounts, a forecast with "Pass on N tasks to <account>", and API budget bars.
     - Light/dark theme toggle (⇧T).
   - **Page area** (scrolls).
   - **Ask bar** at the bottom (not on phone): one line until you type or attach an image. Then it shows the model dropdown, an Edits / Plan / Ask switch (only when the model is Claude Code), recent-project tags, Attach / Polish / Dictate and Send.
3. **Right rail**: 60px strip of icon tabs with badges.
   - **Tabs**: Notifications, Summary, Queue, Message center, Calendar, To-do, Reminders, Notes.
   - **Also on the strip**: a "+" button to add a panel, and a Refresh/Restart menu at the bottom.
   - **Panel**: a tab opens a 360px panel. On desktop it sits beside the page; on tablet it slides over; on phone it is full screen with the tabs at the bottom, wrapping onto a second line.

**Breakpoints** (measured on the app frame, not the window):
- **Phone** (< 760px): the left sidebar becomes a drawer, opened from the logo button or a left-edge chevron or swipe. A bottom tab bar shows Home, Projects, a coral + button (opens the ⌘K palette), Messages and Queue. Edge chevrons for both sidebars sit 96px above the bottom.
- **Tablet** (760–1179px): the sidebar is collapsed to the icon strip by default.
- **Desktop** (≥ 1180px).
- **Wide**: when the main area is at least 1440px, Home's side widgets switch to two columns.

**Page loading**: show a shimmer skeleton shaped like the page while it loads. Four variants: card grid, list + detail, board columns, rows.

## Screens
- **Home**
  - Health banner (dismissible).
  - Current-task tile:
    - Contents: status label, title, and chips for mode, agent, account and project (each chip is a dropdown).
    - Progress line: 64% bar, step, command preview and ETA.
    - Watch / Pause / Sleep as icon buttons, shortcuts W / P / S.
    - Approval state: shows the command, risk chips, a mic for voice approval, and Details / Deny (D) / Approve (A).
    - Shrinks to a sticky bar after scrolling 200px.
    - Variants: Working, Needs approval, Rate-limited, Idle.
  - Up next:
    - Rows with a drag handle (pointer-based reordering) and a priority menu (Urgent / High / Normal / Low).
    - "Run after" dependency, shown as an "after #829" chip.
    - Move-to-top and cancel buttons.
  - Completed list: same row style; clicking a row opens its chat.
  - Widgets (Customize mode: move up/down, remove, add back): Live activity, CRM today, Coming up, Quick actions, Scheduled.
  - The chat panel slides in from the right: 520px, or full screen on phone.
- **Projects** (`DenProjects`)
  - **Project list**: card grid with filters (All, Running, Needs you, Pinned, Hidden), search and pinning.
  - **Each card's ⋯ menu**: Open in Finder, Pause/Resume, Hide/Unhide, Archive, Delete (tap twice to confirm), Duplicate settings.
  - **Project detail header**: action buttons that collapse into a ⋯ menu as the header narrows (6 actions inline at ≥ 980px, 4 at ≥ 820px, 2 at ≥ 640px, otherwise all in the menu), plus a New task button that always stays visible.
  - **Project detail tabs**: customizable via the sliders icon (show/hide, reorder, pin). Tasks is pinned and stays visible on the left when the tab row scrolls.
  - **Tab contents**:
    - **Tasks**: Board (drag between Backlog / Up next / Running / Review / Done) or List. Each card has an expand icon that reveals its prompt, with Copy and Open chat.
    - **Overview**:
      - Summary strip: priority, health, due date, progress, value.
      - Project details: name, client dropdown, logo colour, category, stage, folder.
      - Description with "Generate from project".
      - Details: priority, health, owner, billing, contract value.
      - Links, Progress, Schedule (start / due / completed dates), Contacts and Tags.
      - Keep or remove: Pause / Hide / Archive / Delete.
    - **Chats**: thread list on the left, conversation on the right.
    - **Ideas**: parked thoughts that never run automatically. Each has Run (adds it to Up next), To backlog, Edit and Delete.
    - **Documents**: drag-and-drop upload area, document list and preview.
    - **Timeline**: milestones on a vertical line; you can add your own.
    - **Guidelines**:
      - Three kinds: Guideline, Requirement, Note.
      - Active guidelines and requirements are injected into every task prompt for the project. Notes are for the user only.
      - Each item has an on/off toggle and a "used in N tasks" count.
      - Pictures can be pasted or attached.
    - **Files**: VS Code-style file browser and editor.
      - Left: folder tree with a filter, new-file and collapse-all buttons, and git markers (M = modified, U = untracked).
      - Right: open-file tabs with an unsaved dot, a breadcrumb path, a line-number gutter and an editable text area.
      - ⌘S saves, Tab indents 2 spaces, Revert discards changes, and "Ask Octopus to edit" asks for a proposed change.
      - Status bar: branch, language, line count.
    - **Activity**, **Recipes**, **Changes** (stage files, a commit message with "Write it for me", Commit, and Push which needs approval), **Settings**.
- **Activity**: stat cards, a 14-day chart, a sticky filter bar (type chips + project), and a day-grouped timeline whose entries expand to show steps and errors (Open chat / Retry / Run again).
- **Email → Unified inbox**: mail, chats, reviews and approvals in one list.
  - Filters: All, Needs you, Email, Chats, Approvals.
  - Reading pane: Octopus summary, quick replies, "Draft with Octopus", Archive / Snooze.
- **CRM / Clients**: pipeline board with drag (Lead / Proposal / Negotiation / Won / Lost), a deal pop-up, summary figures; client list with health dot plus a detail timeline.
- **Notes**: list + editor with autosave, pin, Summarize, Turn into task, Remind me.
- **Recipes & schedules**: visual builder.
  - Trigger: schedule (days + time), an event, or manual only.
  - Steps: add, edit, reorder and remove.
  - Test run shows a dry-run log without sending anything.
- **Token usage**:
  - Account rings and forecast with "pass on".
  - Spend chart (Today / 7 days / 30 days) and cost by project and by model.
  - Budget-cap sliders.
  - **Who worked on what**:
    - Filters: When (Today / 7d / 30d / Custom: a single day or between two dates), Model, Project, Account.
    - Views: Project × model table (click a cell to see those runs), When (hour-of-day heatmap), All runs (sortable table).
- **AI providers**: three tabs.
  - **Providers**: health per provider (speed, error rate, a 24-hour chart), keys in failover order, a health check, fallback options.
  - **Models**: list of models with an on/off toggle.
    - Add AI: provider, model ID, display name, base URL (local or OpenAI-compatible only), API key, capabilities, Test connection.
    - Configure a model: rename, make default, what to use it for, capabilities, temperature, max output tokens, context window, system-prompt add-on, cost per 1M tokens, test, remove.
  - **Routing**: a test box (shows which rule and model a prompt would use) and an editable list of rules checked top to bottom.
- **Connections / Voice**: 8 service cards (Connect / Manage / Renew). Voice: mic test with level meter, voice choice, speed, hands-free switches, language.
- **Settings / Recycle bin**: searchable settings with shortcut rebinding (click, then press keys; clashes are detected). Bin: preview, Restore, Delete forever, Empty bin.
- **To-do** (`DenTodo`, reusable component): used in the right panel (all projects) and in the project page's To-dos tab (filtered to that project). Both views share one store (localStorage key `den.todos.v1`, plus a `den-todos` window event to keep them in sync).
  - **Add box**:
    - Enter adds, Shift+Enter adds a new line.
    - Option chips appear once you type: project, priority flags (Low / Medium / High), Today / Tomorrow / Next week.
    - Typing shortcuts: `#proj` (prefix match on the project name), `!high` / `!!` / `!1-3`, and a trailing `today` / `tomorrow`.
  - **Paste several lines**: shows a preview card ("Create N to-dos") before adding.
    - Bullets (`-`, `*`, `•`) and numbers are removed.
    - `[x]` lines come in already done, `[ ]` lines open.
    - Any line can be skipped.
  - **Toolbar**:
    - Filter: To do / Done / All, with counts.
    - Sort: Manual (drag), Due date, Priority, Project (grouped with headings), Newest, A→Z.
    - Select-multiple toggle.
    - ⋯ menu: Copy list as text (`- [ ] item` lines), Mark all done, Clear completed.
  - **Row**:
    - 20px round checkbox, ringed in the priority colour; filled `#6FCF97` with a check when done, and the text struck through.
    - Meta line: due date (overdue `#FF8FA2`, today `#FFB547`), priority flag, project dot.
    - On hover: ⋯ (Edit, Priority, Due, Project, Send to Octopus queue, Duplicate) and Delete. Delete shows an Undo toast for 6 seconds.
    - Double-click the text to edit it inline.
  - **Select mode**: square checkboxes, plus a sticky bulk bar at the bottom (All/None, count, Complete, Copy, Delete, Move to project, Done).
- **Smart devices** (`DenConnections` tab="devices", sidebar → Personal assistant):
  - Summary figures.
  - Device cards (Siri & Shortcuts, Alexa, Google Home, HomeKit, Stream Deck, Apple Watch), each with a toggle and Configure / Link.
  - Voice commands table: phrase → action, which devices can trigger it, a CONFIRM tag on risky ones, Test and a toggle. Add your own with phrase + action.
  - Webhooks with Copy, and "Get Apple Shortcut".
  - Voice safety: PIN for approvals, read back before running, quiet hours, wake phrase.
  - Recent device log.
- **Message center** (`DenMessages`): full-screen mode plus a sidebar mode (clicking a chat opens the thread in the right panel), with an Archived filter.
- **Date picker** (`DenDatePicker`, replaces native date inputs):
  - Field: shows "Wed, 7 Oct 2026", a relative hint ("in 3d", hidden below 210px width) and a clear ×.
  - Pop-up (288px wide, `position: fixed`):
    - Month navigation, plus a month-grid mode when you click the title.
    - Monday-first grid of 32px round cells: today has a coral ring, the selected day is filled coral.
    - Quick picks: Today, Tomorrow, Next week, +1 month, Clear.
    - Opens upward when there's no room below. Closes on outside click, scroll or resize.
- **Home queue hover menu**: hovering an Up next or Completed row shows a floating icon bar.
  - **Up next**: Run now, Edit prompt, Duplicate, Move to top, Cancel. The bar sits just left of the priority button.
  - **Completed**: Open chat, Run again / Retry, Copy result, Save as note.
  - On touch devices the bar is not shown; the inline buttons stay instead.
- **Calendar** (right panel):
  - Grid: month navigation with Today, round day cells tinted by event type, up to 3 event dots per day.
  - Day selection: click a day to see only its events; double-click to add one.
  - New ▾ menu: Meeting / Deadline / Reminder / Task.
  - Meeting fields: participants (type or pick from suggestions), duration, link or place, project, notes, "Prep brief" and "Send invites" switches.
  - Repeat options: none, daily, every other day, weekdays, weekly, monthly, or custom (every N days / weeks / months, with weekday picks), ending never or on a date.
  - Repeating events: "Skip this one" and "Delete series".

## Interactions & shortcuts
- ⌘K palette with categories (Tab switches them). Plain-language commands are recognised ("pause everything till 9am").
- ⌘1–⌘5 go to pages; ⌘6 / ⌘7 open the Message center / Calendar panels.
- N focuses the Ask bar, A / D approve or deny, G opens the morning briefing, F toggles Focus, ⇧T toggles the theme.
- `[` or ⌘B toggles the sidebar, ⌘⇧B cycles the right panels, `?` shows the shortcut sheet, Esc closes the top-most layer.
- Hovering a control for about 380ms shows a tooltip with its name and shortcut.
- Animations: `denRise` (opacity plus 8px rise, 0.2–0.4s, cubic-bezier(.2,.7,.3,1)), `denSlideR` (panel slides in from the right, 0.28s), `denPulse` (pulsing status dot), `denShimmer` (skeleton, 1.2s linear).
- Dropdowns cap their height at `100vh - 90px` and scroll.

## State to model
- Queue: tasks with id, title, column, priority, model/lane, a "run after" dependency, prompt and progress.
- Approvals.
- Accounts with session and weekly usage percentages and reset times.
- Projects, plus per-project overview fields, guidelines, ideas, docs, milestones, files and the per-user tab layout.
- Calendar events with a repeat rule `{unit: none|day|week|month, n, days[Mon=0..Sun=6], until}` and a list of skipped dates.
- Models and providers, routing rules, recipes, budget caps, theme, view mode, Home widget order and visibility.

## Design tokens
- **Font**: Bricolage Grotesque (UI), JetBrains Mono (labels, numbers, code).
- **Type**:
  - Mono labels: 10px with 0.12em letter-spacing, uppercase.
  - Body: 13–14.5px; card titles 15–16px at weight 700; page titles 21–24px at weight 750.
- **Backgrounds**: page `#05080C`. Glass cards use `rgba(20,28,38,0.45–0.5)` with a 1px border of `rgba(255,255,255,0.06–0.07)`, `backdrop-filter: blur(20–22px) saturate(150%)`, and a highlight `inset 0 1px 0 rgba(255,255,255,0.05)`. Menus use `rgba(15,21,29,0.95)`.
- **Text**: `#E8EEF3` main, `#DCE4EB` / `#C2CDD8` secondary, `#93A1B0` muted, `#6B7A8A` faint.
- **Accents**: coral `#FF7B5C` (primary; dark text `#1A0A05` on it), amber `#FFB547`, teal `#45E0C8`, red (errors) `#FF5470`, blue `#7FB2FF`, violet `#A58BFF`, green `#6FCF97`. Tints are the accent at 0.08–0.16 opacity, with borders at 0.25–0.45.
- **Usage colours**: under 80% teal, 80%+ amber, 95%+ violet.
- **Radius**: chips 999px; buttons 9–12px; cards 16–22px.
- **Spacing**: gaps 4 / 6 / 8 / 10 / 12 / 14 / 16 / 20px.
- **Touch targets**: at least 36px on desktop and at least 44px on phone.
- **Light theme**: in the prototype it is a quick shortcut (the whole UI is colour-inverted with `filter: invert(1) hue-rotate(180deg)`). For production, define a proper set of light colours.

## Assets
The octopus mascot is an inline SVG (coral dome + 5 tentacles) in the shell file. All icons are inline 24px stroke SVG paths (stroke width 1.7–2.2, round caps). No images are bundled.

## Files
- `Den Home.dc.html` — shell, Home, right panels (notifications, summary, queue, messages, calendar, reminders, notes), ⌘K palette, shortcut sheet, chat panel, page router and skeletons.
- `DenProjects.dc.html`, `DenActivity.dc.html`, `DenInbox.dc.html`, `DenCRM.dc.html` (also the Clients page), `DenNotes.dc.html`, `DenRecipes.dc.html`, `DenUsage.dc.html`, `DenProviders.dc.html` (Providers, Models, Routing), `DenConnections.dc.html` (also the Voice page), `DenSettings.dc.html` (also the Recycle bin).
- `DenTodo.dc.html` — To-do component (right panel + project tab).
- `DenMessages.dc.html` — Message center.
- `DenDatePicker.dc.html` — shared date picker.
- `support.js` — the prototype runtime (only needed to open the files).
- `Den - standalone.html` — the whole prototype in one offline file, to compare against your build.
