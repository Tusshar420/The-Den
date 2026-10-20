# Den redesign — changes since the last handoff

Give this file to Claude Code together with the updated `.dc.html` files in this folder. Each item lists **where it lives** (file → section) so it can be found fast. Styles are inline on every element — match them exactly. All data in the prototype is sample data; anything that "saves" uses component state or `localStorage` and must be wired to the real API.

New files since last handoff: **`DenStatus.dc.html`** (splash/error/offline/maintenance screens). Everything else is an update to an existing file.

---

## 1. Global

### 1.1 Currency setting
- **Where:** `DenSettings` → new **MONEY** group (above Appearance). Options: `$ USD`, `₹ INR`, `€ EUR`, `£ GBP`, `AED`, `A$ AUD`.
- Saved to `localStorage['den.currency']`. Shell (`Den Home`) exposes `window.denCur` (symbol) and passes `currency` prop to every child DC.
- Every money string uses `(window.denCur || '$')` instead of a hard-coded `$` — deals, budgets, API costs, reports, usage widgets, AI-account spend.
- Symbol only — **no conversion**.

### 1.2 Skeleton loading
- Shimmer keyframe: `@keyframes denShimmer` in shell helmet. Shimmer style: `linear-gradient(90deg, rgba(255,255,255,0.035) 0%, rgba(255,255,255,0.09) 50%, rgba(255,255,255,0.035) 100%)`, `background-size:400px 100%`, `animation:denShimmer 1.2s linear infinite`.
- **Home** first load (~600 ms), **every page** first open, **right panel** on each tab switch (~420 ms overlay, bg `#0D131B`).
- **Tab switches:** Projects (tabs + opening a project), CRM records, Team (~380 ms).
- **To-do:** shimmer rows (round 20px check + 2 text bars) on first load and on filter/sort change. No white flash between skeleton and data.

### 1.3 Resizable side panels
- Drag the **left edge** of the right panel and the chat panel. 8px invisible handle, coral 2px line on hover, `cursor:col-resize`.
- Right panel: 300 → min(640, 50% viewport). Chat panel: 380 → min(1000, viewport − 120). Defaults 360 / 520.
- Persisted: `localStorage['den.rpWpx']`, `['den.chatWpx']`. **Double-click** handle resets. Hidden on phone (panels are full-width there).

### 1.4 Header search
- On wide screens (main area ≥ 1040px) the search bar **fills all free space** between the page title and the layout button. Narrower: icon-only, sits right before the layout button.

---

## 2. Home & right panel

### 2.1 Usage dropdown (header)
- Accounts are laid out **side by side** (CSS grid `repeat(auto-fit, minmax(190px, 1fr))`), dropdown width 470px (max `100vw − 24px`). Reset time moved **under** each bar.

### 2.2 Queue panel — per-task model / mode / account
- Running task and **every Up next row** show 2–3 chips: **Model** (Auto, Claude Code, GPT-5, Gemini 2.5), **Mode** (Auto, Edits, Plan, Ask — only when model is Claude Code), **Account** (filtered to the chosen model, shows remaining limit/spend).
- Each chip opens a small menu (200px, dark glass). Changing model resets account to that model's first. Clicks don't open the chat (stopPropagation).

### 2.3 Chat panel composer (restored rich version)
- 2-line textarea (Enter sends, Shift+Enter newline).
- Bottom row: **Model·account chip** (opens upward list: Claude Code · LA / Tushar, GPT-5 · Den API, Gemini 2.5 · Pro, Auto) → **Edits / Plan / Auto / Ask** segmented (only for Claude Code) → attach picture → polish ✦ → spacer → **send** (40px coral square, dims when empty).
- Responsive: send sits in its own column; the controls wrap onto a second line on narrow panels.
- Action row above (Run now, Edit prompt, Cancel…) is one line, horizontal scroll.

### 2.4 Calendar (right panel) → full view
- New expand icon (32px) left of "+ New" opens the **Calendar page** (`DenWork` page="calendar"). On phone/tablet the right panel closes first.

### 2.5 Hidden private vault
- **Reveal:** long-press (~900 ms) on the empty space of the right rail (desktop) or the bottom tab strip (phone). A dashed violet lock button appears.
- **Panel** (`rp = 'vault'`): PIN pad (4 digits, auto-submits) → folders (Private notes, IDs & documents, Banking, Contracts) with files, private note box, "Add file".
- **Lock & hide** removes the button again; closing the panel locks it; auto-locks after the "Lock again after" setting.
- ⚠️ Real build must encrypt server-side and never ship the PIN to the client.

---

## 3. Projects (`DenProjects`)

### 3.1 Pinned links row
- Compact pill row under the project header (above the KPI strip): icon + label + ↗, opens in new tab. Trailing dashed **+** jumps to Overview → Links.
- **Overview → Links** card: each link row has **pin** (amber when pinned), **edit inline**, **remove**. Link kinds: GitHub, Live site, Staging, Docs, Design, Other (each with its own icon/colour — see `LK()` at the top of the script).
- Default seed: GitHub, Staging, Design pinned; Docs unpinned.

