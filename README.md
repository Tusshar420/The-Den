# The Den

A small always-on service for your Mac, running as **Tushar's Workspace**:
keep a queue of tasks per project, and it runs them through Claude Code one
at a time, automatically moving to the next task when one finishes, and
automatically resuming if it hits a usage limit. A local HTTP API in front
of it is what your phone (via iOS Shortcuts, over Tailscale) talks to.

The whole system goes by **Octopus** - one brain (the router, which
decides which model handles a request) with many arms (Claude Code in any
registered project, plus API models like OpenAI's), reaching into however
many projects have work queued.

This is Phase 1-4 of the plan: the engine, rate-limit auto-resume, running
it as a background service, and the API. iOS Shortcuts and a nicer status
page are the next phases, built once this is working and tested.

## What you need first

- macOS with Python 3.10+ (`python3 --version` to check)
- The Claude Code CLI installed and already logged in (`claude --version`)
- Tailscale already set up between this Mac and your phone/tablet
- A test project (ideally a small git repo you don't mind Claude editing
  unattended, with nothing uncommitted you care about) to try this on first

## 1. Copy this folder onto your Mac

Get this whole `claude-queue` folder (The Den) onto your Mac (e.g. via Termius/`scp`,
AirDrop, or just re-create the files) at `~/claude-queue`.

## 2. Set up a virtual environment and install dependencies

```bash
cd ~/claude-queue
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Confirm the Claude Code invocation still matches your version

CLI flags shift between versions. Run:

```bash
claude --help
```

and check that `--output-format stream-json`, `--verbose` and
`--permission-mode acceptEdits` (or your version's equivalent for "don't
stop to ask permission") still exist. If the names differ, set `CLAUDE_EXTRA_ARGS` to match — see `worker.py`
for where this is read. Also run `which claude` and note the full path;
you'll need it in step 6 if `claude` isn't on launchd's minimal PATH.

## 4. Set required environment variables and do a manual test run

```bash
export API_KEY="pick-a-long-random-string"
export HOST=127.0.0.1   # keep this local-only for now, switch to 0.0.0.0 in step 7
python3 main.py
```

In another terminal:

```bash
curl http://127.0.0.1:8787/health
# {"ok": true}
```

## 5. Register a project and add a test task

```bash
curl -s -X POST http://127.0.0.1:8787/projects \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "test-project", "path": "/absolute/path/to/your/test/repo"}'

# note the returned "id", then:
curl -s -X POST http://127.0.0.1:8787/projects/1/tasks \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "Add a comment at the top of README.md saying hello from the queue", "priority": 0}'
```

Watch the terminal running `main.py` — you should see it pick up the task,
run Claude Code in that repo, and mark it done. Check with:

```bash
curl -s http://127.0.0.1:8787/queue/status -H "X-API-Key: $API_KEY"
```

Don't move on until this works end to end on a throwaway test repo.

## 6. Install it as a background service (launchd)

Edit `setup/com.tushar.claudequeue.plist`: replace every `YOUR_USERNAME`
with your actual macOS username, put a real random value in for `API_KEY`
(matching what you tested with), and add `claude`'s directory to `PATH` if
`which claude` in step 3 wasn't already in `/usr/local/bin` or
`/opt/homebrew/bin`.

```bash
mkdir -p ~/Library/Logs/claude-queue
cp setup/com.tushar.claudequeue.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tushar.claudequeue.plist
launchctl enable gui/$(id -u)/com.tushar.claudequeue
```

Check it's alive:

```bash
curl http://127.0.0.1:8787/health
tail -f ~/Library/Logs/claude-queue/stdout.log
```

To stop/uninstall later: `launchctl bootout gui/$(id -u)/com.tushar.claudequeue`

## 7. Expose it over Tailscale

Once step 6 is solid, change `HOST` to `0.0.0.0` in the plist's
`EnvironmentVariables` (so it accepts connections from your tailnet, not
just this Mac). The service refuses to start if `HOST` isn't `127.0.0.1`
and `API_KEY` is empty, and 10 wrong keys from one address within a minute
lock that address out (HTTP 429) until they age out. Then:

```bash
launchctl bootout gui/$(id -u)/com.tushar.claudequeue
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tushar.claudequeue.plist
launchctl enable gui/$(id -u)/com.tushar.claudequeue
```

Find this Mac's Tailscale address (`tailscale status`, or the Tailscale
menu bar app — it looks like `your-mac-name.your-tailnet.ts.net`). From
your phone, on Tailscale, test:

```
http://your-mac-name.your-tailnet.ts.net:8787/health
```

If that responds, the API is reachable from your phone. Anything hitting
it besides `/health` needs the `X-API-Key` header with the value you set —
this is what stops other devices on your tailnet from using it silently.

## API reference

All endpoints except `/health` require header: `X-API-Key: <your key>`

| Method | Path | Body | What it does |
|---|---|---|---|
| GET | `/health` | - | liveness check, no auth |
| GET | `/projects` | - | list registered projects |
| POST | `/projects` | `{"name", "path"}` | register a project (path = a folder on this Mac; `~` is expanded, a missing folder is rejected) |
| PATCH | `/projects/{id}` | `{"active": true/false, "category": "work"/"personal", "status": "not_started"/"in_progress"/"need_info"/"on_hold"/"completed"}` | pause/resume a project's queue, move it between Work and Personal, or set its status. With the project setting `auto_in_progress` on (the default, `PATCH /projects/{id}/settings`), a task starting under it sets the status to `in_progress` |
| GET | `/projects/{id}/tasks` | - | list that project's tasks |
| POST | `/projects/{id}/tasks` | `{"prompt", "priority", "thread_id"}` | queue a new task, or a reply (see below) |
| POST | `/tasks/{id}/cancel` | - | cancel a queued/rate-limited task |
| GET | `/queue/status` | - | what's running, what's next, counts by status |
| GET | `/tasks` | - | most recent tasks across all projects (`?limit=`, `?status=queued,error`) |
| GET | `/dashboard` | - | browser status page (see below) |
| GET | `/approvals` | - | pending Bash approval requests |
| POST | `/approvals/{id}/decide` | `{"approved", "note"?, "always"?}` | approve or deny a pending request; `always` also adds the command to the project's allow list, `note` is passed back to Claude on a deny |
| GET | `/projects/{id}/todos` | - | list a project's todo backlog, in order |
| POST | `/projects/{id}/todos` | `{"text"}` | add a todo item (appended to the end) |
| PATCH | `/todos/{id}` | `{"text"?, "checked"?}` | edit or check/uncheck a todo item |
| DELETE | `/todos/{id}` | - | remove a todo item |
| POST | `/projects/{id}/todos/reorder` | `{"ids": [...]}` | set the todo order (full id list, in the new order) |
| POST | `/projects/{id}/todos/run` | - | queue every checked, not-yet-run todo, in order |
| GET | `/projects/{id}/notes` | - | list a project's notes (guidelines, requirements, notes), oldest first. Every task run for the project is given these |
| POST | `/projects/{id}/notes` | `{"text", "kind"?}` | add a note; `kind` is `guideline`, `requirement` or `note` (default) |
| PATCH | `/notes/{id}` | `{"text"?, "kind"?}` | edit a note |
| DELETE | `/notes/{id}` | - | remove a note |
| POST | `/projects/{id}/suggest` | - | the Suggest button: queue a read-only review of the project (its backlog and recent tasks are added to the prompt); a second call while one is waiting or running returns that one |
| POST | `/octopus` | `{"text" (or "request"), "project_id"?, "model"?, "agent"?, "priority"?, "source"?, "review"?}` | Octopus: understand, plan and queue a request (see below). Returns `{status, task, decision, plan, routing_id, spoken}`; `status` is `queued`, `proposed` / `needs_input` (a plan waits for Run), or `done` (an instant command, answer in `result`) |
| POST | `/octopus/plan` | `{"text", "project_id"?, "model"?, "agent"?}` | dry run: the plan Octopus would make (nothing is queued or logged) |
| POST | `/octopus/plans/{routing_id}/run`, `/cancel` | `{"project_id"?, "model"?, "agent"?, "priority"?}` | [Run] / [Cancel] on a plan card; Run may change project, model or agent first, and only ever queues once |
| GET | `/routing`, `/routing/{id}` | `?status=&category=&limit=` | every routed request: classification, project, agent, model, tools, reason, outcome, duration, cost |
| GET | `/octopus/overview` | - | everything the Command Center shows in one call |
| GET | `/octopus/agents`, `/octopus/tools`, `/octopus/providers` | - | the agent and tool registries; models grouped by provider with availability, limits and usage |
| PATCH | `/octopus/models/{id}` | `{"enabled"}` | switch a model on or off (stored in settings, `models.json` is not rewritten) |
| GET | `/tasks/{id}/detail`, `/tasks/{id}/diff` | - | the task page: plan steps, files changed, git branch / commits, tests, approvals, activity, result; the full diff |
| GET | `/octopus/images/{name}` | - | a picture the Creative Agent made |
| POST | `/uploads` | the raw image file (`Content-Type: image/png` etc.) | store a picture to attach to a prompt (PNG / JPEG / GIF / WebP, up to 10 MB, checked by content); returns `{"id","mime","size"}` |
| GET | `/uploads/{id}` | - | a picture that was attached (needs the API key like everything else) |
| GET | `/voice` | - | speech providers and their availability, chosen settings, macOS voices |
| POST | `/voice/transcribe`, `/voice/speak`, `/voice/command` | raw audio / `{"text"}` | server-side speech to text; text to speech (`"on": "mac"` speaks on the Mac); a spoken request end to end, returning `spoken` |
| GET | `/octopus/models` | - | models from `models.json` plus live availability (key set? rate-limited?) |
| GET | `/octopus/usage` | - | per-model / per-category counts, success, duration, tokens, est. cost (`?days=30`) |
| POST | `/octopus/polish` | `{"text"}` | rewrite a prompt with the cheapest available answer-only model → `{"text"}` |
| POST | `/tasks/{id}/requeue` | `{"front": true/false}` | put a failed / cancelled / rate-limited task back in the queue (Retry, Retry now, Undo) |
| POST | `/tasks/{id}/top` | - | move a queued task to the front |
| POST | `/queue/reorder` | `{"ids": [...]}` | set the run order of queued tasks |
| POST | `/tasks/{id}/stop` | - | kill a running Claude Code task |
| POST | `/tasks/{id}/pause` | - | hold a task: a queued one is skipped (keeps its place), a running Claude Code one is frozen (SIGSTOP; paused time doesn't count toward the timeout) |
| POST | `/tasks/{id}/play` | - | release a paused task |
| POST | `/tasks/{id}/reviewed` | - | mark a `needs_review` task done |
| GET | `/tasks/{id}/thread` | - | every message in that task's chat plus each turn's step log |
| GET | `/messages` | - | chats, newest message first: `?q=` (every word, any message), `?status=needs\|active\|done\|error\|cancelled`, `?project_ids=1,2`, `?lane=agent\|api\|assistant`, `?limit=`, `?offset=` |
| GET | `/approvals/{id}/context` | - | risk read, steps so far and the project's uncommitted diff |
| GET / PATCH | `/projects/{id}/settings` | see below | default priority, notifications, quiet hours, allowed commands, monthly budget |
| GET / PATCH | `/settings` | `{"notify_on_resume"?, "hold_low_when_busy"?, "monthly_budget_usd"?}` | global toggles, plus the last Claude usage snapshot |
| POST / DELETE | `/sleep` | `{"hours"?}` (default 8, max 24) | "I'm going to sleep" (the Going to sleep button on Home): the queue keeps going and uses the limits fully - Low tasks are no longer held back, a rate limit with no reset time is retried every 15 minutes, and approvals don't ping your phone and auto-deny after 5 minutes (the task is told to carry on without it). Ends by itself after `hours`; DELETE ends it now. State is `sleep_mode` in `/settings` |
| POST | `/notify/test` | - | send a test ActivitySmith push |
| GET | `/usage/summary` | - | real API spend (today, month, last 7 days, by project) and Claude's 5-hour / weekly usage |
| GET / POST | `/recipes` | `{"name", "prompt", "target"}` | saved prompts (`target`: `any`, `all`, or a project id) |
| DELETE | `/recipes/{id}` | - | remove a recipe |
| POST | `/recipes/{id}/run` | `{"project_id"?, "priority"?}` | queue a recipe in one project, or in every active one for an `all` recipe |
| GET / POST | `/schedules` | `{"prompt", "label"?, "project_id"?, "cadence", "time", "weekday"?, "date"?, "model"?, "priority"?}` | run a prompt at a time of day (`cadence`: `once`, `daily`, `weekdays`, `weekly`; `project_id` null = every active project) |
| PATCH / DELETE | `/schedules/{id}` | `{"enabled"}` | turn a schedule on/off, or remove it |
| GET | `/octopus/result/{task_id}` | `?wait=<seconds>` | the answer to a request as plain text (+ a `spoken` line and any `awaiting_approval` cards) - what a voice client or Shortcut polls |
| GET | `/assistant/status` | - | accounts (status, cost tag, granted services - never tokens), permission policies, which model interprets requests, each tool's actions, `tools_refused`, and every model the assistant can ask (`models`, with why an unavailable one is not) |
| POST | `/assistant/connections/{provider}/connect` | `{"services"?}` | start OAuth: returns the consent-page `auth_url` to open in a browser on the Mac |
| GET | `/assistant/oauth/{provider}/callback` | - | Google's redirect (no API key - tied to the sign-in by a single-use `state`) |
| DELETE | `/assistant/connections/{provider}` | - | revoke at the provider and forget the tokens |
| GET / PATCH | `/assistant/policies` | `{"EMAIL_SEND": "ask"}` | capability policies: `allow`, `ask` or `deny` |
| POST | `/assistant/route` | `{"text", "in_thread"?}` | dry run of the intent router: intent, tool, capabilities, approval, model-or-not |
| GET | `/assistant/activity` | `?limit=&tool=` | the activity log |
| GET | `/assistant/email/overview`, `/assistant/calendar/overview` | - | what the Personal tabs show |
| GET / DELETE | `/assistant/reminders`, `/assistant/reminders/{id}` | - | list / cancel reminders |

## Safety note

`--permission-mode acceptEdits` (or whatever flag your Claude Code version
uses for unattended mode) means Claude edits files and runs commands in the
project without stopping to ask you first — there's no one there to answer
the prompt. Only point this at projects you're comfortable giving that
level of trust to, and keep them committed to git so a bad run is always
just a `git diff` / `git checkout` away from undone. Start with a throwaway
test repo, not a client project, until you trust the behavior.

`acceptEdits` only covers Edit/Write - a Bash command (e.g. `sf project
deploy start`) still needs approval that has nowhere to go in a headless
`-p` session. Don't reach for `--allowedTools` to work around this -
testing found it doesn't reliably scope Bash in non-interactive mode (a
pattern that matched nothing still let arbitrary commands, including
destructive ones, through). Use the approval flow below instead.

## Approvals (Bash commands needing a human)

When Claude wants to run a Bash command `acceptEdits` doesn't cover, it's
routed to `approval_server.py` - an MCP stdio server registered as
`--permission-prompt-tool` in `worker.py` - instead of failing with no one
to ask. That server writes a pending row to the `approvals` table, pushes
an ActivitySmith notification (if `ACTIVITYSMITH_API_KEY` is set), and blocks until you decide
it from the dashboard's "Approval needed" card (Approve/Deny), or until
`APPROVAL_TIMEOUT_SECONDS` (default 1 hour) passes and it auto-denies so a
forgotten request doesn't stall the rest of the queue.

**Setup** - this needs Python 3.10+ for the `mcp` package, which is likely
newer than the 3.9 this project otherwise runs on (Apple's Command Line
Tools Python). Give the approval server its own venv rather than
upgrading the main one:

```bash
brew install python@3.12   # if you don't already have a 3.10+ python3
/opt/homebrew/bin/python3.12 -m venv .approval-venv
.approval-venv/bin/pip install "mcp<2"   # v1 API (FastMCP) - v2 renamed it to MCPServer
```

`mcp-config.json` and `worker.py`'s `MCP_CONFIG_PATH`/`PERMISSION_PROMPT_TOOL`
already point at `.approval-venv/bin/python3` running `approval_server.py`
- no further wiring needed once the venv exists. `GET /approvals` lists
pending requests; `POST /approvals/{id}/decide` with `{"approved": true}`
resolves one (the dashboard calls both for you).

The dashboard shows each request with context: a heuristic risk read of the
command (`risk.py`), the steps the task took before asking, and the
project's uncommitted `git diff`. **Always allow** adds the command to the
project's allow list (Settings tab), and the approval server then lets it
through without asking. An entry covers the program plus its subcommand
(`npm test`, `git status`, `npm run build`, `./scripts/deploy.sh`), except
for risky commands (`rm`, `sudo`, `curl`, force-pushes, anything mentioning
production...), which are only ever allowed exactly as approved. A command
that chains, pipes, redirects or substitutes (`;`, `&&`, `|`, `>`, `$(...)`)
never matches an allow-list entry.

## Dashboard

Visit `http://127.0.0.1:8787/dashboard` (or your tailnet address) - it's
Tushar's Workspace, the redesign in `design_handoff_tushars_workspace/`.
Phone first (bottom tab bar, swipe between pages, full-screen chat), with a
sidebar layout at 900px and up. It refreshes every 5 seconds.

The page itself loads without auth, but it asks for your `X-API-Key` on
first visit and keeps it in that browser's `localStorage` (`cq_api_key`) -
it needs the key to actually fetch data, same as any other client of this
API.

- **Home** - what Octopus is doing, the Ask box (routes through `/octopus`),
  anything that needs you (approve by tapping or swiping right, deny by
  swiping left; retry or dismiss failures), what's running with its live
  step, the rate-limit countdown, **Up next** (drag to reorder, ↑ to move
  to the top, ✕ to cancel with Undo), schedules, usage, models and recent
  work.
- **Projects** - filter chips, sort/filter/search, and each project's
  active tasks. A project page has Work (live tasks, its place in the
  queue, the **Backlog**), Activity, Recipes and Settings tabs.
- **Recipes & schedules**, **Activity** - saved prompts and schedules, and
  every finished chat grouped by day.
- **Messages** (key `6`) - the message centre: every chat, one entry per
  task with its replies folded in, newest message first and grouped by day.
  Search matches the title, project and every message in a chat (all words
  must appear) and shows the matching message; filter by status (needs you /
  active / done / failed), project and model. It searches the whole history,
  not just the recent tasks the rest of the dashboard holds.
- **+ / ⌘K / N** - New task: pick a project, a model (or Auto), a priority
  and when to run it.

Tapping a task opens it as a chat: every message, Claude Code's step log,
reply footers (status, model, time, cost) and a reply box. Each task is
normally its own fresh `claude -p` invocation with no memory of anything
else in the queue - replying keeps you in the same conversation instead,
via `thread_id`:

- A brand new task (no `thread_id`) always starts a fresh Claude Code
  session.
- Passing `thread_id: <task id>` in the `POST /projects/{id}/tasks` body
  makes the new task resume whatever session that task's thread last ran
  in (`worker.py` tracks this with `claude --resume`, keyed by whichever
  task in the thread most recently captured a `session_id`).
- A reply can switch models: add `"model": "<models.json id>"` and it goes
  to that model instead (Agent and API models only; not while the chat's
  last message is still unfinished). The choice sticks - later replies stay
  on the latest model. The new model gets the whole conversation: an API
  model is sent the earlier messages (oldest dropped to fit its context
  window), and Claude Code resumes its session plus a catch-up transcript
  of whatever other models said since it last spoke.

The **Backlog** (the todos endpoints) is a per-project list separate from
the queue: add items, drag them into order, select the ones you want and
press **Run selected** - they join the queue in your order, not the order
you selected them in.

The code is plain ES modules with no build step: `static/js/app.js` (router,
actions, polling), `static/js/views/` (one file per screen), and the
handoff's helpers (`api.js`, `format.js`, `gestures.js`...). Views render
template strings that `morph.js` patches into the page, so polling never
interrupts typing. The old single-file dashboard is still at `/dashboard/legacy`.

### Attaching pictures

The Ask box, the New task sheet and a chat's reply box each have a picture
button (and accept a pasted picture). A picture is uploaded once to
`POST /uploads` (stored under `workspace/uploads/`), and the prompt is then sent
with `"images": ["<id>", ...]` - on `POST /octopus`, `POST /octopus/plan` and
`POST /projects/{id}/tasks` (up to 6 per message). What happens next depends on
the lane:

- **Octopus routing** only picks a model that can read pictures (`"vision": true` in
  `models.json`, or Claude Code). If none is available the request says so.
- **Claude Code** is told where the files are and asked to look at them; the task is
  given read access to the upload folder (and the `Read` tool, even for an agent
  without the filesystem tool).
- **API models** (OpenAI, Gemini) get the pictures as real image parts of the message,
  including earlier pictures in the same chat.
- Scheduled prompts can't carry pictures yet.

## Queue order, schedules, usage

- **Order** - the agent lane runs tasks by `queue_pos`: a new task goes after
  everything queued with the same or higher priority (Urgent 1 > Normal 0 >
  Low -1), and reordering or Retry changes it from there.
- **Progress** - Claude Code runs with `--output-format stream-json`; every
  tool call becomes a step ("Running npm test") in `task_events`, and the
  same stream reports real 5-hour and weekly usage. That's where the
  dashboard's usage bars and exact rate-limit resume times come from. **Stop**
  kills a running task and everything it launched.
- **Schedules** - checked every 30 seconds. One that was due while the Mac
  slept runs once when it wakes (not once per missed time).
- **Spend** - "API spend" and budgets count real money only: answer-only
  models (OpenAI, Gemini). Claude Code's cost is an estimate of what the run
  would cost at API prices; a subscription doesn't bill it, so it shows on
  each task but isn't counted as spend. A project that reaches its monthly
  budget is paused, and you get a push.
- **Notifications** (ActivitySmith) - per project: approvals and failures
  (time-sensitive, break through Focus), completions (passive), and quiet hours 23:00-08:00
  that hold everything except approvals. **Ping me when back** sends one push
  when a rate-limit pause ends. **Save the session for urgent work** holds
  Low tasks while the 5-hour window is over 85%.

## Octopus: understand, plan, route, execute, report

```
request (text · voice · API)
  -> understand   router.classify: instant commands, personal assistant, image, then a cheap classifier (keyword rules as backup)
  -> plan         octopus/plan.py: project, agent (agents.json), model (scored), tools (tools.json), permissions, risk
  -> review?      risky / blocked / needs an answer / you asked to see every plan  -> the OCTOPUS PLAN card, [Cancel] [Run]
  -> execute      the existing queue: agent lane (Claude Code), api lane (text or image), assistant lane, or an instant command
  -> validate     tests seen in the run, files changed, git branch and diff  (task page > Details)
  -> report       text on the dashboard, and `spoken` for voice
```

- **Command Center** (Home): the Ask box (agent override, plan preview, microphone), what Octopus is doing now with
  project / agent / model / progress, plans waiting on you, provider availability and the autonomy switch.
- **Model choice is scored, not just ordered.** Among the enabled candidates of a category `octopus/plan.py` weighs
  strength, cost, speed, track record (`db.model_category_stats`) and your configured order, with a penalty for spending
  a *scarce* resource (`"scarce": true`, Claude Code) on work that doesn't need it, more so while its 5-hour window is
  nearly used up. `optimize` on each category in `models.json` (`cost` / `balanced` / `quality`) sets the weights.
  Non-transferable categories (coding) wait for their first choice instead of hopping. Every decision is stored in the
  `routing` table with each candidate's score, and shown on the **Routing** page.
- **Agents** (`agents.json`): instructions, preferred models, allowed tools and capabilities, context needs, execution
  policy (lane, when to show the plan, Claude Code mode per autonomy level, cost limit). **Tools** (`tools.json`): what a
  request *needs* - only those are given to a task (Claude Code gets the rest switched off with `--disallowedTools`);
  MCP servers slot in as `"kind": "mcp"` entries. Both re-read on change, no restart.
- **Autonomy** (Settings): *Assist* investigates and proposes (coding runs read-only), *Execute* runs normal actions and
  asks for risky ones, *Autonomous* also runs what would ask - but protected capabilities (deploy, force-push, delete,
  production, send email, admin) always ask unless you set them to Allow yourself. **Plan preview**: standard, every
  request, or only risky ones. "Don't change anything yet" in a request runs Claude Code in plan (read-only) mode.
- **Instant commands** (`octopus/control.py`, no model): "what's happening?", "stop the current task", "cancel that"
  (a waiting plan first), "continue the previous task", "list my projects", "add X to the backlog of Y".
- **Image generation**: the Creative Agent's `gpt-image-1` / `gemini-image` models save pictures under `workspace/images/`.
  **Research** uses web-search grounding on OpenAI / Gemini.
- **Voice** (`octopus/voice/`, `static/js/voice.js`): a way in and out of the same pipeline, never a second assistant.
  Providers: speech-to-text = the browser's own recognition, or local Whisper (`whisper-cli` from whisper.cpp + a model,
  `WHISPER_CLI` / `WHISPER_MODEL`; not installed by default); text-to-speech = the browser's voices, or macOS `say`.
  Add a cloud provider by subclassing `voice.STT` / `voice.TTS` and calling `register_stt` / `register_tts`. Follow-ups
  ("fix the second one", "run the tests afterward") attach to the last chat, a spoken plan is answered with "yes" / "no",
  and `POST /voice/command` returns the `spoken` line.

