# Handoff: Tushar's Workspace (dashboard redesign + Octopus routing)

## Overview
A full redesign of the `claude-queue` dashboard (`dashboard.html`, served by FastAPI at `/dashboard`). It is a single-user ops cockpit where you:
- queue natural-language tasks against coding projects;
- ask **Octopus**, which routes each request to **Claude Code** (acts inside a project) or an **answer-only OpenAI model** (GPT-6 Sol / Luna);
- watch several tasks run at once;
- approve risky Bash commands from your phone;
- manage a per-project backlog (the existing Todos feature).

It fixes nine UX problems of the old dashboard:
1. You couldn't see why the queue had stalled.
2. Blocked tasks were buried.
3. Todos and history felt disconnected.
4. There was no per-project view.
5. Approval requests carried too little context.
6. Costs were invisible.
7. Priority was flat.
8. Common actions took too many steps.
9. Empty states were neglected.

It is **mobile-first**: 375px phone with a bottom tab bar, full-screen panes and 44px tap targets. It is **responsive to desktop** at 900px and up, with a left sidebar and a side pane. There is no horizontal scrolling at any width.

## About the design files
Everything in `reference/` is a **design reference built in HTML**. It is a working prototype that shows the intended look and behaviour, **not production code to paste in**. It runs on a small custom runtime (`support.js`) and fakes all data with a local simulation.

**Your task:** recreate this design inside the existing `claude-queue` app.
- **Backend:** FastAPI plus SQLite (`api.py`, `db.py`, `worker.py`).
- **Frontend:** today, one vanilla-JS `dashboard.html` with no build step.

Keep that no-build approach. Split the page into the static modules described under **Target file structure**.

`src/` holds **production-ready, framework-free building blocks** you can use directly:
- tokens and component CSS;
- the Octopus SVG;
- formatters and status/lane mapping;
- gesture helpers, polish, icons;
- an API client with a polling store, and JSDoc types.

Build the views (render functions) on top of them.

## Fidelity
**High fidelity.** Colours, type, spacing, radii, motion and copy are final. Recreate them exactly. Every value is in `src/css/tokens.css`, and the component classes in `src/css/components.css` already encode each component's spec.

Open `reference/Workspace Prototype.dc.html` in a browser; keep `support.js` next to it. It runs as the phone frame by default. Its **Tweaks** panel switches:
- `scenario`: waiting / running / rate_limited / idle / empty
- `openaiKey`: on/off, which shows the "unavailable" and error states
- `frame`: phone / responsive; responsive at ≥900px shows the desktop layout
- `voice`: playful / plain Octopus copy

---

## Target file structure (recommended)
```
claude-queue/
  api.py                  + app.mount("/static", StaticFiles(directory="static"))  + new /octopus routes
  dashboard.html          shell only: <div id="app"> + <link> tokens/components + <script type="module" src="/static/js/app.js">
  static/
    css/tokens.css        ← src/css/tokens.css   (unchanged)
    css/components.css    ← src/css/components.css
    js/api.js             ← src/js/api.js        (client, 5 s polling store, selectors)
    js/format.js          ← src/js/format.js     (time/money/status/lane/greeting helpers)
    js/octopus.js         ← src/js/octopus.js    (character SVG, 6 moods)
    js/gestures.js        ← src/js/gestures.js   (3D page swipe, swipe-to-approve, drag reorder, double tap, autogrow)
    js/polish.js          ← src/js/polish.js
    js/icons.js           ← src/js/icons.js
    js/types.js           ← src/js/types.js      (JSDoc typedefs)
    js/views/             write these:
      shell.js            topbar, sidebar (desk), tab bar + FAB (phone), pager, toast, density
      splash.js
      home.js             hero, ask box, rollups, needs-you, running now, rate-limit card, up next, scheduled, usage, models, recent
      projects.js         quick filters, toolbar + menus + search, expandable list
      project.js          header, stats, tabs (work / activity / recipes / settings), backlog
      recipes.js          schedules + recipe cards
      activity.js         filter chips + grouped history
      chat.js             chat pane
      approval.js         approval detail pane
      sheets.js           new-task palette, usage, add project, shortcuts
    js/app.js             router (hash: #/home, #/projects, #/project/12, #/recipes, #/activity), render loop, keyboard shortcuts
```
Render with template strings and `innerHTML` per region, plus event delegation (`data-action="…"` on buttons). Use `escapeHTML()` from `format.js` for every user or model string. Keep each view a pure `render(state) → html` plus a `bind(root)` for gestures.

---

## Global layout

### Phone (<900px, or `frame=phone`)
- **Structure:** A full-height column holds the sticky **topbar**, then the scrolling **page** (inside a `.pager` for swiping), then the absolutely positioned **tab bar**.
- **Page padding:** `4px 14px calc(120px + safe-area-bottom)`, so the tab bar never covers content.
- **Panes:** Chat and approval panes cover the full screen with a back arrow (`←`).
- **Sheets:** The new-task panel, usage, add project and shortcuts are bottom sheets. They use a 26px top radius, a max height of `100% − safe-top − 24px` and a scrim `rgba(3,5,8,.62)` with a 4px blur.

