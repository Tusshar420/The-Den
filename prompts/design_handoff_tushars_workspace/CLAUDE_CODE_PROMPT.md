# Prompt for Claude Code

Copy everything below the line into Claude Code, run from the root of the `claude-queue` repo. First put the `design_handoff_tushars_workspace/` folder in the repo root.

---

You are implementing a finished, high-fidelity redesign of this repo's dashboard. The complete spec and reusable code are in `design_handoff_tushars_workspace/`. Implement it **exactly as designed**: same layout, colours, type, spacing, copy, states and animations. Don't redesign, simplify or "improve" anything.

## Read first, in this order
1. `design_handoff_tushars_workspace/README.md`: the full spec. It is the source of truth.
2. `design_handoff_tushars_workspace/reference/Workspace Prototype.dc.html`: the clickable prototype. Its markup has every exact inline style; its `class Component` logic has all copy, states and edge cases. When the README is ambiguous, match the prototype. Open it in a browser alongside `reference/support.js` to see it working; use the Tweaks panel to switch scenarios.
3. `design_handoff_tushars_workspace/src/`: production-ready CSS and JS. **Use these files as-is.** Don't rewrite them.
4. The current app: `api.py`, `db.py`, `worker.py`, `dashboard.html`, `README.md`.

## Constraints
- Keep the current stack: FastAPI, SQLite, and a vanilla-JS dashboard with **no build step, no framework, no npm**. Use native ES modules.
- Keep every existing feature and endpoint working: projects, tasks, chat threads via `thread_id`, approvals, todos (called **Backlog** in the new UI), cancel, pause/resume, the API-key prompt with its localStorage key, and 5-second refresh.
- Mobile-first: 375px phone first, then desktop at 900px and up. No horizontal scrolling at any width. Tap targets are at least 44px and inputs are 16px.
- Escape every user or model string with `escapeHTML()` from `format.js`.
- Respect `prefers-reduced-motion`; `tokens.css` already handles it.

## Build plan: work in this order and commit after each step
1. **Scaffold.**
   - Create `static/css/` and `static/js/`.
   - Copy `src/css/*` and `src/js/*` into them unchanged.
   - Mount them in `api.py` with `app.mount("/static", StaticFiles(directory="static"), name="static")`.
   - Reduce `dashboard.html` to a shell: `<div id="app">`, the two stylesheets, and `<script type="module" src="/static/js/app.js">`.
   - Keep the old `dashboard.html` as `dashboard_legacy.html` until you're done.
2. **Backend: Octopus routing** (README › Backend › New). Build:
   - `tasks` columns `lane, model_id, category, route_reason, duration_ms, input_tokens, output_tokens, cost_usd`, added through the existing `_migrate()` pattern;
   - a built-in **General** project (`is_general=1`, no path), created on startup;
   - `POST /octopus`, `GET /octopus/models`, `GET /octopus/usage`, following the exact response shapes in `src/js/types.js`;
   - worker support: an `agent` lane (Claude Code, one at a time, existing behaviour) and an `api` lane (OpenAI models, run concurrently and write `result_summary`);
   - models config in `models.json`, with `OPENAI_API_KEY` from the environment;
   - an unavailable or rate-limited model must report `unavailable_reason` or `rate_limited_until`.

   **Ask me before choosing** the routing heuristics or the model IDs.
3. **App shell** (`static/js/app.js`, `views/shell.js`):
   - hash router `#/home`, `#/projects`, `#/project/:id`, `#/recipes`, `#/activity`;
   - topbar;
   - phone tab bar with icons and the FAB;
   - desktop sidebar;
   - density toggle;
   - toast;
   - the `.pager` with `pageSwipe()`;
   - skeletons for about 520ms after each view change, then a staggered `rise` reveal;
   - keyboard shortcuts.
4. **Splash** (`views/splash.js`): `greeting()` plus a dancing `octopusSVG('dance')`. It stays up until a double tap or double click (`onDoubleTap`).
5. **Home** (`views/home.js`), sections in README order:
   - hero ("Octopus says", every state in the table);
   - **Ask Octopus** box (all states: sending, success, error; model and target menus; image attach; polish);
   - rollups;
   - Needs you (approval card with `swipeDecision`, failed card, review card);
   - **Running now** (list of 0–n);
   - rate-limited card;
   - Up next (`dragReorder`);
   - Scheduled;
   - Usage;
   - **Models · last 30 days**;
   - Recent.
6. **Projects** (`views/projects.js`):
   - quick filter chips with counts;
   - Sort / Filter / Search toolbar with menus, plus the Add button;
   - expandable rows listing task titles;
   - General pinned first;
   - no spend shown on rows.
7. **Project detail** (`views/project.js`): header, stats, the Work / Activity / Recipes / Settings tabs, and the Backlog on the existing todos endpoints (drag reorder, select, Run selected).
8. **Chat pane and approval pane** (`views/chat.js`, `views/approval.js`):
   - header "project · model · status";
   - reply footers and the "Routed: …" note;
   - quick actions only for Claude Code chats;
   - "Reply to {model}…" placeholder;
   - long-answer typography;
   - typing indicator.
9. **Sheets** (`views/sheets.js`): New task (with the model chips), Usage, Add project, Shortcuts.
10. **Recipes and Activity views.**
11. **Model label everywhere:** use `modelChipHTML()` on every task card listed in README › Model label.

## Backend gaps
README › Backend gaps lists features the design shows but the API doesn't have yet:
1. queue reorder;
2. schedules and recipes;
3. usage limits and budgets;
4. per-project settings;
5. approval risk, steps and diff;
6. image uploads;
7. polish endpoint;
8. dismissing a failed card;
9. live step progress.

For each one: **stop and ask me** whether to build the backend now or to hide that UI behind a clearly named flag. Never show fake data in production.

## Definition of done
- Side by side with the prototype at 375px and at 1280px, every screen and state matches: open the prototype and use its Tweaks panel for waiting, running, rate_limited, idle, empty, and OpenAI key off.
- No console errors. No horizontal overflow at 320px, 375px, 768px or 1280px.
- All existing API flows still work end to end on a test repo.
- Update the repo `README.md` to cover the new endpoints and `models.json`.

Start by reading the files above. Then give me a short implementation plan and your questions about routing, model IDs and the backend gaps before writing code.