### Octopus V1 details: routing requests to the right model

The "Ask Octopus" box at the top of the dashboard (or `POST /octopus`) takes
any request and decides where it goes, instead of everything going to
Claude Code:

1. **Classify** - a cheap model (`routing.classifier` in `models.json`)
   labels the request `coding`, `general` or `simple`. If that model
   isn't reachable, keyword rules in `router.py` decide instead.
2. **Pick a model** - each category in `models.json` has an ordered
   `candidates` list. The first one that is enabled, has its API key and
   isn't rate-limited wins; the rest are fallbacks. `coding` is marked
   `"transferable": false`, so it waits for Claude Code through a rate
   limit rather than hopping to a model without project access.
3. **Queue it** - on the lane that model runs in:
   - `agent` lane = the original worker, unchanged: `claude -p` inside the project.
   - `api` lane = a plain model call (`providers.py`), run by a second
     loop in `worker.py` alongside the agent lane, so a quick question never
     waits behind an hour-long coding task.

Every task records the decision (`model_id`, `category`, `routed_by`,
`route_reason`) and its usage (`duration_ms`, tokens, estimated
`cost_usd`) - shown in the chat view and the dashboard's Models panel,
and summarised by `GET /octopus/usage`.

Things that did not change: the plain `POST /projects/{id}/tasks`
endpoint and Todos still go straight to Claude Code; a reply stays
on its thread's latest model unless it names another (Claude threads keep
`--resume`, api-lane threads get the earlier messages as history); approvals work as before.