### Desktop (≥900px and `frame=responsive`)
- **Sidebar:** 250px on the left, bg `--bg-sidebar`, 1px right border `--surface-2`. It contains:
  - brand: 42px Octopus, then "TUSHAR'S" as a 10.5px mono label above "Workspace" in 20px 800;
  - a status pill (pulsing dot plus a short status);
  - nav items (Home 1 / Projects 2 / Recipes & schedules 3 / Activity 4) with key hints;
  - a **PROJECTS** list: General pinned first, a swatch, the name, and a right-hand meta such as running / needs you / asleep / failed / N queued / paused;
  - "+ Add project";
  - at the bottom, the coral **New task ⌘K** button and "Keyboard shortcuts · ?".
- **Main:** padding `8px 28px 48px`; the Home grid is two columns (`repeat(auto-fit, minmax(min(100%,340px),1fr))`, gap `22px 26px`, max 1180px).
- **Pane:** 460px on the right. At ≥1280px it sits inline and pushes content; between 900 and 1279px it overlays with a `--sh-pane` shadow.
- **Sheets:** They become a centred 640px modal 10vh from the top with a 22px radius.

### Topbar (sticky)
- **Background:** `rgba(11,16,22,.88)` with a 14px backdrop blur.
- **Padding:** phone `calc(10px + safe-top) 16px 10px`; desktop `16px 28px`.
- **Contents, left to right:**
  - back button (44px, only on project detail on phone);
  - title block: an eyebrow on phone ("TUSHAR'S" on Home, "WORKSPACE" elsewhere; 10.5px mono `--text-meta`, letter-spacing .06em), then the title (20px phone / 22px desktop, 800, letter-spacing −.02em, ellipsis);
  - a flex spacer;
  - **live** indicator (6px teal pulsing dot plus "live", 11px mono). Hide it on the phone's project detail page;
  - **usage pill**: two 40×4px bars (session % in the session colour, week % in `--text-meta`) and today's spend in 12px mono. It opens the Usage sheet;
  - **density toggle**: a 40px icon button. When compact, it gets a coral tint, a coral border and coral icon;
  - on desktop, "+ New task ⌘K".

### Tab bar (phone)
- **Placement:** absolute bottom. Background `linear-gradient(transparent, --bg 36%)`, padding `14px 8px calc(10px + safe-bottom)`.
- **Order:** Home, Projects, **FAB**, Recipes, Activity.
- **Tabs:** 64×48 each, a 22px outline icon (`icons.js`: home, projects, recipes, activity) above an 11.5px 600 label, gap 3px. Inactive colour is `#8593A3`. Active: the label is `--text` and the icon stroke `--coral`.
- **Badge:** Home carries the "needs you" count (amber, 18px, 2px `--bg` ring, top −6px, right 8px).
- **FAB:** 56px, radius 19px, coral, "+" in 30px, `--sh-fab`. It opens the New task sheet.

### Density (compact layout)
The toggle sets `data-density="compact"` on `.app`, persisted in `localStorage['octopus-compact']`. Compact changes:
- **Rows and gaps:** rows go from 56 to 44, section gap from 20 to 12, card padding from 14 to 11.
- **Hero:** the status line goes from 23 to 19px, the Octopus from 76 to 52px, and the sub-line is hidden.
- **Hidden:** all grey meta lines and the risk chips.
- **Rollups:** 46px tall with 16px numbers.
- **Project rows:** 50px, with a 28px avatar.

It shows a toast: "Compact layout on" or "Comfortable layout on".

---

## Screens

### 1. Splash / welcome (on every load)
- **Layout:** full screen `--bg`, content centred with a 20px gap:
  - dancing Octopus, 124px (`octopusSVG('dance')`);
  - copy block, max 380px, centred;
  - loading bar: a 120×3px track `--line-soft` with a coral 40% segment, `load-bar 1.1s` infinite.
- **Greeting:** `greeting()` in `format.js`.
  - **Hello line:** 31px 800, letter-spacing −.03em, for example "Good morning, **Tushar!**" with the name in coral.
  - **Time of day:** 5–12 morning, 12–17 afternoon, 17–22 evening, otherwise night ("Up late, Tushar?").
  - **Friendly line:** 16px 500 `--text-3`, one of three per time of day, picked at random. In plain voice it reads "Your workspace is loading."
  - **Recap pill:** 12px mono, `--surface`, 1px `--line`. It reads "while you were away: 3 done · 2 need you" (amber when anything needs you, otherwise green) or "first time here? let's set you up".
  - **Status line:** "stretching tentacles…" in 11.5px mono `--text-faint`.
