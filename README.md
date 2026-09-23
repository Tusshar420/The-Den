# claude-queue

A small always-on service for your Mac, running as **Tushar's Workspace**:
keep a queue of tasks per project, and it runs them through Claude Code one
at a time, automatically moving to the next task when one finishes, and
automatically resuming if it hits a usage limit. A local HTTP API in front
of it is what your phone (via iOS Shortcuts, over Tailscale) talks to.

The background worker that actually pulls tasks and runs them (`worker.py`)
goes by **Octopus** - one brain processing at a time, but reaching into
however many projects have work queued.

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

and check that `--output-format json` and `--permission-mode acceptEdits`
(or your version's equivalent for "don't stop to ask permission") still
exist. If the names differ, set `CLAUDE_EXTRA_ARGS` to match — see `worker.py`
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
| POST | `/projects` | `{"name", "path"}` | register a project (path = absolute repo path on this Mac) |
| PATCH | `/projects/{id}` | `{"active": true/false}` | pause/resume a project's queue |
| GET | `/projects/{id}/tasks` | - | list that project's tasks |
| POST | `/projects/{id}/tasks` | `{"prompt", "priority", "thread_id"}` | queue a new task, or a reply (see below) |
| POST | `/tasks/{id}/cancel` | - | cancel a queued/rate-limited task |
| GET | `/queue/status` | - | what's running, what's next, counts by status |
| GET | `/tasks` | - | most recent tasks across all projects (`?limit=`, `?status=queued,error`) |
| GET | `/dashboard` | - | browser status page (see below) |
| GET | `/approvals` | - | pending Bash approval requests |
| POST | `/approvals/{id}/decide` | `{"approved": true/false}` | approve or deny a pending request |
| GET | `/projects/{id}/todos` | - | list a project's todo backlog, in order |
| POST | `/projects/{id}/todos` | `{"text"}` | add a todo item (appended to the end) |
| PATCH | `/todos/{id}` | `{"text"?, "checked"?}` | edit or check/uncheck a todo item |
| DELETE | `/todos/{id}` | - | remove a todo item |
| POST | `/projects/{id}/todos/reorder` | `{"ids": [...]}` | set the todo order (full id list, in the new order) |
| POST | `/projects/{id}/todos/run` | - | queue every checked, not-yet-run todo, in order |

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

## Dashboard

Visit `http://127.0.0.1:8787/dashboard` (or your tailnet address) in a
browser for a live view of the queue: counts by status, what's running now,
what's next, and a searchable, filterable history of every task. It
auto-refreshes every 5 seconds and works on mobile.

The page itself loads without auth, but it asks for your `X-API-Key` on
first visit and keeps it in that browser's `localStorage` - it needs the
key to actually fetch data, same as any other client of this API.

Tapping a task opens it as a chat: Claude's full result/error, exact
timestamps, and a reply box. Each task is normally its own fresh `claude -p`
invocation with no memory of anything else in the queue - replying keeps
you in the same conversation instead, via `thread_id`:

- A brand new task (no `thread_id`) always starts a fresh Claude Code
  session.
- Passing `thread_id: <task id>` in the `POST /projects/{id}/tasks` body
  makes the new task resume whatever session that task's thread last ran
  in (`worker.py` tracks this with `claude --resume`, keyed by whichever
  task in the thread most recently captured a `session_id`).
- The flat task list only shows root tasks (one card per conversation);
  replies live inside that task's chat view.

The dashboard's **Todos** tab is a per-project backlog, separate from the
task queue itself: add items, reorder them with ↑/↓, and check the ones
you want to run. Nothing happens until you press **Run selected** - that
converts every checked, not-yet-run item into a real queued task, in the
order you arranged them (not the order you checked them in), and the
worker's usual one-at-a-time processing takes it from there - box 1
finishes before box 2 starts. A checkbox greys out while its task is
in-flight (queued/running); once it finishes, uncheck and re-check the
item to stage a fresh rerun. Unchecking an in-flight item doesn't cancel
it - use the cancel button on the task itself (in History) for that.

## Not built yet (next phases)

- iOS Shortcuts flows calling this API
- Multi-project priority tuning beyond simple priority number + insertion order