**Registry** - `models.json` lists every model (provider, capabilities,
rough coding/reasoning/cost/speed ratings, context size, price) and the
routing rules. It's re-read on change - no restart needed to add a model,
disable one (`"enabled": false`) or reorder fallbacks. Requests without a
project land in a built-in **General** project (`workspace/general/`).

**Setup** - add your OpenAI API key (platform.openai.com - separate from
a ChatGPT subscription) to the plist's `EnvironmentVariables` as
`OPENAI_API_KEY`, then restart the service. Without it, OpenAI models show
as unavailable and general/simple requests fall back to Claude Code.

## Personal assistant (email, calendar, reminders, queue, AI models)

Octopus can also act as a secretary: talk to it in plain language and it reads
your calendar, searches and drafts email, schedules and moves meetings, and sets
reminders - through your own Google account, never a password. The same chat can
look at Octopus's own queue and projects and hand work to the app's AI models
(see *Queue, projects and AI models* below).

**Models think. Agents specialize. Tools perform actions. Octopus orchestrates.**

```
request -> assistant/intent.py     rules, no model: intent, tool, capabilities,
                                   approval?, needs a model?, can run alone?
        -> router.py               "assistant" lane (worker.assistant_worker_loop)
        -> assistant/orchestrator  plain tool calls when the words are enough
                                   ("what's on my calendar tomorrow") - NO model;
                                   otherwise assistant/planner.py: a model that
                                   only *proposes* tool calls as JSON
        -> assistant/actions.py    THE gate: capability policy ALLOW / ASK / DENY,
                                   approval, audit log - for every path
        -> assistant/scope.py      is the tool / permission even given to the Personal
                                   Assistant? (agents.json + tools.json, checked per call)
        -> assistant/tools/        email, calendar, reminders, agent (Claude Code),
                                   workspace (queue, tasks, projects, backlog), ai (models)
        -> assistant/providers/    Gmail + Google Calendar (Microsoft next)
```