- **Entrance:** hello `rise .55s` at .15s delay, line at .4s, pill `fadein` at .75s, status at .9s.
- **Hint:** at the bottom (36px plus safe area) "Double-tap anywhere to dive in" ("Double-click" on desktop), 13px 600 `--text-meta`. It fades in at 1.4s, then pulses.
- **Dismiss:** only by a **double tap or double click**, two taps within 380ms (`onDoubleTap`). The splash then plays `splash-out .4s`, the skeleton clears and Home reveals. Keep the splash up while data loads; the animation loops until dismissed. Use `touch-action: manipulation` so iOS doesn't zoom.

### 2. Home (Chats tab in the old app)
Sections top to bottom. On phone they form one column; on desktop the left column is sections 1–7 and the right column 8–11. Each section enters with `rise .4s var(--ease-out)`, delays 0 / 30 / 60 / 120 / 150 / 180 / 210 / 240ms.

**2.1 Hero: "Octopus says"**
- **Layout:** grid `76px 1fr`, gap 12. It holds the Octopus in the current mood (`moodFor()`), then:
  - **label:** 11px mono 600, status colour, letter-spacing .06em, with a pulsing 7px dot;
  - **status line:** 23px 700;
  - **sub-line:** 13.5px `--text-muted`;
  - **actions:** 44px buttons.
- **States (playful copy, plain in brackets):**

| State | Label | Line | Sub | Action |
|---|---|---|---|---|
| approval pending | `WAITING ON YOU · 12m` amber | "I'm holding the queue until you answer." (Queue paused: approval needed.) | "{task} ({project}) needs a yes before I run deploy.sh. N tasks waiting behind it." | **Review request** (amber) |
| rate-limited | `RATE-LIMITED · RESUMES 14:20` violet | "Out of juice. Napping until 14:20." | "Hit the 5-hour session limit partway through "{task}". I'll resume it on my own in H:MM:SS, then run N more tasks." | Ping me when back / Will ping you ✓ |
| agent running | `WORKING · m:ss` teal | "On it: {task, lower-case first letter}." | "{current step} in {project}. N tasks queued after this." | Watch |
| queue held (all paused) | `IDLE · QUEUE HELD` grey | "Nothing I'm allowed to touch." | "The N tasks left belong to paused projects…" | Open {project} |
| idle | `IDLE` green | "All clear. Tentacles free." | "Next scheduled: Run tests at 18:00." | New task (coral) · Morning status check |
| no projects | `NO PROJECTS` | "Give me something to hold." | → Onboarding (screen 11) | |

**2.2 Ask Octopus box (new)**
- **Wrapper:** `position:relative; z-index:20`. This is required so its menus paint above the animated sections below it.
- **Box:** `--surface`, 1px `--line-strong` border, radius 18, padding `8px 8px 8px 14px`. It contains:
  - **Textarea:** placeholder "🐙 Ask Octopus anything…", 16px 500. It grows with the text up to 180px (`autoGrow`). Enter sends; Shift+Enter adds a new line.
  - **Image thumbnails** (when attached): 56px squares with a 24px ✕.
  - **Controls row:**
    - **Model picker:** 36px, `--inset`, radius 10. A dot and "Auto" or the model name, then a chevron.
    - **Attach image:** a 36px icon; `accept="image/*"` with multiple files, which opens the camera or library on a phone.
    - **Polish text:** a 36px sparkle icon (see Polish).
    - **Target label:** flex, 12px mono `--text-meta`, ellipsis. It reads "in {project} ▾" or "no project (General) ▾" and opens the target menu.
    - **Send:** a 40px coral square with "↑", radius 13.
- **Model menu:** popover 320px, header "MODEL". Rows are 48px: a dot, the label, a status sub-line in 11px mono, and a ✓ on the current pick.
  - First row: "Auto - Octopus decides" / "picks a model for each request".
  - Then every model from `/octopus/models`. Unavailable ones stay listed at 55% opacity with " (unavailable)" or " (rate limited)" after the name.
- **Target menu:** 240px, header "SEND TO": "No project (General)", then projects with a swatch.
- **On desktop:** default the target to the project selected in the sidebar.
- **States:**
  - **Sending (1–3s):** the textarea, picker and target are disabled and the box drops to 70% opacity. Send shows a 16px spinner. Below the box: a 22px dancing Octopus and "Routing… picking a model" (12px mono `--text-meta`).
  - **Success:** one line, 12.5px mono `--text-muted`: `→ **GPT-6 Luna** · simple: short factual question · open #123`. The model name is 700 and lane-coloured (agent `--lane-agent-fg`, api `--lane-api-fg`). "open #123" is a coral text button that opens the chat. The input clears.
  - **Error:** the same line in `--err-soft`: "Could not send: {detail}". The box border turns `rgba(255,84,112,.5)`. Typing clears it.
- **API:** `POST /octopus {text, project_id|null, model:'auto'|id, images?}`.

