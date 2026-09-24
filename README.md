# claude-queue

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

Get this whole `claude-queue` folder onto your Mac (e.g. via Termius/`scp`,
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
just this Mac), then:

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
| PATCH | `/projects/{id}` | `{"active": true/false}` | pause/resume a project's queue |
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
| POST | `/octopus` | `{"text" (or "request"), "project_id"?, "model"?, "priority"?}` | Octopus: classify + route a request, queue it on the right lane (see below). Returns `{task, decision}` |
| GET | `/octopus/models` | - | models from `models.json` plus live availability (key set? rate-limited?) |
| GET | `/octopus/usage` | - | per-model / per-category counts, success, duration, tokens, est. cost (`?days=30`) |
| POST | `/octopus/polish` | `{"text"}` | rewrite a prompt with the cheapest available answer-only model → `{"text"}` |
| POST | `/tasks/{id}/requeue` | `{"front": true/false}` | put a failed / cancelled / rate-limited task back in the queue (Retry, Retry now, Undo) |
| POST | `/tasks/{id}/top` | - | move a queued task to the front |
| POST | `/queue/reorder` | `{"ids": [...]}` | set the run order of queued tasks |
| POST | `/tasks/{id}/stop` | - | kill a running Claude Code task |
| POST | `/tasks/{id}/reviewed` | - | mark a `needs_review` task done |
| GET | `/tasks/{id}/thread` | - | every message in that task's chat plus each turn's step log |
| GET | `/approvals/{id}/context` | - | risk read, steps so far and the project's uncommitted diff |
| GET / PATCH | `/projects/{id}/settings` | see below | default priority, notifications, quiet hours, allowed commands, monthly budget |
| GET / PATCH | `/settings` | `{"notify_on_resume"?, "hold_low_when_busy"?, "monthly_budget_usd"?}` | global toggles, plus the last Claude usage snapshot |
| POST | `/notify/test` | - | send a test ntfy push |
| GET | `/usage/summary` | - | real API spend (today, month, last 7 days, by project) and Claude's 5-hour / weekly usage |
| GET / POST | `/recipes` | `{"name", "prompt", "target"}` | saved prompts (`target`: `any`, `all`, or a project id) |
| DELETE | `/recipes/{id}` | - | remove a recipe |
| POST | `/recipes/{id}/run` | `{"project_id"?, "priority"?}` | queue a recipe in one project, or in every active one for an `all` recipe |
| GET / POST | `/schedules` | `{"prompt", "label"?, "project_id"?, "cadence", "time", "weekday"?, "date"?, "model"?, "priority"?}` | run a prompt at a time of day (`cadence`: `once`, `daily`, `weekdays`, `weekly`; `project_id` null = every active project) |
| PATCH / DELETE | `/schedules/{id}` | `{"enabled"}` | turn a schedule on/off, or remove it |

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
an ntfy notification (if `NTFY_TOPIC` is set), and blocks until you decide
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

The **Backlog** (the todos endpoints) is a per-project list separate from
the queue: add items, drag them into order, select the ones you want and
press **Run selected** - they join the queue in your order, not the order
you selected them in.

The code is plain ES modules with no build step: `static/js/app.js` (router,
actions, polling), `static/js/views/` (one file per screen), and the
handoff's helpers (`api.js`, `format.js`, `gestures.js`...). Views render
template strings that `morph.js` patches into the page, so polling never
interrupts typing. Image attachments are designed but hidden
(`static/js/features.js`) until the backend supports them. The old
single-file dashboard is still at `/dashboard/legacy`.

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
- **Notifications** (ntfy) - per project: approvals (priority 5, breaks
  through Focus), failures (4), completions (2), and quiet hours 23:00-08:00
  that hold everything except approvals. **Ping me when back** sends one push
  when a rate-limit pause ends. **Save the session for urgent work** holds
  Low tasks while the 5-hour window is over 85%.

## Octopus (V1): routing requests to the right model

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
endpoint and Todos still go straight to Claude Code; a reply always stays
on its thread's model (Claude threads keep `--resume`, api-lane threads
get the earlier messages as history); approvals work as before.

**Registry** - `models.json` lists every model (provider, capabilities,
rough coding/reasoning/cost/speed ratings, context size, price) and the
routing rules. It's re-read on change - no restart needed to add a model,
disable one (`"enabled": false`) or reorder fallbacks. Requests without a
project land in a built-in **General** project (`workspace/general/`).

**Setup** - add your OpenAI API key (platform.openai.com - separate from
a ChatGPT subscription) to the plist's `EnvironmentVariables` as
`OPENAI_API_KEY`, then restart the service. Without it, OpenAI models show
as unavailable and general/simple requests fall back to Claude Code.

## Not built yet (next phases)

- iOS Shortcuts flows calling this API
- Image attachments (the dashboard's attach buttons are hidden until then)