### Set up Google (free, once)

Nothing is enabled until you do this; the dashboard shows **NOT CONFIGURED** with
these steps until then.

1. console.cloud.google.com -> create a project (no billing needed).
2. Enable the **Gmail API** and **Google Calendar API**.
3. OAuth consent screen: *External*, add your own Google account as a test user.
4. Credentials -> Create credentials -> OAuth client ID -> **Desktop app**.
5. Add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to the launchd plist's
   `EnvironmentVariables` and restart the service (same as `OPENAI_API_KEY`).
6. Open `http://127.0.0.1:8787/dashboard` **in a browser on the Mac** -> Personal
   -> Connections -> Connect Google. (The sign-in redirects to `127.0.0.1`, so it
   completes on the Mac itself; after that everything works from your phone.)

If Google rejects the redirect URI, register
`http://127.0.0.1:8787/assistant/oauth/google/callback` on the client, or use a
*Web application* client. **Refresh tokens for an app left in "Testing" expire
after 7 days** - either publish the consent screen (unverified is fine for your
own account) or reconnect weekly; Octopus shows *Reconnect needed* when it happens.

Scopes are least-privilege: `gmail.readonly` + `gmail.compose` (read, draft,
send - **no** scope that can delete mail) and `calendar.events` + `calendar.freebusy`.