**2.3 Rollups**
- **Layout:** four cells in a grid with 1px gaps on `--line-soft`, outer radius 16. Each cell: `--surface`, padding `11px 12px`, min height 62, hover `--surface-2`.
- **Cells:** a 20px 700 mono number in its colour, over an 11.5px label.
  - "Total tasks" (`--text`) → Activity, all.
  - "In queue" (`--queued`) → Projects with the In queue filter.
  - "Succeeded" (`--ok`) → Activity, Done.
  - "Failed" (`--err`) → Activity, Failed.
- **Source:** `/queue/status.counts`.

**2.4 Needs you** (only when there are items)
- **Header:** "Needs you" plus an amber count badge.
- **Approval card:** radius 20, `--wait-card`, 1px `--wait-line`.
  - Head row (12px mono): "APPROVE" amber · project swatch and name · **model label** · time ago on the right.
  - Title: 18px 700.
  - Command block: `--inset-deep`, radius 10, "$ ./scripts/deploy.sh --env staging", with "in ~/code/api-gateway" below in 11.5px.
  - Risk chips (hidden in compact): "Medium risk" amber tint · "network" · "rollback exists" green tint · "+48 −12 · 3 files".
  - Buttons: grid `auto 1fr 1.5fr`, 46px, radius 14. **Details** (ghost), **Deny** (ghost), **Approve** (amber) with a hint "or swipe →" on phone or "A" on desktop.
  - **Swipe** the card right past 110px to approve, left past 110px to deny (`swipeDecision`). While dragging, the card translates with a 3D tilt and the layer behind shows amber "✓ Approve" or red "Deny ✕".
- **Failed card:** `--err-card`, 1px `--err-line`, radius 18.
  - "FAILED" in red, then a 16px 600 title and the error message in 12px mono `--err-soft`.
  - Buttons: **Retry** (`--err-btn`, queues at the front), **Open chat**, **Dismiss** (text, right).
- **Review card:** `--surface`, `--line-strong`.
  - "REVIEW" in blue, then the title and the review note in 13.5px `--text-muted`.
  - Buttons: **Open · +210 −144**, **Looks good** (`--review-btn`).

**2.5 Running now** (replaces the old single "Octopus is on" panel)
- **Header:** "Running now" plus a count.
- **List:** `--run-card`, 1px `rgba(69,224,200,.28)`, radius 18. It shows 0–n rows, Claude Code first.
- **Row** (a button that opens the chat): grid `16px 1fr`, padding `card-pad 14px`.
  - A 14px spinner: teal for agent, green for api.
  - Title: 14.5px 600, ellipsis.
  - Meta row (11.5px mono): project · **model label** · "started 2m ago".
  - For agent tasks only, the current step: "› Running npm test" in 12px mono `--run-soft`. Hidden in compact.
- **Empty:** a dashed box reading "Nothing running".

**2.6 Rate-limited card** (when an agent task is `rate_limited`)
- **Card:** `--sleep-card`, 1px `--sleep-line`.
- **Content:**
  - "RATE-LIMITED · project";
  - the title;
  - a live countdown `H:MM:SS` in 30px mono, with "until 14:20 · paused at step 3/6";
  - a full violet 6px bar;
  - the explanation "5-hour session window is full. I'll resume this exact session automatically, so you don't need to do anything.";
  - buttons **Retry now** (`--sleep-btn`) and **Open chat**.

**2.7 Up next**
- **Header:** "Up next" plus a count. The note on the right is "held · waiting on you" (amber), "held · until 14:20" (violet) or "drag to reorder".
- **Rows:** 56px, grid `30px 8px 1fr auto`.
  - Drag handle "⋮⋮" (44px hit area, `touch-action:none`).
  - 8px project swatch.
  - Title (14.5px).
  - Meta: project · **model label** · priority, plus "· reply" or "· skipped, project paused" when they apply.
  - **↑** (move to top) and **✕** (cancel; toast "Cancelled" with Undo), 44px each.
- **Paused rows:** tasks in paused projects render at 45% opacity.
- **Drag:** `dragReorder`. The lifted row gets `--surface-drag`, `--sh-drag` and radius 14; the other rows shift ±row height.
- **Empty:** "Queue's empty." with a **Queue something** button.

**2.8 Scheduled:** the next 3 enabled schedules. Each row shows the time (13px mono violet, 52px column), the label, "{cadence} · {target} · next {when}" and a violet toggle. The header links "Manage" → Recipes.

**2.9 Usage:**
- **Session window:** "Session window 62%" with a 6px bar in the session colour (teal <80, amber ≥80, violet ≥95) and "resets 14:20".
- **This week:** 38% with a grey bar and "resets Mon".
- **Spend:** today's API spend in 22px mono with "API spend today · $X of $100 this month", plus a 7-day mini bar chart (today coral).