### 3.2 Env & credentials tab (PIN-locked)
- Tab label **"Env & credentials"**. Locked state → PIN screen. After unlock, a sub-switch: **Credentials | Environment variables**.
- **Credentials:** platform preset (GitHub, Vercel, Hostinger, AWS, Google Workspace, Stripe, Figma, Cloudflare, cPanel, Custom) auto-fills the login URL; label, username, password (show/hide, **Generate** 18-char), environment (All/Production/Staging/Dev), note.
  - Row: coloured initial tile, name + env badge, URL; username box with copy; password box masked (reveal = 10 s) with copy; **Open** (coral, `window.open(url)`), Edit, Delete (click twice).
  - Search + env filter, **Lock** button. Auto-lock 5 min.
- **Environment variables:** the former Environment tab moved here (Dev / Staging / Prod, import `.env`, download, secret flag, sync).

### 3.3 Files tab now contains Changes
- The separate **Changes** tab is gone; git changes (staged files, commit message, commit/push) live inside **Files**.

---

## 4. CRM & Clients (`DenCRM`)
- **Create client:** "+ New" on the Clients tab opens an inline form.
- **Edit client:** "Edit" button in the client header → same form (name, main contact, phone, email, colour swatches, health Healthy/Watch/At risk). **Delete** in edit mode (two-click confirm; also removes that client's deals).

---

## 5. AI accounts (`DenSystem` page="accounts")
- Renamed from "Claude accounts" (nav + ⌘K).
- **Provider tabs:** Claude · OpenAI · Gemini · Others (with counts). Each tab has its own accounts, active account, add flow and fallback order.
- **Claude:** session + weekly meters (as before), log in via device code or paste token.
- **OpenAI / Gemini / OpenRouter:** "Spend this month" vs budget + "Rate limit used". Add by API key.
- **Others:** also asks for a **Base URL** (OpenRouter, Ollama, any OpenAI-compatible). Local Ollama shows GPU memory + loaded model.
- Per card: Switch to this, Rename, Colour, Refresh limits, Log out (two-click).

---

## 6. Settings additions (`DenSystem` page="setx", shown under `DenSettings`)
- **PINs & locks** (top section): "Use one PIN for both" toggle → one card, or separate cards for **Hidden vault** and **Credentials**. Each card: current PIN, new, confirm, Save (validates, shows ✓). **Lock again after:** 1 min / 5 min / 15 min / Never. Footer hint: reset from server with `den pin --reset`.
- PINs stored (prototype) in `localStorage['den.pin.vault']` / `['den.pin.creds']`, default `1234`.

---

## 7. Message center (`DenMessages`)
- **Share chat:** "Share" button in the thread header (becomes "Shared · N" with avatar stack). Popover: invite by name/email with role (**Can chat / Can view**), suggestions, member list (click role to toggle, remove), **share link** toggle + copy, "Ask me before Octopus acts on guests' messages" toggle.
- Guests' messages render as a separate "person" bubble style; a guest with chat rights replies in the demo.
- Composer is at the **bottom**; full-screen is a **header icon** just before close (no extra full-screen button inside opened records).

---

## 8. New: status screens (`DenStatus.dc.html`)
Prop `page` = `crash` | `pagecrash` | `down` | `maint`. Shell shows them as overlays; preview from ⌘K ("Preview: …").

- **Splash** (in shell, once per session — `sessionStorage['den.splash']`): bobbing octopus, "Good morning/afternoon/evening, **Tushar!**", random friendly line, status ticker + progress bar → "Double-click anywhere to open" (also Enter / button). Exit: octopus scales up, screen blurs + fades (600 ms).
- **pagecrash** — inline card replacing one page's content: error text, Try again, Go to Home, Copy error, Report to Octopus. Rest of the app keeps working.
- **crash** — full-screen recovery: worried octopus, "Something tangled my arms.", Reload the Den, **Open in safe mode** (choose: widgets, extra modules, custom settings, cached data) → yellow "Safe mode" banner with "Turn off safe mode". Technical details toggle, copy, report.
- **down** — Octopus/provider outage: red top banner on pages ("Octopus is offline since 14:02 · 3 tasks paused · the Den still works" + Status + Restart). Status page: state card with Restart (spinner → green), **system health** list (server, DB, runner, each AI account, OpenAI, Gemini, disk) with quick actions, last log lines + copy.
- **maint** — "Updating to Den v3.1": progress bar, step list (backup → download → migrate → restart → done), then "Open the Den".

> For the real app: `crash` must be a **static HTML page served by the server** (not React) so it shows even when the JS bundle fails; `pagecrash` = a React error boundary per route; `down` = driven by a `/health` endpoint.

---

## 9. ⌘K additions
Show splash screen · Preview: page error · Preview: app crash · Preview: Octopus offline · Preview: maintenance · Sign-in screen · Replay first-run tour · Review finished runs · Switch AI account · Show plan card demo.