### Permissions

Each capability is `ALLOW`, `ASK` or `DENY` (Personal -> Connections -> Permissions,
or `PATCH /assistant/policies`). A call needing several gets the strictest.

| Capability | Default | | Capability | Default |
|---|---|---|---|---|
| `EMAIL_READ` | allow | | `CALENDAR_READ` | allow |
| `EMAIL_DRAFT` | allow | | `CALENDAR_CREATE` (nobody invited) | allow |
| `EMAIL_SEND` (send/reply/forward) | **ask** | | `CALENDAR_UPDATE` | **ask** |
| `EMAIL_DELETE` | **deny** (no such action exists) | | `CALENDAR_DELETE` / `CALENDAR_INVITE` | **ask** |
| `REMINDER_*` | allow | | `AGENT_CODING` (hand work to Claude Code) | **ask** |
| `PROJECT_READ` (queue, tasks, projects, backlog) | allow | | `TODO_WRITE` (add to a backlog) | allow |
| `TASK_CONTROL` (cancel / stop a task) | allow | | `AI_MODEL_USE` (ask the app's AI models) | allow |
| `WEB_ACCESS` (research answers) | allow | | `IMAGE_GENERATE` | allow |

Setting a normally-`ask` capability to `allow` is "trusted": it runs without asking
and the activity log marks it `trusted`. Even then, **once a run has read text
written by someone else (an email body, an invite), outbound actions in that run
ask anyway** - a prompt-injected email can't send mail on your behalf. The same goes for
text written by a task, a backlog item or another model's answer: after reading any, cancelling a
task, adding to a backlog, handing work to Claude Code and sending mail all ask first. In
**Assist** autonomy, anything that changes something asks (cancelling a task included).

An approval card shows exactly what will run (recipient, subject, message; or
title, date, time, attendees, calendar) and running it happens once, from the
stored arguments - never regenerated. Message content is dropped from the database
after you decide; the activity log never holds bodies or tokens. Approvals unanswered
for 24 hours expire.

### Queue, projects and AI models

Two more tools sit behind the same gate, so policies, approvals and the activity log apply unchanged.

**`workspace`** - Octopus's own queue, through the same `db` layer the dashboard uses:
`queue` (what is running / waiting / needs approval), `tasks` (filter by status or project),
`task` (one task's request and answer), `projects`, `backlog`, `usage`, `add_todo`, `cancel_task`.
"Which tasks failed today?", "cancel task 12" and "how much have I spent on AI this month?" go
straight to it; "what's happening?" and "stop that" are still the instant no-model commands.

**`ai`** - `models`, `ask` and `image`: the assistant can hand a long piece of writing, a
translation, a web-grounded answer or a picture to the models in `models.json`. The model is chosen
exactly as the Ask box chooses it (`routing.categories`, then enabled + key present + not
rate-limited, including the AI Providers page switches); a model you name ("using Gemini") is used or
the call fails - it is never quietly swapped. The answering agent's instructions and `max_cost_usd`
apply, and a project over its monthly budget can't spend more. Token counts and cost are added to the
task's usage.

What is deliberately **not** possible:

- **Approvals cannot be answered by the assistant**, only counted - no tool has an approve / decide action.
- Only **active projects** are visible: an archived or budget-paused project's tasks and backlog are
  not listed, and asking for one by id gets the same "can't find it" as an id that doesn't exist.
- **Personal-assistant chats are listed by status only** - never their text (it is your email).
- Pending-approval details (shell commands, email bodies) are not shown, only their count.

**Scope is enforced.** `agents.json` -> `personal` -> `tools` and `permissions`, and `enabled` in
`tools.json`, now bind: remove `ai_models` from the agent, set `"enabled": false` on it, or drop
`TASK_CONTROL` from its permissions and the assistant is no longer shown that tool and every call
is refused (an approval that was already waiting is refused too). If either file can't be read,
nothing is allowed. `GET /assistant/status` lists `tools_refused` and the `models` the assistant can see.

**Setup:** nothing new to connect - but the models need their keys *in the service's environment*.
The launchd plist only has the keys you put in it, so add `OPENAI_API_KEY` / `GEMINI_API_KEY` to its
`EnvironmentVariables` (as above) and restart, or `ai.ask` can only reach Claude Code (your
subscription, tools off) and `ai.image` has no model at all - `GET /assistant/status` -> `models`
shows each model's `why_not`. Then restart the service once so it loads this code.

**Limits:** an `ai.ask` answer reaches the planner cut at about 5,000 characters, so keep requests to
roughly 600 words (the tool tells the planner when it was shortened). The prompt the planner writes is
sent to the model's provider (Gemini's free tier may train on it - see `models.json`); set
`AI_MODEL_USE` to `ask` or `deny` if you don't want that without a yes. If a model call is approved
rather than run automatically, its cost is not added to the task's usage.

### How a request is understood

- **No model** for lookups the words fully specify: calendar reads and availability,
  latest email from X, reminders, "block 3 PM tomorrow for project work".
- **A model** for drafting, replies, meetings (who is "Rahul"?), moving "it",
  analysing an email, and anything spanning tools. `routing.assistant_planner` in
  `models.json` orders the candidates; with no OpenAI/Gemini key it falls back to
  the local `claude` CLI **with every tool switched off** (your Claude
  subscription, text in / text out). "Fix the bug mentioned in this email" ends in
  an `agent.code` step: an approval card, then a normal Claude Code task.
- Conversation memory (the events/emails a chat has surfaced, with their ids) is kept
  per chat so "which one is at 3 PM?" -> "move it to 4" works; the move still asks first.

### Voice / Shortcuts

Voice is built in (see *Octopus* above: `POST /voice/command`). For a client that does its own speech
(a Shortcut), the interface is the same one the dashboard uses - no separate logic:

1. STT text -> `POST /octopus {"text": "...", "continue_recent": true}` (`continue_recent`
   attaches to the assistant chat active in the last 15 minutes, so follow-ups work)
2. `GET /octopus/result/{task.id}?wait=30` -> `text`, a ready-to-speak `spoken` line, and
   `awaiting_approval` (each with its card)
3. read `spoken` aloud; on a yes/no, `POST /approvals/{id}/decide {"approved": true}`
4. `GET /octopus/result/{task.id}` again for the outcome

### Adding a tool (Slack, Drive, GitHub ...)

Write a `Tool` subclass in `assistant/tools/` (describe each action; `prepare_<action>`
resolves arguments and says which capabilities it needs and what the approval card shows;
`run_<action>` does it), `register()` it, add its capabilities to
`assistant/capabilities.py`. Routing, the planner's tool list, the gate, approvals, the
activity log and the dashboard's approval cards pick it up. A new mail/calendar backend
is one file in `assistant/providers/` implementing `EmailProvider` / `CalendarProvider`.

### Not done yet

- **Outlook / Microsoft 365**: the OAuth config is registered; the Graph adapters are not built.
- Local Whisper needs `whisper-cpp` and a model installed by you; until then the browser's recognition is used.
- Recipes and schedules still queue through the older router path (no plan record / agent).
- Forwarding does not include attachments; free-slot search only sees attendees whose
  calendars are shared with you (it says so when it can't).
- Reminders push through ActivitySmith (`ACTIVITYSMITH_API_KEY`); without it they only appear in the dashboard.

### Tests

`.venv/bin/python -m unittest discover -s tests -t .` - no network, no Google account,
no model calls: fake Gmail/Calendar, a scripted stand-in model, a real local OAuth
token server, and the real HTTP API + assistant worker end to end.

## Not built yet (next phases)

- iOS Shortcuts flows calling this API (the endpoints they need exist - see *Voice / Shortcuts* above)
- Pictures on scheduled prompts (a picture can be attached to anything sent now)