**2.10 Models · last 30 days (new)**
- **Rows:** grid `8px 1fr auto`.
  - **Status dot:** 8px with a 3px halo. Available is green, rate-limited violet, unavailable or disabled grey.
  - **Name:** 14px 600; grey when off.
  - **Usage, right-aligned:** 11.5px mono, "64 tasks · 97% ok · ~$0.09". Use "<$0.01" for tiny amounts and "unused" in `--text-faint`.
  - **Status text:** always on its **second line** (`grid-column: 2/4`, wraps, never truncated), in 11.5px mono `--text-meta`, violet when rate-limited. It reads "agent · runs in the project", "api · answers only", "rate limited · back in 5m", or the reason ("OPENAI_API_KEY is not set", "disabled in models.json").
- **Source:** `/octopus/models` plus `/octopus/usage`, through `modelState()` and `usageSummary()`.

**2.11 Recent:** the last 4 done or cancelled tasks.
- **Row:** glyph (✓ green, – grey, ✕ red, ◐ blue), title, meta "project · **model label** · 2h · 4:12 · $0.42" (api tasks show "2.1s" and "<$0.01"), and **↻** Run again.
- **Header link:** "All activity".

### 3. Projects
- **Quick filter chips** (horizontal scroll, `data-noswipe`): All · Pending · Ongoing · In queue · Failed · Done.
  - Each chip: 40px pill, a coloured dot (not on All), the label and a mono count.
  - Selected chip: white fill, dark text.
  - Choosing a filter hides projects with no matching tasks and auto-expands the rest to show only the matching tasks.
- **Toolbar**, 44px controls:
  - **Sort:** icon plus the current label; "Sort" when it's the default. It opens a menu "SORT BY": Most active (default), Recently used, Most queued, Name A–Z.
  - **Filter:** icon plus "Filter", with a coral count badge when not default. It opens "SHOW" with checkbox rows: Show paused projects (on), Only projects with tasks (off).
  - **Search:** an icon button that toggles a 48px search field below the toolbar. It autofocuses, reads "Search projects and tasks", has ✕ to close, and matches project name, path and task titles. A title match auto-expands that project.
  - **+ Add** on the right, coral; it opens the Add project sheet.
- **List:** one card with rows separated by 1px `--line-soft`. **General is always pinned first.**
  - **Main button** (opens project detail): 38px avatar (radius 12, project colour, initial in 18px 800; General uses a 22px Octopus on a `--surface-2` tile), the name (16px 700), and the status in 12px mono. Status reads "2 need you" amber · "running now" teal · "3 queued" · "quiet" · "paused" grey · for General "answers only · pinned".
  - **Expand button:** 48×44, the count of items plus a chevron that rotates 180°. Expanded, it lists that project's active and queued tasks as **titles only**: a 6px status dot and the title, 44px rows, each opening the task's chat or approval. Empty text: "Nothing in the queue." or "No failed tasks."
  - **No spend or amount** on these rows.
  - Rows enter staggered: `rise .35s`, 45ms each, capped at 8 rows.
- **Empty (filters):** "No projects match." with **Clear filters**.

### 4. Project detail
- **Header:**
  - 48px avatar;
  - name (26px 800) and path (12.5px mono);
  - **Pause/Resume** (secondary) and **+ Task** (coral, opens the palette preset to this project).
- **Paused banner:** a dashed box with a sleeping Octopus: "Paused. Queued tasks for this project are skipped but kept. Resume and I'll pick them up in order."
- **Stats:** three cells, "$18.40 spent this month", "4 done this week", "2 in the queue".
- **Tabs:** a segmented bar, Work · Activity · Recipes · Settings.
- **Work tab**, two columns on desktop:
  - **Live:** cards for running, approval, error, review and rate-limited tasks, each with a coloured label.
  - **In the queue:** "#N" global position, the title, the **model label** and the priority. The note reads "global position".
  - **Done recently:** history rows.
  - **Backlog** (= Todos API):
    - an "Add to backlog…" input (Enter adds);
    - 56px draggable rows with a 24px checkbox (coral when checked);
    - a "Select all / Clear" link;
    - a sticky bulk bar "N selected · joins after N tasks" with **Delete** and **Run selected**;
    - the hint "Drag to set the order. Selected items join the queue in this order."
- **Activity tab:** a timeline of this project's tasks (time, glyph, title, "status · cost · duration").
- **Recipes tab:** recipe cards with **Run here** and **Schedule**.
- **Settings tab:**
  - default priority (Low / Normal / Urgent);
  - notification toggles:
    - Approvals: "Urgent push that breaks through Focus";
    - Failures: "Push right away";
    - Completions: "Quiet push, bundled if several finish together";
    - Quiet hours 23:00 to 08:00: "Hold everything except approvals";
  - "Always allowed commands" chips with ✕;
  - monthly budget stepper with a bar and "At 100% I'll pause this project and ask before continuing."

### 5. Recipes & schedules
- **Intro:** "Recipes are saved prompts. Run one in two taps from +, or put it on a schedule. If the Mac is asleep when a schedule is due, it runs as soon as it wakes."
- **Schedules list:** rows with toggles.
- **Recipe cards grid:** name, schedule tag (violet mono), prompt, target, and **Run now** / **Schedule** buttons.

### 6. Activity
- **Filter chips:** All / Done / Failed / Review / Cancelled.
- **Groups:** TODAY / YESTERDAY / EARLIER, each with history rows including the model label. Groups enter staggered 80ms each.

### 7. Chat pane
- **Header:** ← or ✕, the title (16px 700) and a sub-line of swatch · project · **model name** (lane-coloured) · status (status colour), for example "General · GPT-6 Luna · Done". The sub-line is a single line with ellipsis.
- **Messages:**
  - **You:** right-aligned, max 84%, `--you-bubble`, 1px coral 30% border, radius `18 18 4 18`. Attached images render as 104px tiles above the bubble.
  - **Reply** (Octopus or model):
    - 30px Octopus avatar and bubble: radius `4 18 18 18`, `--surface`, **15px/1.62**, `white-space: pre-wrap`, max 62ch, for comfortable long answers.
    - **Footer** under every reply: a status badge (for example "Done", 11px mono on `--surface-2` in the status colour), then "2m ago · GPT-6 Luna · 2.1s · <$0.01". Drop any value that is missing.
    - **First reply only:** an italic 12px `--text-faint` note, "Routed: simple: brief factual definition" or "Routed: you picked GPT-6 Sol".
  - **Tool log** (agent): an inset box of mono lines, "✓ Read 6 files…", with the current line showing an 11px teal spinner.
  - **Inline approval** (agent): an amber mini card with Details / Deny / Approve.
  - **System pills:** centred, for example "You approved · resuming".
  - **Typing indicator** while running: a 30px dancing Octopus, a bubble with three bouncing dots, and "Octopus is on it" (agent) or "GPT-6 Sol is writing…" (api).
- **Composer** (sticky, gradient background):
  - **Quick-action chips** (Status, Run tests, Deploy, Pull latest, plus contextual Retry / Looks good / Run again / Open a PR / Stop) appear **only for Claude Code chats**. Hide the whole row for answer-only chats.
  - **Box:** attach image, Polish text, a textarea (16px, max 120px) and a coral send button (40px).
  - **Placeholder:** "Reply to GPT-6 Luna…" (api), "Reply to Claude Code…" (agent, idle), "Add a note for when this turn ends…" (agent, active).
  - **Replies always stay on the chat's model**, so there is no picker. Send with `thread_id` (existing API).

### 8. Approval detail pane
- **Risk card** (amber): "RISK" with "waiting 12m"; "Medium" in 26px 800 amber with a 3-segment meter; reason bullets (amber = concern, green = mitigating).
- **Command block:** plus facts: cwd, env, "task · step 4 of 6".
- **What I was doing:** step list (✓ done, › current in amber, pending greyed).
- **Changes so far · 3 files +48 −12:** file rows with +/− counts and a diff excerpt (add, del and hunk colours from the tokens).
- **Sticky actions:**
  - **Approve once** (52px amber, 800);
  - **Always allow `deploy.sh` in api-gateway**;
  - a "Deny with a note (optional)" input with **Deny** (`--err-btn`);
  - on desktop, the hint "A approve · D deny · Esc close".

### 9. New-task sheet (FAB / ⌘K / N)
- **Input:** a 34px Octopus (it dances while routing) beside a 19px 600 textarea, "What should Octopus do?".
- **Image thumbnails.**
- **Chip rows:**
  - **projects** (General first; paused ones at 60%);
  - **model** (Auto · Claude Code · GPT-6 Sol · GPT-6 Luna · GPT-6 Nova at 50% when unavailable);
  - **priority** (Low / Normal / Urgent; Urgent selected = coral);
  - **when** (Now / Tonight 18:00 / Tomorrow 09:00 / Every day 09:00 / Weekdays 18:00; scheduled selection = violet).
- **Lists:** "RECIPES · TAP TO QUEUE" (the prompt filters them) and "RECENT PROMPTS".
- **Footer:**
  - attach image and Polish (44px boxed icons);
  - an **ETA line** (12px mono), one of:
    - "Waits behind Deploy to staging (needs you) + 2 more";
    - "Starts after the limit resets at 14:20";
    - "#3 in line";
    - "Starts right away";
    - "{project} is paused. It queues but won't run until you resume.";
    - "GPT-6 Sol answers right away · no queue" (green, when an api model is picked);
    - "Could not send: …" (red);
  - the CTA **Queue ↵** or **Schedule ↵**, which shows a spinner and "Routing…" while sending.

### 10. Other sheets
- **Usage:**
  - two stat cards (Claude session 5h %, weekly limit %);
  - an API spend bar chart, 7 days labelled T F S S M T W;
  - by-project bars;
  - a "Save the session for urgent work" toggle ("Hold Low-priority tasks when the session is over 85%.").
- **Add project:** "Point Octopus at a folder on your Mac. Its tasks run one at a time from the shared queue." Fields Name and Path (mono), and an **Add project** button that shows a spinner and "Adding…".
- **Shortcuts:** ⌘K / N new task · A approve · D deny · 1–4 switch views · Esc close · ? this list.

### 11. Onboarding (no projects)
- **Header:** a 150px sleeping Octopus, "Give me something to hold." in 34px 800, and "Register a project folder. Queue tasks from your phone, the menu bar, or here, and I'll run Claude Code against it one task at a time while you're away."
- **Step cards:**
  - 1: Register a project (name, path, Add project);
  - 2: Queue a first task (recipe chips), at 60% opacity;
  - 3: Get pinged ("ntfy topic · tushar-octopus", Send test push), at 60% opacity.
- **General** still exists here.

### Toast
- **Placement:** bottom-centre, above the tab bar (96px plus safe area; 24px on desktop).
- **Style:** `--text` background, dark text, 13.5px 600, radius 14, `--sh-toast`.
- **Behaviour:** an optional action button in `#C4452A` (Undo) and auto-hide after 4s.
- **Messages used:**
  - "Approved · resuming {task}"
  - "Denied · Octopus will wrap up without it"
  - "Retry queued at the front"
  - "Moved to the top"
  - "Cancelled" (with Undo)
  - "Queued · {project} · #N in line"
  - "→ GPT-6 Luna · answering · #N"
  - "Answered · GPT-6 Sol · #N"
  - "Done · {task} · $0.42"
  - "Text polished" (with Undo)
  - "Already reads well"
  - "Compact layout on"
  - "Added {name} · ready when you are"

---

## Model label (lane chip)
Put it on every task card: needs-you, running now, up next, recent, activity, project lists, and the chat header (as text).

| Lane | Style | Meaning |
|---|---|---|
| `agent` (Claude Code) | **squared** (radius 5), coral tint fill, coral 38% border, text `#FFA48C`, glyph `›_` | acts on your project: edits files, runs commands |
| `api` (GPT-6 Sol/Luna/…) | **pill** (999), transparent, green 50% border, text `#8FE0B0`, glyph `“` | answers only |

- **Size:** 18px tall, 10.5px 600 mono, padding 0 6px.
- **Why three cues:** shape, colour and glyph together keep the lanes distinguishable in greyscale and for colour-blind users.
- **Implementation:** `modelChip()` plus `modelChipHTML()` in `format.js`, styled by `.model-chip--agent` and `.model-chip--api`.

## Interactions & behaviour
- **Polling:** every 5s (`startPolling`), paused while the tab is hidden. Countdowns (rate-limit, "started Xm ago", elapsed) tick every 1s locally from `resume_at` / `started_at`.
- **Loading:**
  - **Skeletons:** shown for about 520ms after every view change, and on first load until the splash is dismissed. Home skeleton: hero circle plus 4 lines, a 62px bar, a 150px card and 5 rows. List skeleton: 5 chip pills, a toolbar and 5 avatar rows. Use `.sk` with `shimmer 1.3s`.
  - **Staggered reveal:** follows the skeleton (see the section delays above).
  - **Button spinners:** 16px spinners on Approve ("Approving…"), Retry ("Retrying…"), Add project ("Adding…"), Queue ("Routing…"), Ask send and Polish. **In production, show them only while the real request is in flight.** The prototype fakes 650–700ms.
- **Page swipe (phone):** `pageSwipe` on `.pager`.
  - Swiping left or right moves through Home ↔ Projects ↔ Recipes ↔ Activity; on project detail, a right swipe goes back.
  - The axis locks when |dx| > |dy|×1.3 after 10px; commit at 70px; edge resistance ×0.25.
  - **3D:** `perspective(1100px) translateX(x) rotateY(r·34°) scale(1−|r|·.12)`, opacity `1−min(.6,|r|·.7)`, transform-origin on the trailing edge.
  - The out-fling takes 230ms. The new page then enters from the opposite side (−dir·W·.7), transitioning `.26s var(--ease-out)`.
  - Ignored when the gesture starts on: `[data-noswipe]` (chip rows, project tabs), inputs, drag handles, the approval card.
  - Clicks are suppressed for 80ms after a swipe.
- **Keyboard (desktop):** ⌘K or N opens New task · A / D approve or deny the waiting request · 1–4 switch views · Esc closes the sheet, then the pane · ? opens shortcuts.
- **Polish text:** `polish.js`. It's disabled when the field is empty (40% opacity). The icon becomes a spinner while running, then the text is replaced in place with an Undo toast.
- **Images:** attached images show as thumbnails with remove; sent images render in the you-bubble. **A backend is needed** (see gaps).
- **Reduced motion:** `tokens.css` disables animations under `prefers-reduced-motion`.

## State management
`api.js` exports `store.data` (server snapshot) and `store.ui`: view, projectId, projectTab, density, skeleton, pane, sheet, toast, projectsList{quick, sort, q, searchOpen, menu, showPaused, onlyActive, open}, ask{text, model, projectId, images, sending, result, menu}, activityFilter, booted.
- **Derived selectors:** `uiStatus` (a task with a pending approval counts as `approval`), `running` (Claude Code first), `needsYou`, `queued` (priority DESC, created ASC, matching the worker), `rollups`.
- **Optimistic updates:** approve, deny, cancel, reorder and toggles update `store.data` immediately, then reconcile on the next poll.

## Backend
Existing endpoints map directly:
- Projects: `GET/POST /projects`, `PATCH /projects/{id}` (Pause/Resume).
- Tasks: `GET /tasks`, `GET/POST /projects/{id}/tasks` (chat replies via `thread_id`), `POST /tasks/{id}/cancel`.
- Queue: `GET /queue/status` (rollups).
- Approvals: `GET /approvals` plus `POST /approvals/{id}/decide`.
- Todos (**Backlog** in the UI): `GET/POST /projects/{id}/todos`, `PATCH/DELETE /todos/{id}`, `POST …/reorder`, `POST …/run` (Run selected).

**New, required for this design:**

| Endpoint | Purpose |
|---|---|
| `POST /octopus` `{text, project_id\|null, model}` → `{task, decision:{model_id, model_label, lane, category, reason}}` | Ask box and New task routing |
| `GET /octopus/models` → `[{id,label,lane,enabled,available,unavailable_reason,rate_limited_until}]` | pickers, Models panel |
| `GET /octopus/usage` → `{[model_id]:{tasks,done,failed,avg_duration_ms,cost_usd}}` (30 days) | Models panel |
| `tasks` columns `lane, model_id, category, route_reason, duration_ms, input_tokens, output_tokens, cost_usd` | model labels, reply footers, routed note |
| built-in **General** project (`is_general`, no path) | requests with no project; pin first |

**Backend gaps (in the prototype, not in the current API).** Build these, or hide the UI until they exist:
1. **Queue reordering and move-to-top:** today order is only priority plus created_at. Add `POST /queue/reorder {ids}` or a `position` column.
2. **Schedules and recipes:** the Recipes page, "Scheduled" on Home, and "when" in New task.
3. **Usage/limits:** session % and weekly %, daily spend, per-project spend and monthly budget.
4. **Per-project settings:** default priority, notification toggles, the always-allowed command list (the "Always allow" button on approvals).
5. **Approval context:**
   - risk level and reasons;
   - the step list;
   - the diff so far.

   Derive these from `tool_input`, the task log and `git diff`, or omit the sections.
6. **Image attachments:** upload endpoint plus passing them to the model.
7. **Polish:** `POST /octopus/polish {text}` → `{text}`, using a cheap model with `POLISH_SYSTEM_PROMPT`. Without it, `polishLocal` works offline.
8. **Dismissing a failed card:** client-side (store the id in localStorage), or a `dismissed` flag.
9. **Running task progress** ("current step", step bar): needs a streamed log or last-tool line from `worker.py`.

## Design tokens
All values are in `src/css/tokens.css`. The key ones:
- **Background and surfaces:** background `#0B1016`, page `#06090D`, surfaces `#121922` / `#18212C` / `#223040`, borders `#1A2330` / `#223040` / `#2A3747`.
- **Text:** `#E8EEF3` / `#C2CDD8` / `#A3B0BE` / `#93A1B0` / `#6B7A8A`.
- **Brand coral:** `#FF7B5C` (hover `#FFA48C`, text on coral `#1A0A05`).
- **Status:**
  - waiting `#FFB547`
  - running `#45E0C8`
  - failed `#FF5470`
  - review `#7FB2FF`
  - rate-limited `#A58BFF`
  - done / api `#6FCF97`
  - queued `#C2CDD8`
- **Type:** Bricolage Grotesque 400–800 (UI) and JetBrains Mono 400–600 (numbers, meta, code), from Google Fonts. Inputs are always 16px.
- **Radii:** 5 · 8 · 10 · 12 · 14 · 16 · 18 · 20 · 26 · 999.
- **Motion:** `cubic-bezier(.2,.7,.3,1)`; .15s / .2s / .26s / .4s.

## Assets
- **Octopus:** code-drawn SVG (`src/js/octopus.js`), six moods, all animation in CSS keyframes (`tokens.css`). No image files.
- **Icons:** inline SVG (`src/js/icons.js`).
- **Emoji:** only 🐙 in the Ask placeholder.
- **Fonts:** Google Fonts, imported at the top of `tokens.css`. Self-host them if the Mac serves the dashboard offline over Tailscale.

## Files in this bundle
- `README.md`: this spec.
- `reference/Workspace Prototype.dc.html` plus `reference/support.js`: the clickable hi-fi prototype, which opens directly in a browser. Its markup has every exact inline style; its `class Component` logic has the simulation, copy and state machine.
- `src/css/tokens.css`, `src/css/components.css`: production CSS.
- `src/js/api.js`, `format.js`, `octopus.js`, `gestures.js`, `polish.js`, `icons.js`, `types.js`: production helpers (ES modules, no build).
