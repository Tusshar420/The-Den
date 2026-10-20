"""
Simple SQLite-backed storage for the Claude task queue.

One process, low throughput (one task runs at a time), so a plain
threading.Lock around a single sqlite3 connection is plenty - no need
for a heavier DB. Keeping this dependency-free (sqlite3 is stdlib).
"""

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import uploads

DB_PATH = Path(__file__).parent / "queue.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'work',
    -- not_started | in_progress | need_info | on_hold | completed | archived (PROJECT_STATUSES)
    status TEXT NOT NULL DEFAULT 'not_started'
);

-- Environments under one project (dev, staging, prod...): each is its own
-- folder. A task can pick one (tasks.env_id) and runs there; todos, notes,
-- phases and the timeline stay shared at the project level.
CREATE TABLE IF NOT EXISTS project_envs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    prompt TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    -- queued | running | done | error | needs_review | rate_limited | cancelled | deleted (in the recycle bin)
    status TEXT NOT NULL DEFAULT 'queued',
    result_summary TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    resume_at TEXT,
    -- NULL on a root task (the first message of a new chat). A reply sets
    -- this to the root task's own id, so every message in a chat -
    -- root or reply - carries the same thread_id once resolved (see
    -- get_thread_session_id) even though the column itself is only set
    -- on replies.
    thread_id INTEGER REFERENCES tasks(id),
    -- The Claude Code session this exact task ran (or resumed) in. The
    -- next reply in the same thread resumes whichever task in that
    -- thread most recently captured one - see get_thread_session_id.
    session_id TEXT,
    -- Octopus routing (see router.py / models.json). lane 'agent' = the
    -- original worker (Claude Code in the project folder); 'api' = a plain
    -- model call handled by worker.api_worker_loop. A reply always inherits
    -- its thread root's lane and model_id so a conversation never hops models.
    lane TEXT NOT NULL DEFAULT 'agent',
    model_id TEXT,
    category TEXT,
    route_reason TEXT,
    -- classifier | rules | manual | thread | direct
    routed_by TEXT,
    -- Usage, filled in when the task finishes. cost_usd is an estimate.
    duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    -- Where this task sits in the shared run queue: lowest runs first. Set
    -- on insert (after everything of equal or higher priority), rewritten
    -- by a manual reorder / move-to-top. Kept on cancel so Undo can put
    -- the task back exactly where it was.
    queue_pos REAL,
    -- The last thing the agent reported doing ("Running npm test"), for
    -- the dashboard's live progress line. Full history is in task_events.
    current_step TEXT,
    -- The chat's short name, shown everywhere instead of the full prompt.
    -- Every message in a thread carries its root's title. title_by:
    -- text (quick cut of the prompt, a model may still rename it) | ai |
    -- recipe | user | kept (the model couldn't do better - leave it).
    title TEXT,
    title_by TEXT,
    -- Cached "Summarise" output on the root task; summary_key records the
    -- thread state it was made from, so it's only regenerated after the
    -- thread changes.
    summary TEXT,
    summary_key TEXT
);

-- What an agent task did, step by step (one row per tool call, parsed
-- from Claude Code's stream-json output), plus approval/decision notes.
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    -- tool | approval | note
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);

-- Saved prompts. target: 'any' (whichever project you run it in), 'all'
-- (every active project), or a project id as text.
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'any',
    created_at TEXT NOT NULL
);

-- A prompt queued automatically at a time of day. project_id NULL means
-- every active project. cadence: once | daily | weekdays | weekly
-- (weekday 0 = Monday). next_run_at is UTC; the worker fires anything due,
-- so a schedule missed while the Mac slept runs once as soon as it wakes.
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    prompt TEXT NOT NULL,
    project_id INTEGER REFERENCES projects(id),
    cadence TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    weekday INTEGER,
    model TEXT NOT NULL DEFAULT 'auto',
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);

-- Per-project preferences from the dashboard's Settings tab. A project
-- without a row uses PROJECT_SETTINGS_DEFAULTS.
CREATE TABLE IF NOT EXISTS project_settings (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    default_priority INTEGER NOT NULL DEFAULT 0,
    notify_approvals INTEGER NOT NULL DEFAULT 1,
    notify_failures INTEGER NOT NULL DEFAULT 1,
    notify_completions INTEGER NOT NULL DEFAULT 1,
    quiet_hours INTEGER NOT NULL DEFAULT 0,
    -- JSON list of command prefixes the approval server allows without asking
    allow_commands TEXT NOT NULL DEFAULT '[]',
    -- Monthly cap on real (api lane) spend; hitting it pauses the project.
    budget_usd REAL NOT NULL DEFAULT 20,
    -- Automation: move the project's status to in_progress when a task under it starts.
    auto_in_progress INTEGER NOT NULL DEFAULT 1,
    -- The Claude account (accounts.py id) this project's Claude Code tasks always
    -- run on. NULL = the default account (the claude_account setting).
    claude_account TEXT,
    -- JSON list of {name, instructions}: the places the chat's Deploy button ships this project to.
    deploy_targets TEXT NOT NULL DEFAULT '[]'
);

-- Small global key/value store (JSON values): dashboard toggles, the last
-- Claude usage snapshot, one-time migration markers.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per-model availability that outlives a single task: set when a provider
-- answers "rate limited" so the router can skip that model until then.
CREATE TABLE IF NOT EXISTS model_state (
    model_id TEXT PRIMARY KEY,
    rate_limited_until TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- A tool call (usually Bash) that needed permission while a task was
-- running, raised via approval_server.py (the --permission-prompt-tool
-- MCP server), and the human decision on it made from the dashboard.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    tool_name TEXT NOT NULL,
    tool_input TEXT NOT NULL,
    -- pending | approved | denied | timed_out
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    -- Optional reason typed when denying, passed back to Claude.
    note TEXT
);

-- A per-project backlog item. Unlike `tasks` (the actual run queue), a
-- todo just sits here until you check it and hit "Run selected" - only
-- then does it become a real task (see run_checked_todos). `position`
-- is the order you arrange them in and the order they're queued in.
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    text TEXT NOT NULL,
    position INTEGER NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0,
    -- pending | queued | done | error | needs_review | rate_limited | cancelled
    -- ('queued' onward mirrors the linked task's status - see list_todos)
    status TEXT NOT NULL DEFAULT 'pending',
    task_id INTEGER REFERENCES tasks(id),
    created_at TEXT NOT NULL,
    -- How it will run once queued. NULL = the defaults (Claude Code, acceptEdits, normal).
    model_id TEXT,
    permission_mode TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    -- Pictures attached to the item: JSON list of upload ids (see uploads.py), carried onto its task.
    images TEXT,
    -- Starred in the backlog so it stands out. Only a marker - doesn't change run order (that's `priority`).
    important INTEGER NOT NULL DEFAULT 0
);

-- What you've told Octopus about a project: guidelines, requirements, plain
-- notes. Added one at a time from the project's Notes tab, and handed to every
-- task run for that project (see project_notes_prompt).
CREATE TABLE IF NOT EXISTS project_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    -- guideline | requirement | note
    kind TEXT NOT NULL DEFAULT 'note',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Pictures in the note: JSON list of upload ids (see uploads.py), shown to every run with the text.
    images TEXT
);

-- Files kept under a project (see documents.py). Only the record is here; the
-- bytes are on disk under `stored`.
CREATE TABLE IF NOT EXISTS project_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    stored TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Personal assistant (see assistant/). One row per connected account - a
-- provider can have several (two Google accounts). id is the provider's id for
-- its first account ('google') and '<provider>-<hex>' for the others; it is
-- also the account's name in the token store. The OAuth tokens themselves are
-- NOT here - they live in the Keychain (assistant/token_store.py); this is
-- only what the dashboard may show.
-- status: connected | reauth_required | disconnected
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    account TEXT,
    status TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '[]',
    connected_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- Every external action the assistant took or asked to take. No message
-- bodies and no tokens: `target` is a short label (recipient, event title,
-- search query) and `ref` an opaque provider id (a draft id, an event id).
-- approval: not_required | requested | approved | denied | expired | trusted
-- result:   ok | error | pending | denied
CREATE TABLE IF NOT EXISTS assistant_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    approval TEXT NOT NULL DEFAULT 'not_required',
    result TEXT NOT NULL DEFAULT 'ok',
    error TEXT,
    task_id INTEGER,
    approval_id INTEGER,
    ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_assistant_activity_created ON assistant_activity(created_at);

-- What a conversation has been talking about (the events and emails a tool
-- just returned, with their ids), so "move it to 4" can be resolved. One row
-- per chat, keyed by the thread's root task.
CREATE TABLE IF NOT EXISTS assistant_context (
    thread_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Local reminders: fired by the scheduler loop as a push to the phone.
-- status: pending | fired | cancelled
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    fired_at TEXT,
    task_id INTEGER
);

-- Octopus's routing log: one row per request it understood - what was asked,
-- the plan it made (classification, project, agent, model, tools, permissions,
-- how each candidate model scored) and what became of it. The Routing page and
-- the plan preview read this; `plan` is the JSON built by octopus/plan.py.
-- status: proposed (waiting for Run / Cancel) | needs_input (e.g. which
-- project?) | queued (a task was made) | done (answered on the spot) |
-- cancelled | expired
CREATE TABLE IF NOT EXISTS routing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- dashboard | voice | api | shortcut | schedule | recipe
    source TEXT NOT NULL DEFAULT 'api',
    request TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    category TEXT,
    agent_id TEXT,
    model_id TEXT,
    project_id INTEGER,
    risk TEXT,
    plan TEXT NOT NULL,
    task_id INTEGER,
    -- auto (Octopus chose) | user (chose or overrode in the plan card)
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_routing_created ON routing(created_at);

-- Model calls that aren't a task of their own - naming a chat, summarising
-- it, classifying a request, polishing a prompt - so the Token usage page
-- can show what they burn too. A task's own usage stays on the task row.
-- kind: title | summary | classifier | polish
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    model_id TEXT,
    task_id INTEGER,
    project_id INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_log_created ON usage_log(created_at);

-- The project calendar and timelines: deadlines, milestones, meetings, demos,
-- dated tasks (see timeline.py, which owns the rules). date is the local
-- calendar day (YYYY-MM-DD), time an optional local HH:MM. remind_days: JSON
-- list of how many days ahead to push an alert (NULL = the kind's defaults);
-- alerts_sent: JSON list of alert keys already pushed ("d3", "soon", "overdue";
-- a repeating item's are prefixed with the day they were for, "2026-10-08:d1").
-- repeat_days: NULL = once, else it happens every that many days from date (the
-- first one) up to repeat_until (a YYYY-MM-DD, NULL = no end). repeat_rule: JSON,
-- any other way of repeating (weekdays, chosen weekdays, monthly; see timeline.rule_of).
-- status: open | done | cancelled
CREATE TABLE IF NOT EXISTS project_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL DEFAULT 'deadline',
    title TEXT NOT NULL,
    notes TEXT,
    date TEXT NOT NULL,
    time TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    remind_days TEXT,
    alerts_sent TEXT NOT NULL DEFAULT '[]',
    -- dashboard | octopus | api
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    done_at TEXT,
    repeat_days INTEGER,
    repeat_until TEXT,
    repeat_rule TEXT,
    -- A meeting's Google Calendar invite (see invites.py): guests is a JSON list of email
    -- addresses invited besides its contacts, duration its length in minutes (NULL = an
    -- hour); google_event_id / meet_link / invite_link are set once the invite has gone
    -- out, invite_error when the last attempt to send or update it failed. google_account
    -- is the connected Google account (a connections id) the invite went out from - NULL
    -- for invites from before several accounts could be connected, which used 'google'.
    guests TEXT,
    duration INTEGER,
    google_event_id TEXT,
    meet_link TEXT,
    invite_link TEXT,
    invite_error TEXT,
    google_account TEXT
);
CREATE INDEX IF NOT EXISTS idx_project_events_date ON project_events(date);
-- Who a meeting on the timeline is with (CRM contacts).
CREATE TABLE IF NOT EXISTS event_contacts (
    event_id INTEGER NOT NULL REFERENCES project_events(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    PRIMARY KEY (event_id, contact_id)
);

-- A project's phases (see phases.py, which owns the rules): ordered stages of work.
-- Todos are assigned to a phase (todos.phase_id); autopilot feeds a phase's todos to
-- the queue one at a time. start_date / end_date are optional local days (YYYY-MM-DD).
-- status: planned | active | paused | done. blocked_note: why autopilot stopped on it.
CREATE TABLE IF NOT EXISTS phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    goal TEXT,
    description TEXT,
    position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    start_date TEXT,
    end_date TEXT,
    blocked_note TEXT,
    -- JSON list of alert keys already pushed for this phase ("started", "done", "blocked", "late")
    alerts_sent TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_phases_project ON phases(project_id, position);

-- CRM clients (accounts) and their contacts (see clients.py, which checks every value). A project
-- belongs to at most one client (projects.client_id); contacts are linked to projects through
-- project_contacts, where role is 'key' (at most one per project) or 'stakeholder'.
-- status: prospect | active | past. color "#rrggbb" or NULL; logo: a stored name (project_images.py).
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    industry TEXT,
    website TEXT,
    address TEXT,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'prospect',
    color TEXT,
    logo TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
-- preferred_method: email | phone | text | whatsapp, or NULL. client_id NULL = not tied to a client.
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id),
    name TEXT NOT NULL,
    role TEXT,
    email TEXT,
    phone TEXT,
    notes TEXT NOT NULL DEFAULT '',
    preferred_method TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_client ON contacts(client_id);
CREATE TABLE IF NOT EXISTS project_contacts (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    role TEXT NOT NULL DEFAULT 'stakeholder',
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, contact_id)
);

-- CRM activities and follow-ups (see activities.py, which checks every value and builds the
-- timelines). Each is logged on exactly one of a project, a client or a contact.
-- kind: call | meeting | email | note | decision, or 'stage' - written by the Den itself when a
-- project moves along the pipeline (data: {"from", "to"}). occurred_at: when it happened (UTC ISO).
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    outcome TEXT,
    project_id INTEGER REFERENCES projects(id),
    client_id INTEGER REFERENCES clients(id),
    contact_id INTEGER REFERENCES contacts(id),
    data TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_project ON activities(project_id, occurred_at);
-- Who was involved in an activity (contacts).
CREATE TABLE IF NOT EXISTS activity_contacts (
    activity_id INTEGER NOT NULL REFERENCES activities(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    PRIMARY KEY (activity_id, contact_id)
);
-- A to-do with a due date (and optional local HH:MM time). status: open | done.
-- reminded_at: when its push went out (NULL = not yet; set straight away when it's added already due).
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL,
    due_time TEXT,
    project_id INTEGER REFERENCES projects(id),
    client_id INTEGER REFERENCES clients(id),
    contact_id INTEGER REFERENCES contacts(id),
    status TEXT NOT NULL DEFAULT 'open',
    done_at TEXT,
    reminded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, due_date);
-- The team (see team.py, which checks every value): the company's employees, the tasks assigned to
-- them, their time off, and the meetings they attend (event_employees, next to event_contacts).
-- status: active | on_leave | inactive. employment_type: full_time | part_time | contractor | intern.
-- Tasks here are for people (employee_tasks), not the Octopus run queue. task status: open | in_progress | done.
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    job_title TEXT,
    department TEXT,
    email TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    employment_type TEXT NOT NULL DEFAULT 'full_time',
    manager_id INTEGER REFERENCES employees(id),
    start_date TEXT,
    location TEXT,
    skills TEXT,
    notes TEXT NOT NULL DEFAULT '',
    color TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS employee_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    due_date TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    project_id INTEGER REFERENCES projects(id),
    done_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_employee_tasks_emp ON employee_tasks(employee_id, status);
-- Time off, kind: vacation | sick | other. Both days are local YYYY-MM-DD, inclusive.
CREATE TABLE IF NOT EXISTS employee_leave (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    kind TEXT NOT NULL DEFAULT 'vacation',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_employee_leave_emp ON employee_leave(employee_id, start_date);
-- Which employees attend a meeting on the timeline.
CREATE TABLE IF NOT EXISTS event_employees (
    event_id INTEGER NOT NULL REFERENCES project_events(id),
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    PRIMARY KEY (event_id, employee_id)
);
-- A browser that gets Web Push (webpush.py): the dashboard added to the Dock / Home
-- Screen and allowed to notify. endpoint is the push service URL for that one install.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

# Every query - reads included - must go through this lock. FastAPI runs
# sync route handlers in a threadpool, so without it two threads can call
# into the same sqlite3 connection at once; the C extension releases the
# GIL mid-call, and concurrent access corrupts the SQL parser's internal
# state and segfaults the process (RLock so a locked function can safely
# call another, e.g. add_task -> get_task).
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# SQL condition on a task aliased `t`: its chat has nothing running and no
# earlier message still waiting - a chat's messages run one at a time, in the
# order they were sent, each resuming the session the one before it left.
CHAT_FREE = """NOT EXISTS (
    SELECT 1 FROM tasks c
    WHERE COALESCE(c.thread_id, c.id) = COALESCE(t.thread_id, t.id) AND c.id != t.id
      AND (c.status = 'running' OR (c.id < t.id AND c.status IN ('queued', 'rate_limited'))))"""

TITLE_MAX_CHARS = 60


def quick_title(prompt: str) -> str:
    """A readable name straight from the prompt, no model needed: its first
    sentence of real text, markdown stripped, cut at a word boundary."""
    text = ""
    for line in (prompt or "").splitlines():
        line = re.sub(r"^\s*((#+|[-*>]|\d+[.)])\s+)+", "", line)
        line = re.sub(r"[*_`#]+", "", line).strip()
        if line and not re.fullmatch(r"[-=~\s]+", line):
            text = line
            break
    # End of the first sentence - a word then . ! or ?, so "1. Work" or
    # "e.g. this" don't count.
    text = re.split(r"(?<=[A-Za-z]{2}[.!?])\s", text, maxsplit=1)[0].rstrip(".!? ")
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "Untitled task"
    if len(text) > TITLE_MAX_CHARS:
        text = text[:TITLE_MAX_CHARS].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return text[0].upper() + text[1:]


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS won't add new columns to a table that
    already exists from before this column was introduced - patch those
    in by hand, guarded so this is a no-op on a fresh or already-migrated
    database."""
    # Projects predate the Work / Personal split. Everything already
    # registered starts as 'personal' so nothing leaks into a Work view
    # until it has been sorted on purpose.
    project_cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "category" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN category TEXT NOT NULL DEFAULT 'personal'")
    if "status" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'not_started'")
    # Hidden: kept out of every list and shown only on the Hidden page. Display only - a hidden
    # project's tasks still run.
    if "hidden" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    # In the Recycle bin (see den_bin.py): paused and out of every list until restored or erased.
    if "deleted_at" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT")
    # Environments (project_envs): what the project's own folder is called next to them
    # (NULL = "Main"). To-dos and phases belong to one environment like tasks do
    # (env_id NULL = the project's own folder), so each environment keeps its own plan.
    if "env_label" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN env_label TEXT")
    for table in ("todos", "phases"):
        if "env_id" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN env_id INTEGER")
    # Recycle bin for things that aren't tasks or projects (notes, recipes, schedules): a
    # snapshot of the deleted row, put back as it was on restore (see den_bin.py).
    conn.execute("""CREATE TABLE IF NOT EXISTS bin_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        -- note | recipe | schedule
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        dest TEXT,
        preview TEXT,
        data TEXT NOT NULL,
        deleted_at TEXT NOT NULL
    )""")
    # The CRM record (see crm.py, which checks every value before it is saved).
    for name, decl in PROJECT_CRM_COLUMNS:
        if name not in project_cols:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {decl}")
    # Den 2.1 CRM: how a client relationship is going - healthy / watch / at_risk (the owner's call).
    if "health" not in {r["name"] for r in conn.execute("PRAGMA table_info(clients)").fetchall()}:
        conn.execute("ALTER TABLE clients ADD COLUMN health TEXT")
    setting_cols ={r["name"] for r in conn.execute("PRAGMA table_info(project_settings)").fetchall()}
    if "auto_in_progress" not in setting_cols:
        conn.execute("ALTER TABLE project_settings ADD COLUMN auto_in_progress INTEGER NOT NULL DEFAULT 1")
    if "claude_account" not in setting_cols:
        conn.execute("ALTER TABLE project_settings ADD COLUMN claude_account TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "thread_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN thread_id INTEGER REFERENCES tasks(id)")
    if "session_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
    for name, decl in (
        ("lane", "TEXT NOT NULL DEFAULT 'agent'"),
        ("model_id", "TEXT"),
        ("category", "TEXT"),
        ("route_reason", "TEXT"),
        ("routed_by", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("cost_usd", "REAL"),
        # The part of input_tokens that was prompt cache (read + write) - Claude Code only.
        ("cache_tokens", "INTEGER"),
        ("queue_pos", "REAL"),
        ("current_step", "TEXT"),
        # A running task that has gone quiet (worker.py's stuck watch): when its last output
        # came, and why it looks stuck. Both NULL while it is fine; cleared once it speaks again.
        ("stuck_since", "TEXT"),
        ("stuck_reason", "TEXT"),
        ("title", "TEXT"),
        ("title_by", "TEXT"),
        ("summary", "TEXT"),
        ("summary_key", "TEXT"),
        ("run_now", "INTEGER NOT NULL DEFAULT 0"),
        # 1 = held by the user: a queued task is skipped by the worker until
        # played, a running one is frozen mid-run. Status doesn't change.
        ("paused", "INTEGER NOT NULL DEFAULT 0"),
        ("permission_mode", "TEXT"),
        # The project environment (project_envs.id) the task runs in; NULL = the project's own folder.
        ("env_id", "INTEGER"),
        # Octopus (see octopus/): which agent handled it, the tools it was given
        # (JSON list of tool ids), the routing-log row that made it, where the
        # request came from, its risk read, the live plan the agent reported
        # (JSON list of {content, status}), the git state when it started
        # (JSON), and files it produced (JSON list, e.g. generated images).
        ("agent_id", "TEXT"),
        ("tools", "TEXT"),
        ("routing_id", "INTEGER"),
        ("source", "TEXT"),
        ("risk", "TEXT"),
        ("plan_steps", "TEXT"),
        ("git_base", "TEXT"),
        ("artifacts", "TEXT"),
        # Pictures the user attached to this message: JSON list of upload ids (see uploads.py).
        ("images", "TEXT"),
        # 1 when the owner starred this chat (set on its first message) as important.
        ("important", "INTEGER NOT NULL DEFAULT 0"),
        # Den 2.1: 1 when the run ended by asking the owner something (octopus/execution.split_waiting) -
        # the chat needs an answer, so it isn't really finished. Cleared by a reply or "It's done".
        ("awaiting_reply", "INTEGER NOT NULL DEFAULT 0"),
        # The Claude account (accounts.py id) an agent task last started on - what a
        # usage limit it hits belongs to (see claude_account_limits).
        ("claude_account", "TEXT"),
        # The account the user chose for this one task when queuing it (overrides its project's).
        ("pinned_account", "TEXT"),
        # The only account this task may run on: if the account it would run on is any other,
        # it waits (is never started or sent to Claude) until the user changes it - see set_only_account.
        ("only_account", "TEXT"),
        # "Run after": a queued task that waits until this other task has finished
        # successfully (done, or needs_review without an error) - see runnable_tasks and set_run_after.
        ("run_after", "INTEGER"),
        # Recycle bin: a deleted task has status 'deleted'; deleted_from is the
        # status it had, so restoring puts it back exactly (a queued one keeps
        # its queue_pos). See delete_task.
        ("deleted_at", "TEXT"),
        ("deleted_from", "TEXT"),
        ("hidden", "INTEGER NOT NULL DEFAULT 0"),   # shown only on the Hidden page
        # What the item is: 'task' = work that runs to completion (a fix, a feature);
        # 'message' = a question put to an AI model whose answer the owner reads. See ITEM_KINDS.
        ("kind", "TEXT NOT NULL DEFAULT 'task'"),
        # When the owner opened a message's answer (NULL = unread). Only messages use it.
        ("read_at", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
    if "kind" not in cols:
        # One-off, only the run that adds the column: items from before the split that were
        # plain model calls (api lane) were always questions, so they become messages; everything
        # else stays a task. Lane, model and queue position are left exactly as they were, and
        # answers already given count as read so nobody gets a wall of "unread".
        conn.execute("UPDATE tasks SET kind = 'message' WHERE lane = 'api'")
        conn.execute("UPDATE tasks SET read_at = COALESCE(finished_at, created_at) WHERE kind = 'message' AND status = 'done'")
    # Tasks from before titles existed: roots get a quick name from their
    # prompt (the namer loop upgrades these), replies take their root's.
    for r in conn.execute("SELECT id, prompt FROM tasks WHERE title IS NULL AND thread_id IS NULL").fetchall():
        conn.execute("UPDATE tasks SET title = ?, title_by = 'text' WHERE id = ?", (quick_title(r["prompt"]), r["id"]))
    conn.execute(
        "UPDATE tasks SET title = (SELECT r.title FROM tasks r WHERE r.id = tasks.thread_id), title_by = 'thread' "
        "WHERE title IS NULL AND thread_id IS NOT NULL"
    )
    phase_cols = {r["name"] for r in conn.execute("PRAGMA table_info(phases)").fetchall()}
    if "description" not in phase_cols:
        conn.execute("ALTER TABLE phases ADD COLUMN description TEXT")
    todo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    for name, decl in (("model_id", "TEXT"), ("permission_mode", "TEXT"), ("priority", "INTEGER NOT NULL DEFAULT 0"), ("images", "TEXT")):
        if name not in todo_cols:
            conn.execute(f"ALTER TABLE todos ADD COLUMN {name} {decl}")
    if "only_account" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN only_account TEXT")
    if "phase_id" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN phase_id INTEGER REFERENCES phases(id)")
    if "important" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN important INTEGER NOT NULL DEFAULT 0")
    note_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_notes)").fetchall()}
    if "images" not in note_cols:
        conn.execute("ALTER TABLE project_notes ADD COLUMN images TEXT")
    if "active" not in note_cols:
        # Off = kept but not given to runs (the Guidelines tab's switch).
        conn.execute("ALTER TABLE project_notes ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    # Ideas: parked thoughts for a project that never run on their own (see list_ideas).
    conn.execute("""CREATE TABLE IF NOT EXISTS project_ideas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        text TEXT NOT NULL,
        -- Small | Medium | Big
        size TEXT NOT NULL DEFAULT 'Medium',
        -- parked | queued (sent to the queue as a task) | backlog (turned into a to-do)
        status TEXT NOT NULL DEFAULT 'parked',
        task_id INTEGER,
        todo_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    ps_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_settings)").fetchall()}
    if "deploy_targets" not in ps_cols:
        conn.execute("ALTER TABLE project_settings ADD COLUMN deploy_targets TEXT NOT NULL DEFAULT '[]'")
    if "shortcuts" not in ps_cols:
        conn.execute("ALTER TABLE project_settings ADD COLUMN shortcuts TEXT NOT NULL DEFAULT '[]'")
    if "autopilot" not in ps_cols:
        conn.execute("ALTER TABLE project_settings ADD COLUMN autopilot TEXT NOT NULL DEFAULT 'manual'")
    if "autopilot_ceiling" not in ps_cols:
        conn.execute("ALTER TABLE project_settings ADD COLUMN autopilot_ceiling INTEGER NOT NULL DEFAULT 85")
    for name, decl in (("default_model", "TEXT"), ("default_mode", "TEXT"), ("run_tests", "INTEGER NOT NULL DEFAULT 0"),
                       ("auto_format", "INTEGER NOT NULL DEFAULT 0"), ("nightly_backlog", "INTEGER NOT NULL DEFAULT 0")):
        if name not in ps_cols:
            conn.execute(f"ALTER TABLE project_settings ADD COLUMN {name} {decl}")
    pe_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_events)").fetchall()}
    for name, decl in (("repeat_days", "INTEGER"), ("repeat_until", "TEXT"), ("repeat_rule", "TEXT"), ("guests", "TEXT"),
                       ("duration", "INTEGER"), ("google_event_id", "TEXT"), ("meet_link", "TEXT"), ("invite_link", "TEXT"),
                       ("invite_error", "TEXT"), ("google_account", "TEXT")):
        if name not in pe_cols:
            conn.execute(f"ALTER TABLE project_events ADD COLUMN {name} {decl}")
    # connections used to hold one account per provider (keyed by provider); now
    # a provider can have several. The existing account keeps the provider's id
    # as its own, so its token-store entry still matches.
    conn_cols = {r["name"] for r in conn.execute("PRAGMA table_info(connections)").fetchall()}
    if "id" not in conn_cols:
        conn.execute("ALTER TABLE connections RENAME TO connections_old")
        conn.execute("CREATE TABLE connections (id TEXT PRIMARY KEY, provider TEXT NOT NULL, account TEXT, "
                     "status TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '[]', connected_at TEXT, last_error TEXT, "
                     "updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO connections (id, provider, account, status, scopes, connected_at, last_error, updated_at) "
                     "SELECT provider, provider, account, status, scopes, connected_at, last_error, updated_at FROM connections_old")
        conn.execute("DROP TABLE connections_old")
    approval_cols = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    if "note" not in approval_cols:
        conn.execute("ALTER TABLE approvals ADD COLUMN note TEXT")
    # Everything queued before Octopus ran on Claude Code - say so, so usage
    # stats don't lump it under "unknown".
    conn.execute("UPDATE tasks SET model_id = 'claude-code' WHERE model_id IS NULL AND lane = 'agent'")
    # Queued tasks from before queue_pos existed keep the order the worker
    # would have run them in.
    unplaced = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('queued', 'rate_limited') AND queue_pos IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    if unplaced:
        start = conn.execute("SELECT COALESCE(MAX(queue_pos), -1) + 1 AS n FROM tasks").fetchone()["n"]
        for i, r in enumerate(unplaced):
            conn.execute("UPDATE tasks SET queue_pos = ? WHERE id = ?", (start + i, r["id"]))
    # The worker used to file every failure as needs_review; failures are
    # 'error' now (needs_review is left for work that finished but wants a
    # human look). Relabel the old failures once.
    if not _get_setting(conn, "migrated_failures_to_error"):
        conn.execute("UPDATE tasks SET status = 'error' WHERE status = 'needs_review' AND error_message IS NOT NULL")
        _set_setting(conn, "migrated_failures_to_error", True)
    if not _get_setting(conn, "recipes_seeded"):
        if not conn.execute("SELECT 1 FROM recipes LIMIT 1").fetchone():
            for name, prompt, target in DEFAULT_RECIPES:
                conn.execute("INSERT INTO recipes (name, prompt, target, created_at) VALUES (?, ?, ?, ?)",
                             (name, prompt, target, _now()))
        _set_setting(conn, "recipes_seeded", True)
    conn.commit()


# Starter recipes, seeded once into an empty recipes table (delete or edit
# them freely afterwards - they're never re-added).
DEFAULT_RECIPES = [
    ("Run tests", "Run the full test suite. Fix anything trivially broken and report the rest.", "any"),
    ("Morning status check", "Summarize git status, open TODOs and failing tests in five bullets.", "all"),
    ("Bump dependencies", "Update minor and patch deps, run tests, revert anything that breaks.", "any"),
    ("Changelog since last tag", "Write a CHANGELOG entry for every commit since the last tag.", "any"),
]


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.commit()
            _migrate(_conn)
        return _conn


# Columns that hold JSON (tasks: see _migrate; projects: tags and links) - handed to callers as Python values.
_JSON_COLUMNS = ("tools", "plan_steps", "git_base", "artifacts", "images", "tags", "links")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    for k in _JSON_COLUMNS:
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                out[k] = None
    out["unread"] = out.get("kind") == "message" and out.get("status") == "done" and not out.get("read_at")
    return out


def _get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


# ---------------------------------------------------------------- projects

# The built-in project for requests that don't belong to a repo (see
# api._general_project). Identified by name - it's created on demand.
GENERAL_PROJECT_NAME = "General"


def is_general_project(project: Optional[dict[str, Any]]) -> bool:
    return bool(project) and project["name"] == GENERAL_PROJECT_NAME

PROJECT_CATEGORIES = ("work", "personal")      # the two areas every Den starts with


def project_categories() -> tuple:
    """The workspaces a project can belong to (Den 2.1: Work, Personal and any you add) - see den_areas.py."""
    return tuple(a["id"] for a in (get_setting("areas") or []) if a.get("id")) or PROJECT_CATEGORIES

# Where a project stands, set by hand from the dashboard. The one automation:
# a project whose auto_in_progress setting is on moves to in_progress when a
# task under it starts (see _auto_in_progress).
PROJECT_STATUSES = ("not_started", "in_progress", "need_info", "on_hold", "completed", "archived")

# The CRM record on a project (crm.py validates; the Overview tab shows and edits it). Dates are
# YYYY-MM-DD, tags a JSON list of strings, links a JSON list of {label, url}. client_id stays NULL
# until clients exist. updated_at: when any of these last changed.
PROJECT_CRM_COLUMNS = (
    ("description", "TEXT NOT NULL DEFAULT ''"),
    ("client_id", "INTEGER"),
    ("priority", "TEXT NOT NULL DEFAULT 'medium'"),
    ("health", "TEXT NOT NULL DEFAULT 'on_track'"),
    ("start_date", "TEXT"),
    ("due_date", "TEXT"),
    ("completed_date", "TEXT"),
    ("contract_value", "REAL"),
    ("currency", "TEXT NOT NULL DEFAULT 'USD'"),
    ("billing_type", "TEXT"),
    ("owner", "TEXT"),
    ("tags", "TEXT NOT NULL DEFAULT '[]'"),
    ("links", "TEXT NOT NULL DEFAULT '[]'"),
    ("updated_at", "TEXT"),
    # stage: the pipeline stage (crm.STAGES). NULL until one is picked - crm.stage_of then works it out from status.
    ("stage", "TEXT"),
    # color: "#rrggbb" or NULL (the dashboard's automatic colour); image: a stored name from project_images.py.
    ("color", "TEXT"),
    ("image", "TEXT"),
    # logo_bg: "#rrggbb" or NULL - the tile behind the picture/letter, separate from the project colour.
    ("logo_bg", "TEXT"),
)

# Claude Code --permission-mode values a task may run under (Claude Code lane
# only). None on a task means DEFAULT_PERMISSION_MODE. bypassPermissions and
# dontAsk are left out on purpose: unattended, they'd skip the phone approvals.
PERMISSION_MODES = ("acceptEdits", "plan", "auto", "manual")

# What a queue item is. A task runs to completion and is judged by its outcome (done / needs
# review / error); a message is a question to an AI model, judged by its answer being read.
# Messages never change files: one that lands on Claude Code runs in "plan" (read-only) mode.
ITEM_KINDS = ("task", "message")
MESSAGE_PERMISSION_MODE = "plan"
DEFAULT_PERMISSION_MODE = "acceptEdits"


# A project with no folder (path "") is a CRM record only: notes, phases, documents and the
# calendar all work for it, but nothing ever runs in it - the API refuses to queue work and the
# worker refuses to start any (see worker._refuse_folderless). Linking a folder makes it workable.
NO_FOLDER_HINT = "Link a folder to let Octopus work on this project"


def has_folder(project: Optional[dict[str, Any]]) -> bool:
    return bool(project) and bool((project.get("path") or "").strip())


def add_project(name: str, path: str = "", category: str = "work") -> dict[str, Any]:
    """path "" makes a CRM-only project (see NO_FOLDER_HINT)."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO projects (name, path, active, created_at, category) VALUES (?, ?, 1, ?, ?)",
            (name, path, _now(), category),
        )
        conn.commit()
        return get_project(cur.lastrowid)


def add_env(project_id: int, name: str, path: str) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO project_envs (project_id, name, path, created_at) VALUES (?, ?, ?, ?)",
                           (project_id, name, path, _now()))
        conn.commit()
        return get_env(cur.lastrowid)


def get_env(env_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM project_envs WHERE id = ?", (env_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_envs(project_id: int) -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM project_envs WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_env(env_id: int, name: Optional[str] = None, path: Optional[str] = None) -> None:
    with _lock:
        conn = get_conn()
        if name is not None:
            conn.execute("UPDATE project_envs SET name = ? WHERE id = ?", (name, env_id))
        if path is not None:
            conn.execute("UPDATE project_envs SET path = ? WHERE id = ?", (path, env_id))
        conn.commit()


def delete_env(env_id: int) -> None:
    """Tasks that ran in it keep working: with no env they fall back to the project's folder
    (and so do its to-dos and phases)."""
    with _lock:
        conn = get_conn()
        for table in ("tasks", "todos", "phases"):
            conn.execute(f"UPDATE {table} SET env_id = NULL WHERE env_id = ?", (env_id,))
        conn.execute("DELETE FROM project_envs WHERE id = ?", (env_id,))
        conn.commit()


def set_env_label(project_id: int, label: Optional[str]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET env_label = ? WHERE id = ?", (label or None, project_id))
        conn.commit()


# Status a task may be in for its project to be merged into another: anything but mid-run.
_MERGE_BLOCKING = ("running",)


def merge_project_into(target_id: int, source_id: int, env_name: str) -> dict[str, Any]:
    """Fold a whole project into another as one of its environments: the source's
    folder becomes environment `env_name` of the target, and everything recorded
    under the source (chats, to-dos, phases, notes, documents, timeline, CRM
    activity, usage...) moves across, tagged with that environment where it can
    be. The folder is the same, so Claude Code sessions keep resuming. The source's
    own environments come along too. The emptied source project is then removed;
    its settings and credentials vault entries stay with the target's. Returns the
    new environment; raises ValueError when it can't merge."""
    with _lock:
        conn = get_conn()
        target = conn.execute("SELECT * FROM projects WHERE id = ?", (target_id,)).fetchone()
        source = conn.execute("SELECT * FROM projects WHERE id = ?", (source_id,)).fetchone()
        if not target or not source:
            raise ValueError("Project not found")
        if target_id == source_id:
            raise ValueError("Pick a different project to bring in")
        if not (source["path"] or "").strip():
            raise ValueError(f"{source['name']} has no folder to turn into an environment")
        busy = conn.execute(f"SELECT id FROM tasks WHERE project_id = ? AND status IN ({','.join('?' * len(_MERGE_BLOCKING))}) LIMIT 1",
                            (source_id, *_MERGE_BLOCKING)).fetchone()
        if busy:
            raise ValueError(f"{source['name']} has task #{busy['id']} running - wait for it to finish first")
        taken = {r["name"].lower() for r in conn.execute("SELECT name FROM project_envs WHERE project_id = ?", (target_id,)).fetchall()}
        taken.add((target["env_label"] or "Main").lower())
        if env_name.lower() in taken:
            raise ValueError(f"{target['name']} already has an environment called {env_name}")
        try:
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
            now = _now()
            new_env = conn.execute("INSERT INTO project_envs (project_id, name, path, created_at) VALUES (?, ?, ?, ?)",
                                   (target_id, env_name, source["path"], now)).lastrowid
            # The source's own environments come along (renamed if the name is taken).
            for e in conn.execute("SELECT * FROM project_envs WHERE project_id = ?", (source_id,)).fetchall():
                name = e["name"]
                if name.lower() in taken | {env_name.lower()}:
                    name = f"{env_name} {e['name']}"
                taken.add(name.lower())
                conn.execute("UPDATE project_envs SET project_id = ?, name = ? WHERE id = ?", (target_id, name, e["id"]))
            # Work tied to the source's folder runs in the new environment from now on.
            for table in ("tasks", "todos", "phases"):
                conn.execute(f"UPDATE {table} SET env_id = ? WHERE project_id = ? AND env_id IS NULL", (new_env, source_id))
            # The source's phases go after the target's, in their own order.
            offset = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM phases WHERE project_id = ?", (target_id,)).fetchone()[0]
            conn.execute("UPDATE phases SET position = position + ? WHERE project_id = ?", (offset, source_id))
            offset = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM todos WHERE project_id = ?", (target_id,)).fetchone()[0]
            conn.execute("UPDATE todos SET position = position + ? WHERE project_id = ?", (offset, source_id))
            for table in ("tasks", "todos", "phases", "project_notes", "project_documents", "project_events", "project_ideas",
                          "schedules", "activities", "followups", "checklist_items", "employee_tasks", "routing", "usage_log",
                          "pad_notes", "den_credentials"):
                if table in have:
                    conn.execute(f"UPDATE {table} SET project_id = ? WHERE project_id = ?", (target_id, source_id))
            # Link tables: keep the target's row where both have one.
            for table in ("project_contacts", "den_project_link_pins"):
                if table in have:
                    conn.execute(f"UPDATE OR IGNORE {table} SET project_id = ? WHERE project_id = ?", (target_id, source_id))
                    conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (source_id,))
            if "project_settings" in have:
                conn.execute("DELETE FROM project_settings WHERE project_id = ?", (source_id,))
            # A client the target doesn't have yet comes along with the work.
            if source["client_id"] and not target["client_id"]:
                conn.execute("UPDATE projects SET client_id = ? WHERE id = ?", (source["client_id"], target_id))
            conn.execute("DELETE FROM projects WHERE id = ?", (source_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _row_to_dict(conn.execute("SELECT * FROM project_envs WHERE id = ?", (new_env,)).fetchone())


def task_workdir(task: dict[str, Any], project: dict[str, Any]) -> str:
    """The folder a task runs in: its environment's, else the project's own."""
    env = get_env(task["env_id"]) if task.get("env_id") else None
    return env["path"] if env and env["project_id"] == project["id"] else project["path"]


def list_projects(active_only: bool = False, include_deleted: bool = False) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        q = "SELECT * FROM projects WHERE 1=1"
        if not include_deleted:
            q += " AND deleted_at IS NULL"       # in the Recycle bin
        if active_only:
            q += " AND active = 1"
        q += " ORDER BY name"
        return [_row_to_dict(r) for r in conn.execute(q).fetchall()]


def get_project(project_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_project_by_name(name: str) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return _row_to_dict(row) if row else None


def set_project_active(project_id: int, active: bool) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET active = ? WHERE id = ?", (1 if active else 0, project_id))
        conn.commit()


def set_project_category(project_id: int, category: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET category = ? WHERE id = ?", (category, project_id))
        conn.commit()


def set_project_hidden(project_id: int, hidden: bool) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET hidden = ? WHERE id = ?", (1 if hidden else 0, project_id))
        conn.commit()


def set_project_name(project_id: int, name: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
        conn.commit()


def link_project_folder(project_id: int, path: str) -> bool:
    """Give a project its folder, or point it at a different one. False if the project is gone."""
    with _lock:
        conn = get_conn()
        cur = conn.execute("UPDATE projects SET path = ? WHERE id = ?", (path, project_id))
        conn.commit()
        return cur.rowcount == 1


def project_has_active_tasks(project_id: int) -> bool:
    """Queued or running work, which must not have its folder swapped from under it."""
    with _lock:
        return get_conn().execute(
            "SELECT 1 FROM tasks WHERE project_id = ? AND status IN ('queued', 'running', 'rate_limited') LIMIT 1",
            (project_id,)).fetchone() is not None


# What a hard delete removes along with the project (all rows that are only about it). The
# project's folder on disk is never touched; work that was ever run keeps the project from being
# deleted at all (see project_delete_blocker), so no run history or spend is lost.
_PROJECT_OWNED_TABLES = ("todos", "phases", "project_notes", "project_documents", "project_events",
                         "project_envs", "project_settings", "schedules", "project_contacts", "followups")


def project_delete_blocker(project_id: int) -> Optional[str]:
    """Why this project can't be hard-deleted, or None if it can."""
    with _lock:
        ran = get_conn().execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND (started_at IS NOT NULL "
            "OR status IN ('queued', 'running', 'rate_limited', 'done', 'needs_review'))", (project_id,)).fetchone()[0]
    if ran:
        return (f"Octopus has worked on this project ({ran} task{'s' if ran != 1 else ''} in its history), so it can't be "
                "deleted - archive or hide it instead")
    return None


def delete_project(project_id: int) -> None:
    """Remove a project and everything recorded only under it. Callers check
    project_delete_blocker first (only work that never ran - tasks refused for having no folder -
    is deleted with it). Pad notes and routing history stay, just no longer tied to it."""
    with _lock:
        conn = get_conn()
        # Some tables are made on first use (pad_notes) - skip any that don't exist yet.
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        task_ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE project_id = ?", (project_id,)).fetchall()]
        for tid in task_ids:
            for table in ("task_events", "approvals"):
                if table in have:
                    conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        _drop_activities(conn, "project_id", project_id)
        conn.execute("DELETE FROM event_contacts WHERE event_id IN (SELECT id FROM project_events WHERE project_id = ?)", (project_id,))
        conn.execute("DELETE FROM event_employees WHERE event_id IN (SELECT id FROM project_events WHERE project_id = ?)", (project_id,))
        conn.execute("UPDATE employee_tasks SET project_id = NULL WHERE project_id = ?", (project_id,))
        _untie_pad_notes(conn, "project_id = ?", (project_id,))
        for table in _PROJECT_OWNED_TABLES:
            if table in have:
                conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
        for table in ("pad_notes", "checklist_items", "routing", "usage_log"):
            if table in have:
                conn.execute(f"UPDATE {table} SET project_id = NULL WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()


def set_task_hidden(task_ids: list[int], hidden: bool) -> None:
    """Hide / unhide whole chats: a root task and its replies go together."""
    with _lock:
        conn = get_conn()
        for tid in task_ids:
            conn.execute("UPDATE tasks SET hidden = ? WHERE id = ? OR thread_id = ?", (1 if hidden else 0, tid, tid))
        conn.commit()


def set_project_status(project_id: int, status: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
        conn.commit()


def update_project_record(project_id: int, fields: dict[str, Any]) -> None:
    """Save CRM fields (already checked by crm.clean). Only PROJECT_CRM_COLUMNS can be written here."""
    allowed = {name for name, _ in PROJECT_CRM_COLUMNS} - {"updated_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"not a project record field: {', '.join(sorted(unknown))}")
    if not fields:
        return
    values = {k: json.dumps(v) if k in ("tags", "links") else v for k, v in fields.items()}
    values["updated_at"] = _now()
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                     (*values.values(), project_id))
        conn.commit()


# ------------------------------------------------------- the team (see team.py)

EMPLOYEE_FIELDS = ("name", "job_title", "department", "email", "phone", "status", "employment_type", "manager_id",
                   "start_date", "location", "skills", "notes", "color")
EMPLOYEE_TASK_FIELDS = ("employee_id", "title", "notes", "due_date", "priority", "status", "project_id", "done_at")
LEAVE_FIELDS = ("employee_id", "kind", "start_date", "end_date", "note")


def add_employee(fields: dict[str, Any]) -> dict[str, Any]:
    """fields: already checked by team.clean_employee (name required)."""
    return get_employee(_insert("employees", {k: v for k, v in fields.items() if k in EMPLOYEE_FIELDS}))


def get_employee(employee_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    return dict(row) if row else None


def list_employees() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM employees ORDER BY name COLLATE NOCASE, id").fetchall()
    return [dict(r) for r in rows]


def update_employee(employee_id: int, fields: dict[str, Any]) -> None:
    _update("employees", employee_id, fields, EMPLOYEE_FIELDS)


def delete_employee(employee_id: int) -> None:
    """Remove an employee with their tasks, time off and meeting attendance. People they managed
    are left without a manager."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE employees SET manager_id = NULL, updated_at = ? WHERE manager_id = ?", (_now(), employee_id))
        conn.execute("DELETE FROM employee_tasks WHERE employee_id = ?", (employee_id,))
        conn.execute("DELETE FROM employee_leave WHERE employee_id = ?", (employee_id,))
        conn.execute("DELETE FROM event_employees WHERE employee_id = ?", (employee_id,))
        conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        conn.commit()


def add_employee_task(fields: dict[str, Any]) -> dict[str, Any]:
    return get_employee_task(_insert("employee_tasks", {k: v for k, v in fields.items() if k in EMPLOYEE_TASK_FIELDS}))


def get_employee_task(task_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM employee_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_employee_tasks(employee_id: Optional[int] = None, project_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Open work first (by due date, undated last), then finished work, newest first."""
    where, args = [], []
    if employee_id is not None:
        where.append("employee_id = ?"); args.append(employee_id)
    if project_id is not None:
        where.append("project_id = ?"); args.append(project_id)
    sql = ("SELECT * FROM employee_tasks" + (" WHERE " + " AND ".join(where) if where else "") +
           " ORDER BY (status = 'done'), CASE WHEN status = 'done' THEN 0 WHEN due_date IS NULL THEN 1 ELSE 0 END, "
           "CASE WHEN status = 'done' THEN '' ELSE COALESCE(due_date, '') END, COALESCE(done_at, '') DESC, id")
    with _lock:
        return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def update_employee_task(task_id: int, fields: dict[str, Any]) -> None:
    _update("employee_tasks", task_id, fields, EMPLOYEE_TASK_FIELDS)


def delete_employee_task(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM employee_tasks WHERE id = ?", (task_id,))
        conn.commit()


def add_leave(fields: dict[str, Any]) -> dict[str, Any]:
    return get_leave(_insert("employee_leave", {k: v for k, v in fields.items() if k in LEAVE_FIELDS}))


def get_leave(leave_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM employee_leave WHERE id = ?", (leave_id,)).fetchone()
    return dict(row) if row else None


def list_leave(employee_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM employee_leave", ()
    if employee_id is not None:
        sql, args = sql + " WHERE employee_id = ?", (employee_id,)
    with _lock:
        return [dict(r) for r in get_conn().execute(sql + " ORDER BY start_date, id", args).fetchall()]


def delete_leave(leave_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM employee_leave WHERE id = ?", (leave_id,))
        conn.commit()


def employee_meetings(employee_id: int) -> list[dict[str, Any]]:
    """The timeline meetings an employee attends, in date order."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(_EVENT_SELECT + " WHERE e.id IN (SELECT event_id FROM event_employees WHERE employee_id = ?) "
                            "ORDER BY e.date, COALESCE(e.time, ''), e.id", (employee_id,)).fetchall()
        return _event_rows(conn, rows)


# ------------------------------------------------------- clients and contacts (CRM - see clients.py)

CLIENT_FIELDS = ("name", "industry", "website", "address", "notes", "status", "color", "logo")
CONTACT_FIELDS = ("client_id", "name", "role", "email", "phone", "notes", "preferred_method")
PROJECT_CONTACT_ROLES = ("key", "stakeholder")


def _insert(table: str, fields: dict[str, Any]) -> int:
    now = _now()
    values = {**fields, "created_at": now, "updated_at": now}
    with _lock:
        conn = get_conn()
        cur = conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                           tuple(values.values()))
        conn.commit()
        return cur.lastrowid


def _update(table: str, row_id: int, fields: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ValueError(f"not a {table} field: {', '.join(sorted(unknown))}")
    if not fields:
        return
    values = {**fields, "updated_at": _now()}
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (*values.values(), row_id))
        conn.commit()


def add_client(fields: dict[str, Any]) -> dict[str, Any]:
    """fields: already checked by clients.clean_client (name required)."""
    return get_client(_insert("clients", {k: v for k, v in fields.items() if k in CLIENT_FIELDS}))


def get_client(client_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_client_by_name(name: str) -> Optional[dict[str, Any]]:
    """Names are unique regardless of case: "Acme" and "acme" are the same client."""
    with _lock:
        row = get_conn().execute("SELECT * FROM clients WHERE lower(name) = lower(?)", (name.strip(),)).fetchone()
        return _row_to_dict(row) if row else None


def list_clients() -> list[dict[str, Any]]:
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()]


def update_client(client_id: int, fields: dict[str, Any]) -> None:
    _update("clients", client_id, fields, CLIENT_FIELDS)


def client_projects(client_id: int) -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM projects WHERE client_id = ? AND deleted_at IS NULL ORDER BY name COLLATE NOCASE", (client_id,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_client(client_id: int) -> None:
    """Remove a client. Callers make sure no project belongs to it any more; its contacts stay,
    no longer tied to a client."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE contacts SET client_id = NULL, updated_at = ? WHERE client_id = ?", (_now(), client_id))
        _drop_activities(conn, "client_id", client_id)
        conn.execute("DELETE FROM followups WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        conn.commit()


def add_contact(fields: dict[str, Any]) -> dict[str, Any]:
    """fields: already checked by clients.clean_contact (name required)."""
    return get_contact(_insert("contacts", {k: v for k, v in fields.items() if k in CONTACT_FIELDS}))


def get_contact(contact_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_contacts(client_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Every contact (or one client's), each with project_ids: the projects it's linked to."""
    with _lock:
        conn = get_conn()
        q, args = "SELECT * FROM contacts", ()
        if client_id is not None:
            q, args = q + " WHERE client_id = ?", (client_id,)
        rows = [_row_to_dict(r) for r in conn.execute(q + " ORDER BY name COLLATE NOCASE", args).fetchall()]
        links: dict[int, list[int]] = {}
        for r in conn.execute("SELECT contact_id, project_id FROM project_contacts ORDER BY project_id").fetchall():
            links.setdefault(r["contact_id"], []).append(r["project_id"])
    return [{**c, "project_ids": links.get(c["id"], [])} for c in rows]


def update_contact(contact_id: int, fields: dict[str, Any]) -> None:
    _update("contacts", contact_id, fields, CONTACT_FIELDS)


def delete_contact(contact_id: int) -> None:
    """Remove a contact, its links to projects (the projects themselves are untouched) and the
    activities and follow-ups logged on it. Activities elsewhere just no longer list them."""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM project_contacts WHERE contact_id = ?", (contact_id,))
        _drop_activities(conn, "contact_id", contact_id)
        conn.execute("DELETE FROM activity_contacts WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM event_contacts WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM followups WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        conn.commit()


def project_contacts(project_id: int) -> list[dict[str, Any]]:
    """The contacts linked to a project, the key contact first, each with its project_role."""
    with _lock:
        rows = get_conn().execute(
            "SELECT c.*, pc.role AS project_role FROM project_contacts pc JOIN contacts c ON c.id = pc.contact_id "
            "WHERE pc.project_id = ? ORDER BY pc.role = 'key' DESC, c.name COLLATE NOCASE", (project_id,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def link_project_contact(project_id: int, contact_id: int, role: str = "stakeholder") -> None:
    """Link a contact to a project (or change its role there). A project has one key contact at
    most: making someone the key contact makes the previous one a stakeholder."""
    if role not in PROJECT_CONTACT_ROLES:
        raise ValueError(f"role must be one of: {', '.join(PROJECT_CONTACT_ROLES)}")
    with _lock:
        conn = get_conn()
        if role == "key":
            conn.execute("UPDATE project_contacts SET role = 'stakeholder' WHERE project_id = ? AND role = 'key' AND contact_id != ?",
                         (project_id, contact_id))
        conn.execute("INSERT INTO project_contacts (project_id, contact_id, role, created_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(project_id, contact_id) DO UPDATE SET role = excluded.role", (project_id, contact_id, role, _now()))
        conn.commit()


def unlink_project_contact(project_id: int, contact_id: int) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM project_contacts WHERE project_id = ? AND contact_id = ?", (project_id, contact_id))
        conn.commit()
        return cur.rowcount == 1


# ------------------------------------------- activities and follow-ups (CRM - see activities.py)

ACTIVITY_FIELDS = ("kind", "occurred_at", "summary", "outcome", "project_id", "client_id", "contact_id", "data")
FOLLOWUP_FIELDS = ("title", "notes", "due_date", "due_time", "project_id", "client_id", "contact_id",
                   "status", "done_at", "reminded_at")
SUBJECT_COLUMNS = ("project_id", "client_id", "contact_id")


def _drop_activities(conn: sqlite3.Connection, column: str, value: int) -> None:
    """Delete the activities logged on one project / client / contact (column), with who was in them."""
    assert column in SUBJECT_COLUMNS
    conn.execute(f"DELETE FROM activity_contacts WHERE activity_id IN (SELECT id FROM activities WHERE {column} = ?)", (value,))
    conn.execute(f"DELETE FROM activities WHERE {column} = ?", (value,))


def _activity_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        a = {k: r[k] for k in r.keys()}
        try:
            a["data"] = json.loads(a["data"]) if a.get("data") else None
        except ValueError:
            a["data"] = None
        out.append(a)
    if out:
        ids = [a["id"] for a in out]
        people: dict[int, list[int]] = {}
        for r in conn.execute(f"SELECT activity_id, contact_id FROM activity_contacts WHERE activity_id IN ({','.join('?' * len(ids))}) "
                              "ORDER BY contact_id", ids).fetchall():
            people.setdefault(r["activity_id"], []).append(r["contact_id"])
        for a in out:
            a["contact_ids"] = people.get(a["id"], [])
    return out


def _set_activity_contacts(conn: sqlite3.Connection, activity_id: int, contact_ids: list[int]) -> None:
    conn.execute("DELETE FROM activity_contacts WHERE activity_id = ?", (activity_id,))
    for cid in dict.fromkeys(contact_ids):
        conn.execute("INSERT INTO activity_contacts (activity_id, contact_id) VALUES (?, ?)", (activity_id, cid))


def add_crm_activity(fields: dict[str, Any], contact_ids: Optional[list[int]] = None) -> dict[str, Any]:
    """fields: already checked by activities.clean_activity (or written by the Den: a stage change)."""
    values = {k: (json.dumps(v) if k == "data" and v is not None else v) for k, v in fields.items() if k in ACTIVITY_FIELDS}
    values["created_at"] = values["updated_at"] = _now()
    with _lock:
        conn = get_conn()
        cur = conn.execute(f"INSERT INTO activities ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                           tuple(values.values()))
        _set_activity_contacts(conn, cur.lastrowid, contact_ids or [])
        conn.commit()
        return get_crm_activity(cur.lastrowid)


def get_crm_activity(activity_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        rows = _activity_rows(conn, conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchall())
        return rows[0] if rows else None


def update_crm_activity(activity_id: int, fields: dict[str, Any], contact_ids: Optional[list[int]] = None) -> None:
    """contact_ids None leaves who was involved as it is; a list replaces it."""
    unknown = set(fields) - set(ACTIVITY_FIELDS)
    if unknown:
        raise ValueError(f"not an activity field: {', '.join(sorted(unknown))}")
    values = {**{k: (json.dumps(v) if k == "data" and v is not None else v) for k, v in fields.items()}, "updated_at": _now()}
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE activities SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (*values.values(), activity_id))
        if contact_ids is not None:
            _set_activity_contacts(conn, activity_id, contact_ids)
        conn.commit()


def delete_crm_activity(activity_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM activity_contacts WHERE activity_id = ?", (activity_id,))
        conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        conn.commit()


def _subject_where(project_ids: Optional[list[int]], client_ids: Optional[list[int]], contact_ids: Optional[list[int]],
                   involved: bool = False) -> tuple[str, list[Any]]:
    """'(project_id IN (...) OR client_id IN (...) OR ...)' for the ids given (None: that column isn't
    asked about). involved: an activity whose contacts include one of contact_ids matches too."""
    parts, args = [], []
    for col, ids in (("project_id", project_ids), ("client_id", client_ids), ("contact_id", contact_ids)):
        if ids:
            parts.append(f"{col} IN ({','.join('?' * len(ids))})")
            args += list(ids)
    if involved and contact_ids:
        parts.append(f"id IN (SELECT activity_id FROM activity_contacts WHERE contact_id IN ({','.join('?' * len(contact_ids))}))")
        args += list(contact_ids)
    return ("(" + " OR ".join(parts) + ")" if parts else "0"), args


def list_crm_activities(project_ids: Optional[list[int]] = None, client_ids: Optional[list[int]] = None,
                    contact_ids: Optional[list[int]] = None, limit: int = 200) -> list[dict[str, Any]]:
    """Activities logged on any of these projects / clients / contacts, or with one of the contacts
    involved - newest first."""
    where, args = _subject_where(project_ids, client_ids, contact_ids, involved=True)
    with _lock:
        conn = get_conn()
        rows = conn.execute(f"SELECT * FROM activities WHERE {where} ORDER BY occurred_at DESC, id DESC LIMIT ?",
                            (*args, int(limit))).fetchall()
        return _activity_rows(conn, rows)


def add_followup(fields: dict[str, Any]) -> dict[str, Any]:
    """fields: already checked by activities.clean_followup."""
    return get_followup(_insert("followups", {k: v for k, v in fields.items() if k in FOLLOWUP_FIELDS}))


def get_followup(followup_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM followups WHERE id = ?", (followup_id,)).fetchone()
        return {k: row[k] for k in row.keys()} if row else None


def update_followup(followup_id: int, fields: dict[str, Any]) -> None:
    _update("followups", followup_id, fields, FOLLOWUP_FIELDS)


def delete_followup(followup_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM followups WHERE id = ?", (followup_id,))
        conn.commit()


def list_followups(statuses: Optional[list[str]] = None, project_ids: Optional[list[int]] = None,
                   client_ids: Optional[list[int]] = None, contact_ids: Optional[list[int]] = None,
                   done_since: Optional[str] = None) -> list[dict[str, Any]]:
    """Follow-ups, soonest due first. With any ids: only those on these projects / clients / contacts.
    done_since (UTC ISO): leave out the ones done before then."""
    conds, args = [], []
    if statuses:
        conds.append(f"status IN ({','.join('?' * len(statuses))})")
        args += list(statuses)
    if project_ids is not None or client_ids is not None or contact_ids is not None:
        where, more = _subject_where(project_ids, client_ids, contact_ids)
        conds.append(where)
        args += more
    if done_since:
        conds.append("(status != 'done' OR done_at >= ?)")
        args.append(done_since)
    q = "SELECT * FROM followups" + (" WHERE " + " AND ".join(conds) if conds else "")
    with _lock:
        rows = get_conn().execute(q + " ORDER BY due_date, COALESCE(due_time, '99:99'), id", args).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]


def finished_tasks(project_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """A project's finished work (done or waiting for a review), newest first - for its activity timeline.
    Hidden chats and the recycle bin stay out."""
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM tasks WHERE project_id = ? AND status IN ('done', 'needs_review') AND finished_at IS NOT NULL "
            "AND COALESCE(hidden, 0) = 0 AND deleted_at IS NULL ORDER BY finished_at DESC LIMIT ?", (project_id, int(limit))).fetchall()
        return [_row_to_dict(r) for r in rows]


def last_activity_times() -> dict[int, list[str]]:
    """Per project, the latest time (UTC ISO strings, one per source) anything happened on it: an
    activity logged (stage changes included), a follow-up added or done, a task finished.
    activities.last_activity picks the latest."""
    out: dict[int, list[str]] = {}
    with _lock:
        conn = get_conn()
        for q in ("SELECT project_id AS pid, MAX(occurred_at) AS at FROM activities WHERE project_id IS NOT NULL GROUP BY project_id",
                  "SELECT project_id AS pid, MAX(COALESCE(done_at, created_at)) AS at FROM followups WHERE project_id IS NOT NULL GROUP BY project_id",
                  "SELECT project_id AS pid, MAX(finished_at) AS at FROM tasks WHERE finished_at IS NOT NULL AND deleted_at IS NULL GROUP BY project_id"):
            for r in conn.execute(q).fetchall():
                if r["at"]:
                    out.setdefault(r["pid"], []).append(r["at"])
    return out


# ------------------------------------------------------------------- tasks

def _queue_insert_pos(conn: sqlite3.Connection, priority: int) -> float:
    """queue_pos for a new task: after every queued task of the same or
    higher priority, ahead of anything lower - so priority still decides
    by default, and a manual reorder is only ever a starting point."""
    rows = conn.execute(
        "SELECT queue_pos, priority FROM tasks WHERE status = 'queued' AND queue_pos IS NOT NULL "
        "ORDER BY queue_pos ASC"
    ).fetchall()
    prev = None
    for r in rows:
        if r["priority"] < priority:
            return (prev + r["queue_pos"]) / 2 if prev is not None else r["queue_pos"] - 1
        prev = r["queue_pos"]
    return prev + 1 if prev is not None else 0.0


def _queue_front_pos(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT MIN(queue_pos) AS m FROM tasks WHERE status = 'queued'").fetchone()
    return row["m"] - 1 if row["m"] is not None else 0.0


def _reply_insert_pos(conn: sqlite3.Connection) -> float:
    """queue_pos for a reply in an existing chat: it's part of that chat,
    not a new task, so by default it's next in line: ahead of every queued
    new task, behind replies already waiting there (so replies keep the
    order they were sent). It's an ordinary queue item after that - it can
    be moved anywhere or started right away like any other."""
    rows = conn.execute(
        "SELECT queue_pos, thread_id FROM tasks WHERE status = 'queued' AND queue_pos IS NOT NULL "
        "ORDER BY queue_pos ASC"
    ).fetchall()
    prev = None
    for r in rows:
        if r["thread_id"] is None:
            return (prev + r["queue_pos"]) / 2 if prev is not None else r["queue_pos"] - 1
        prev = r["queue_pos"]
    return prev + 1 if prev is not None else 0.0


# Called with each task add_task creates, after it's saved - the worker uses it
# to start a new task straight away when its lane is free, instead of at the next poll.
task_added_hooks: list = []

# Called with {"task_id", "result_summary"} after mark_done has saved it (the Module Registry's
# TASK_COMPLETED event, den_modules/events.py). Same rule: a hook that fails never matters to the task.
task_completed_hooks: list = []


def add_task(
    project_id: int,
    prompt: str,
    priority: int = 0,
    thread_id: Optional[int] = None,
    lane: str = "agent",
    model_id: Optional[str] = None,
    category: Optional[str] = None,
    route_reason: Optional[str] = None,
    routed_by: Optional[str] = None,
    title: Optional[str] = None,
    permission_mode: Optional[str] = None,
    agent_id: Optional[str] = None,
    tools: Optional[list[str]] = None,
    routing_id: Optional[int] = None,
    source: Optional[str] = None,
    risk: Optional[str] = None,
    images: Optional[list[str]] = None,
    env_id: Optional[int] = None,
    claude_account: Optional[str] = None,
    only_account: Optional[str] = None,
    kind: Optional[str] = None,
) -> dict[str, Any]:
    """Defaults (agent lane, claude-code) are what every pre-Octopus caller -
    the plain task endpoint, todos - has always meant. A reply takes its
    thread's title; a new chat gets `title` if given (a recipe's name),
    else a quick one from the prompt that the namer loop can improve.
    agent_id / tools / routing_id / source / risk record Octopus's decision
    (see octopus/plan.py); a task made without it just leaves them empty."""
    if model_id is None and lane == "agent":
        model_id = "claude-code"
    if kind is not None and kind not in ITEM_KINDS:
        raise ValueError(f"kind must be one of {', '.join(ITEM_KINDS)}")
    with _lock:
        conn = get_conn()
        if thread_id is not None:
            # Answering a question Octopus asked: the chat no longer waits on the owner.
            conn.execute("UPDATE tasks SET awaiting_reply = 0 WHERE (id = ? OR thread_id = ?) AND awaiting_reply = 1", (thread_id, thread_id))
            root = conn.execute("SELECT title, kind FROM tasks WHERE id = ?", (thread_id,)).fetchone()
            kind = root["kind"] if root else (kind or "task")   # a reply is the same kind as its chat
            title, title_by = (root["title"] if root and root["title"] else quick_title(prompt)), "thread"
        else:
            if kind is None:
                kind = "message" if lane == "api" else "task"    # same rule the migration applied
            if title:
                title_by = "recipe"
            else:
                title, title_by = quick_title(prompt), "text"
        if kind == "message" and lane == "agent":
            permission_mode = MESSAGE_PERMISSION_MODE            # questions never change files
        cur = conn.execute(
            "INSERT INTO tasks (project_id, prompt, priority, status, created_at, thread_id, "
            "lane, model_id, category, route_reason, routed_by, queue_pos, title, title_by, permission_mode, "
            "agent_id, tools, routing_id, source, risk, images, env_id, kind) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, prompt, priority, _now(), thread_id,
             lane, model_id, category, route_reason, routed_by,
             _reply_insert_pos(conn) if thread_id is not None else _queue_insert_pos(conn, priority),
             title, title_by, permission_mode,
             agent_id, json.dumps(tools) if tools is not None else None, routing_id, source, risk,
             json.dumps(images) if images else None, env_id, kind),
        )
        if claude_account:   # pinned by the user: the queue runs it on this account, not its project's
            conn.execute("UPDATE tasks SET pinned_account = ? WHERE id = ?", (claude_account, cur.lastrowid))
        if only_account:
            conn.execute("UPDATE tasks SET only_account = ? WHERE id = ?", (only_account, cur.lastrowid))
        conn.commit()
        task = get_task(cur.lastrowid)
    for hook in task_added_hooks:
        try:
            hook(task)
        except Exception:  # a listener must never stop the task being queued
            logging.getLogger(__name__).exception("task_added hook failed")
    return task


def queue_position(task_id: int) -> Optional[int]:
    """1-based place in line among queued tasks, or None if it isn't queued."""
    with _lock:
        conn = get_conn()
        task = conn.execute("SELECT status, queue_pos FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task or task["status"] != "queued":
            return None
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued' AND "
            "(queue_pos < ? OR (queue_pos = ? AND id < ?))",
            (task["queue_pos"], task["queue_pos"], task_id),
        ).fetchone()
        return row["n"] + 1


def reorder_queue(ordered_ids: list[int]) -> None:
    """Set the run order of queued tasks. ids not in the list (e.g. queued
    after the dashboard last refreshed) keep their relative order after them."""
    with _lock:
        conn = get_conn()
        queued = [r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE status = 'queued' ORDER BY queue_pos ASC, id ASC").fetchall()]
        wanted = [i for i in ordered_ids if i in set(queued)]
        order = wanted + [i for i in queued if i not in set(wanted)]
        for pos, task_id in enumerate(order):
            conn.execute("UPDATE tasks SET queue_pos = ? WHERE id = ?", (float(pos), task_id))
        conn.commit()


def move_to_front(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET queue_pos = ? WHERE id = ? AND status = 'queued'",
                     (_queue_front_pos(conn), task_id))
        conn.commit()


def move_after(task_id: int, after_id: Optional[int]) -> None:
    """Put a queued task right behind another queued one (None = first in line)."""
    with _lock:
        conn = get_conn()
        order = [r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE status = 'queued' ORDER BY queue_pos ASC, id ASC").fetchall()]
        if task_id not in order:
            return
        order.remove(task_id)
        order.insert(order.index(after_id) + 1 if after_id in order else 0, task_id)
    reorder_queue(order)


# What an edit may change on a task that hasn't started yet.
EDITABLE_TASK_FIELDS = ("prompt", "title", "title_by", "priority", "project_id",
                        "lane", "model_id", "category", "route_reason", "routed_by", "permission_mode",
                        "pinned_account", "only_account")


def update_queued_task(task_id: int, fields: dict[str, Any]) -> bool:
    """Edit a task that's still queued. A new priority re-files it the way a
    new task of that priority would be; a new title renames the whole chat.
    False if it had already started (or is gone)."""
    fields = {k: v for k, v in fields.items() if k in EDITABLE_TASK_FIELDS}
    with _lock:
        conn = get_conn()
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task or task["status"] != "queued":
            return False
        if "priority" in fields and fields["priority"] != task["priority"]:
            conn.execute("UPDATE tasks SET queue_pos = NULL WHERE id = ?", (task_id,))  # don't count itself
            fields["queue_pos"] = _queue_insert_pos(conn, fields["priority"])
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
        if "title" in fields and task["thread_id"] is None:
            conn.execute("UPDATE tasks SET title = ? WHERE thread_id = ?", (fields["title"], task_id))
        conn.commit()
        return True


REQUEUEABLE = ("error", "needs_review", "cancelled", "rate_limited")


def requeue_task(task_id: int, front: bool = True, prompt: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Put a finished/stuck task back in the queue as the same task (same
    thread, so an agent task resumes its session). front=True is Retry /
    "Retry now"; front=False is Undo on a cancel and restores the old spot.
    A prompt replaces the task's prompt in the same step ("Edit and retry")."""
    with _lock:
        conn = get_conn()
        task = get_task(task_id)
        if not task or task["status"] not in REQUEUEABLE:
            return task
        pos = task["queue_pos"]
        if front or pos is None:
            pos = _queue_front_pos(conn)
        conn.execute(
            "UPDATE tasks SET status = 'queued', paused = 0, error_message = NULL, resume_at = NULL, started_at = NULL, "
            "finished_at = NULL, current_step = NULL, queue_pos = ? WHERE id = ?",
            (pos, task_id),
        )
        if prompt:
            conn.execute("UPDATE tasks SET prompt = ? WHERE id = ?", (prompt, task_id))
            if task["thread_id"] is None and task.get("title_by") in ("text", "ai", "kept"):
                conn.execute("UPDATE tasks SET title = ?, title_by = 'text' WHERE id = ?", (quick_title(prompt), task_id))
        conn.commit()
        return get_task(task_id)


def mark_reviewed(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ? AND status = 'needs_review'", (task_id,))
        conn.commit()


def mark_stopped(task_id: int) -> None:
    """A running task you stopped from the dashboard."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', paused = 0, finished_at = ?, error_message = 'Stopped from the dashboard' "
            "WHERE id = ?",
            (_now(), task_id),
        )
        conn.commit()


# ------------------------------------------------------------- task events

def add_task_event(task_id: int, kind: str, text: str) -> None:
    text = text.strip()[:500]
    if not text:
        return
    with _lock:
        conn = get_conn()
        conn.execute("INSERT INTO task_events (task_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                     (task_id, kind, text, _now()))
        conn.execute("UPDATE tasks SET current_step = ? WHERE id = ?", (text[:200], task_id))
        conn.commit()


def set_current_step(task_id: int, text: str) -> None:
    """The live line alone, without an event - e.g. "Thinking" between tool calls."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET current_step = ? WHERE id = ?", (text[:200], task_id))
        conn.commit()


def set_stuck(task_id: int, since: Optional[str], reason: Optional[str]) -> None:
    """Flag a running task as stuck (since = when it last gave any output), or clear it with None."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET stuck_since = ?, stuck_reason = ? WHERE id = ?",
                     (since, reason[:300] if reason else None, task_id))
        conn.commit()


def list_task_events(task_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not task_ids:
        return {}
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            f"SELECT * FROM task_events WHERE task_id IN ({','.join('?' * len(task_ids))}) ORDER BY id ASC",
            task_ids,
        ).fetchall()
        out: dict[int, list[dict[str, Any]]] = {i: [] for i in task_ids}
        for r in rows:
            out[r["task_id"]].append(_row_to_dict(r))
        return out


def get_thread(task_id: int) -> list[dict[str, Any]]:
    """Every message in the chat this task belongs to, oldest first."""
    with _lock:
        conn = get_conn()
        task = get_task(task_id)
        if not task:
            return []
        root = task["thread_id"] or task["id"]
        rows = conn.execute(
            "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE (t.id = ? OR t.thread_id = ?) AND t.status != 'deleted' ORDER BY t.id ASC",
            (root, root),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_thread_title(root_task_id: int, title: Optional[str], title_by: str,
                     only_if_by: Optional[str] = None) -> bool:
    """Rename a chat - the root and every reply, so lists showing any one
    message show the chat's name. title=None only changes title_by.
    only_if_by skips the rename if the chat was renamed some other way
    meanwhile (e.g. the namer loop won't overwrite a recipe's name)."""
    with _lock:
        conn = get_conn()
        guard, args = (" AND title_by = ?", (only_if_by,)) if only_if_by else ("", ())
        if title is None:
            cur = conn.execute("UPDATE tasks SET title_by = ? WHERE id = ?" + guard, (title_by, root_task_id, *args))
        else:
            cur = conn.execute("UPDATE tasks SET title = ?, title_by = ? WHERE id = ?" + guard,
                               (title, title_by, root_task_id, *args))
            if cur.rowcount:
                conn.execute("UPDATE tasks SET title = ? WHERE thread_id = ?", (title, root_task_id))
        conn.commit()
        return cur.rowcount > 0


def tasks_needing_titles(limit: int = 5) -> list[dict[str, Any]]:
    """Chats still named by a quick cut of their prompt, newest first."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, prompt FROM tasks WHERE thread_id IS NULL AND title_by = 'text' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_thread_model(root_task_id: int) -> Optional[str]:
    """The model the chat's latest message was sent to. A chat can change models
    between messages, so a reply that doesn't pick one stays on this - not on
    whatever the chat started with."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT model_id FROM tasks WHERE (id = ? OR thread_id = ?) AND model_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (root_task_id, root_task_id),
        ).fetchone()
        return row["model_id"] if row else None


def set_thread_summary(root_task_id: int, summary: str, key: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET summary = ?, summary_key = ? WHERE id = ?", (summary, key, root_task_id))
        conn.commit()


def get_thread_history(root_task_id: int, before_task_id: int) -> list[dict[str, Any]]:
    """Earlier messages of a thread (root included), oldest first - what an
    api-lane model is shown as the conversation so far."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE (id = ? OR thread_id = ?) AND id < ? AND status != 'deleted' ORDER BY id ASC",
            (root_task_id, root_task_id, before_task_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_task_usage(task_id: int, duration_ms: Optional[int] = None, input_tokens: Optional[int] = None,
                   output_tokens: Optional[int] = None, cost_usd: Optional[float] = None,
                   cache_tokens: Optional[int] = None) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET duration_ms = ?, input_tokens = ?, output_tokens = ?, cost_usd = ?, cache_tokens = ? WHERE id = ?",
            (duration_ms, input_tokens, output_tokens, cost_usd, cache_tokens, task_id),
        )
        conn.commit()


def typical_run_ms(lane: str = "agent", per_project: int = 15) -> dict[Optional[int], int]:
    """Median run time of each project's last `per_project` finished tasks on
    this lane, plus the median across all of them under the key None - the
    dashboard's progress estimate for a run that hasn't written a plan."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT project_id, duration_ms FROM (SELECT project_id, duration_ms, "
            "ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY id DESC) AS n FROM tasks "
            "WHERE status = 'done' AND lane = ? AND duration_ms > 0) WHERE n <= ?",
            (lane, per_project),
        ).fetchall()

    def median(xs: list[int]) -> int:
        xs = sorted(xs)
        return xs[len(xs) // 2]

    by: dict[Optional[int], list[int]] = {}
    for r in rows:
        by.setdefault(r["project_id"], []).append(r["duration_ms"])
    out: dict[Optional[int], int] = {pid: median(v) for pid, v in by.items() if len(v) >= 3}
    if rows:
        out[None] = median([r["duration_ms"] for r in rows])
    return out


def get_thread_session_id(root_task_id: int) -> Optional[str]:
    """The session to --resume for the next message in this thread: whichever
    task in it (the root, or any reply) most recently captured a session_id.
    None means the thread hasn't produced one yet - the next task starts a
    fresh conversation."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT session_id FROM tasks "
            "WHERE (id = ? OR thread_id = ?) AND session_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (root_task_id, root_task_id),
        ).fetchone()
        return row["session_id"] if row else None


def set_task_session(task_id: int, session_id: Optional[str]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id))
        conn.commit()


def set_task_permission_mode(task_id: int, mode: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET permission_mode = ? WHERE id = ?", (mode, task_id))
        conn.commit()


def get_task(task_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_tasks(project_id: Optional[int] = None, statuses: Optional[list[str]] = None,
               kind: Optional[str] = None) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        q = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if kind:
            q += " AND kind = ?"
            params.append(kind)
        if project_id is not None:
            q += " AND project_id = ?"
            params.append(project_id)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            q += f" AND status IN ({placeholders})"
            params.extend(statuses)
        q += " ORDER BY priority DESC, created_at ASC"
        return [_row_to_dict(r) for r in conn.execute(q, params).fetchall()]


def list_all_tasks(
    limit: int = 200,
    statuses: Optional[list[str]] = None,
    project_id: Optional[int] = None,
    hidden: Optional[bool] = None,
    kind: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Most recent tasks, newest first, for the dashboard - across all projects
    unless project_id narrows it to one."""
    with _lock:
        conn = get_conn()
        q = """
            SELECT t.*, p.name AS project_name
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE 1=1
        """
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            q += f" AND t.status IN ({placeholders})"
            params.extend(statuses)
        else:
            q += " AND t.status != 'deleted'"   # the recycle bin has its own list
        if project_id is not None:
            q += " AND t.project_id = ?"
            params.append(project_id)
        if hidden is not None:
            q += " AND t.hidden = ?"
            params.append(1 if hidden else 0)
        if kind:
            q += " AND t.kind = ?"
            params.append(kind)
        q += " ORDER BY t.id DESC LIMIT ?"
        params.append(limit)
        return [_row_to_dict(r) for r in conn.execute(q, params).fetchall()]


CHAT_STATUS_GROUPS = {
    "active": ("queued", "running", "rate_limited"),
    "done": ("done",),
    "error": ("error",),
    "cancelled": ("cancelled",),
}
_SNIPPET_MARGIN = 60


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _flat(text: Optional[str], limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _snippet(text: str, terms: list[str]) -> Optional[str]:
    """The stretch of `text` around the first search term found in it."""
    flat = " ".join(text.split())
    low = flat.lower()
    hits = [i for i in (low.find(t) for t in terms) if i >= 0]
    if not hits:
        return None
    at = min(hits)
    start, end = max(0, at - _SNIPPET_MARGIN), min(len(flat), at + _SNIPPET_MARGIN * 2)
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def list_chats(q: str = "", status: str = "all", project_ids: Optional[list[int]] = None,
               lane: Optional[str] = None, limit: int = 40, offset: int = 0,
               kind: Optional[str] = None, unread: bool = False, hidden: bool = False) -> dict[str, Any]:
    """The message centre: one entry per chat (a root task and its replies),
    the chat with the most recent message first.

    q         every word must appear somewhere in the chat - its title, its
              project's name, or any prompt / reply / error in it. Case-blind.
    status    all | needs (waiting on an approval, failed or needing review) |
              active | done | error | cancelled - judged by the chat's latest message.
    project_ids / lane   narrow to those projects / one lane (agent, api, assistant).
    kind      task | message - a chat is one kind throughout (replies inherit it).
    unread    only chats with a finished message answer the owner hasn't opened yet.
    Each entry carries `preview` (the latest message) and, while searching,
    `snippet` (the newest message that matched, with a little context)."""
    terms = [w.lower() for w in q.split()][:8]
    # hidden: the archived chats instead (hidden from every list - the Message center's Archived filter).
    where, args = ["r.hidden = 1" if hidden else "r.hidden = 0 AND p.hidden = 0"], []
    for term in terms:
        where.append(
            "(r.title LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\' OR EXISTS ("
            "SELECT 1 FROM tasks x WHERE COALESCE(x.thread_id, x.id) = l.root_id AND x.status != 'deleted' AND ("
            "x.prompt LIKE ? ESCAPE '\\' OR x.result_summary LIKE ? ESCAPE '\\' OR x.error_message LIKE ? ESCAPE '\\')))"
        )
        args += [_like(term)] * 5
    if status == "needs":
        where.append(
            "(lt.status IN ('error', 'needs_review') OR lt.awaiting_reply = 1 OR EXISTS (SELECT 1 FROM approvals a JOIN tasks x ON x.id = a.task_id "
            "WHERE a.status = 'pending' AND COALESCE(x.thread_id, x.id) = l.root_id))"
        )
    elif status in CHAT_STATUS_GROUPS:
        group = CHAT_STATUS_GROUPS[status]
        where.append(f"lt.status IN ({','.join('?' * len(group))})")
        args += group
    if project_ids is not None:
        where.append(f"r.project_id IN ({','.join('?' * len(project_ids))})")
        args += project_ids
    if lane:
        where.append("r.lane = ?")
        args.append(lane)
    if kind in ITEM_KINDS:
        where.append("r.kind = ?")
        args.append(kind)
    if unread:
        where.append("EXISTS (SELECT 1 FROM tasks x WHERE COALESCE(x.thread_id, x.id) = l.root_id "
                     "AND x.kind = 'message' AND x.status = 'done' AND x.read_at IS NULL)")
    frm = (
        "FROM (SELECT COALESCE(thread_id, id) AS root_id, MAX(id) AS last_id, COUNT(*) AS turns "
        "      FROM tasks WHERE status != 'deleted' GROUP BY COALESCE(thread_id, id)) l "
        "JOIN tasks r ON r.id = l.root_id JOIN tasks lt ON lt.id = l.last_id JOIN projects p ON p.id = r.project_id "
        "WHERE " + " AND ".join(where)
    )
    with _lock:
        conn = get_conn()
        total = conn.execute("SELECT COUNT(*) " + frm, args).fetchone()[0]
        page = conn.execute(
            "SELECT l.root_id, l.last_id, l.turns, p.name AS project_name " + frm
            + " ORDER BY COALESCE(lt.finished_at, lt.started_at, lt.created_at) DESC, l.last_id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        if not page:
            return {"chats": [], "total": total}
        roots = [r["root_id"] for r in page]
        marks = ",".join("?" * len(roots))
        msgs = conn.execute(
            f"SELECT *, COALESCE(thread_id, id) AS root_id FROM tasks WHERE (id IN ({marks}) OR thread_id IN ({marks})) "
            "AND status != 'deleted' ORDER BY id",
            roots + roots,
        ).fetchall()
        waiting = {
            r["task_id"] for r in conn.execute(
                "SELECT task_id FROM approvals WHERE status = 'pending' AND task_id IN "
                f"(SELECT id FROM tasks WHERE id IN ({marks}) OR thread_id IN ({marks}))", roots + roots
            ).fetchall()
        }
    by_root: dict[int, list[Any]] = {}
    for m in msgs:
        by_root.setdefault(m["root_id"], []).append(m)

    def reply_of(m) -> Optional[str]:
        if m["status"] == "done":
            return m["result_summary"] or "(done, no summary)"
        if m["status"] in ("error", "needs_review"):
            return m["error_message"] or "Something went wrong."
        return None

    chats = []
    for row in page:
        thread = by_root[row["root_id"]]
        root, last = thread[0], thread[-1]
        reply = reply_of(last)
        preview = ({"role": "reply", "text": _flat(reply)} if reply is not None
                   else {"role": "you", "text": _flat(last["prompt"])})
        snippet = None
        if terms:
            for m in reversed(thread):
                for role, text in (("reply", reply_of(m)), ("you", m["prompt"])):
                    found = _snippet(text, terms) if text else None
                    if found:
                        snippet = {"role": role, "text": found, "task_id": m["id"]}
                        break
                if snippet:
                    break
        costs = [m["cost_usd"] for m in thread if m["cost_usd"] is not None]
        chats.append({
            "id": root["id"],
            "title": root["title"] or _flat(root["prompt"], 80),
            "project_id": root["project_id"],
            "project_name": row["project_name"],
            "env_id": root["env_id"],
            "lane": root["lane"],
            "model_id": root["model_id"],
            "status": last["status"],
            "needs_you": last["status"] in ("error", "needs_review") or bool(last["awaiting_reply"]) or any(m["id"] in waiting for m in thread),
            "awaiting_reply": bool(last["awaiting_reply"]),
            "turns": row["turns"],
            "last_at": last["finished_at"] or last["started_at"] or last["created_at"],
            "preview": preview,
            "snippet": snippet,
            "cost_usd": sum(costs) if costs else None,
        })
    return {"chats": chats, "total": total}


def get_next_queued_task(lane: str = "agent", exclude_models: tuple = (),
                         hold_low_priority: bool = False) -> Optional[dict[str, Any]]:
    """Next task to run in this lane, only from active projects: a
    rate_limited task whose resume_at has passed first (it was already
    mid-run), then queued tasks in queue_pos order. exclude_models skips
    tasks for models currently known to be rate-limited, so the api lane
    doesn't keep hitting a limited model. hold_low_priority leaves Low
    (priority < 0) tasks waiting - "save the session for urgent work".
    A chat's messages run one at a time and in the order they were sent, so
    a message waits while its chat has one running or an earlier one pending
    (see CHAT_FREE)."""
    found = runnable_tasks(lane, exclude_models, hold_low_priority, limit=1)
    return found[0] if found else None


def runnable_tasks(lane: str = "agent", exclude_models: tuple = (), hold_low_priority: bool = False,
                   limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Every task get_next_queued_task could pick, best first. Each carries
    project_account: its project's own Claude account, or None for the default."""
    with _lock:
        conn = get_conn()
        now = _now()
        q = f"""
            SELECT t.*, ps.claude_account AS project_account FROM tasks t
            JOIN projects p ON p.id = t.project_id
            LEFT JOIN project_settings ps ON ps.project_id = t.project_id
            WHERE p.active = 1
              AND t.lane = ?
              AND (
                    t.status = 'queued'
                    OR (t.status = 'rate_limited' AND t.resume_at IS NOT NULL AND t.resume_at <= ?)
                  )
              AND t.paused = 0
              AND {CHAT_FREE}
              AND (t.run_after IS NULL OR EXISTS (SELECT 1 FROM tasks d WHERE d.id = t.run_after
                                                  AND (d.status = 'done' OR (d.status = 'needs_review'
                                                       AND d.error_message IS NULL))))
        """
        params: list[Any] = [lane, now]
        if exclude_models:
            q += f" AND COALESCE(t.model_id, '') NOT IN ({','.join('?' * len(exclude_models))})"
            params.extend(exclude_models)
        if hold_low_priority:
            q += " AND NOT (t.status = 'queued' AND t.priority < 0)"
        q += " ORDER BY (t.status = 'rate_limited') DESC, t.queue_pos ASC, t.id ASC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [_row_to_dict(r) for r in conn.execute(q, params).fetchall()]


def _auto_in_progress(conn: sqlite3.Connection, task_id: int) -> None:
    """Call just before a task's status becomes 'running'. If this is its first
    start (a resume after a rate limit keeps started_at), and its project has
    auto_in_progress on (the default), move the project to in_progress. When that moves it along the
    pipeline too (no stage picked, so the stage follows the status), it goes on its activity timeline."""
    before = conn.execute("SELECT p.id, p.status, p.stage FROM projects p JOIN tasks t ON t.project_id = p.id WHERE t.id = ?",
                          (task_id,)).fetchone()
    cur = conn.execute(
        "UPDATE projects SET status = 'in_progress' "
        "WHERE id = (SELECT project_id FROM tasks WHERE id = ? AND started_at IS NULL) "
        "AND status != 'in_progress' "
        "AND COALESCE((SELECT auto_in_progress FROM project_settings WHERE project_id = projects.id), 1) = 1",
        (task_id,),
    )
    if cur.rowcount and before and not before["stage"]:
        import crm   # crm imports db - only needed here, long after both have loaded
        was = crm.STATUS_STAGE.get(before["status"], "lead")
        if was != "active":
            now = _now()
            conn.execute("INSERT INTO activities (kind, occurred_at, summary, project_id, data, created_at, updated_at) "
                         "VALUES ('stage', ?, ?, ?, ?, ?, ?)",
                         (now, "Moved to Active - work started", before["id"],
                          json.dumps({"from": was, "to": "active", "by": "auto"}), now, now))


def mark_running(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        _auto_in_progress(conn, task_id)
        conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, resume_at = NULL, run_now = 0, "
            "stuck_since = NULL, stuck_reason = NULL WHERE id = ?",
            (_now(), task_id),
        )
        conn.commit()


def running_task_ids(lanes: tuple[str, ...]) -> list[int]:
    with _lock:
        rows = get_conn().execute(
            f"SELECT id FROM tasks WHERE status = 'running' AND lane IN ({','.join('?' * len(lanes))}) ORDER BY id",
            lanes,
        ).fetchall()
    return [r["id"] for r in rows]


def set_only_account(task_id: int, account_id: Optional[str]) -> None:
    """Change the one account a task may run on (None = any). Naming an account
    also moves the task onto it, so the user changing it is what lets it start."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET only_account = ?, pinned_account = COALESCE(?, pinned_account) WHERE id = ?",
                     (account_id or None, account_id or None, task_id))
        conn.commit()


def reroute_task(task_id: int, model_id: str, lane: str, reason: str) -> None:
    """Failover: a task its provider couldn't answer goes back to the front of the
    queue on another model (AI Providers › "Fall back to another provider")."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET status = 'queued', model_id = ?, lane = ?, route_reason = ?, routed_by = 'failover', "
                     "started_at = NULL, resume_at = NULL, error_message = NULL, finished_at = NULL WHERE id = ?",
                     (model_id, lane, reason[:600], task_id))
        conn.commit()


def set_run_after(task_id: int, after_id: Optional[int]) -> None:
    """Make a queued task wait for another to finish successfully (None = run in
    queue order). Refuses itself and anything that would close a loop."""
    with _lock:
        conn = get_conn()
        if after_id is not None:
            seen, cur = set(), after_id
            while cur is not None:
                if cur == task_id or cur in seen:
                    raise ValueError("That would make the tasks wait for each other")
                seen.add(cur)
                row = conn.execute("SELECT run_after FROM tasks WHERE id = ?", (cur,)).fetchone()
                if row is None:
                    if cur == after_id:
                        raise ValueError("That task doesn't exist")
                    break
                cur = row["run_after"]
        conn.execute("UPDATE tasks SET run_after = ? WHERE id = ?", (after_id, task_id))
        conn.commit()


def claim_task(task_id: int, claude_account: Optional[str] = None) -> bool:
    """mark_running, but only if the task is still waiting - so the queue
    and a "Proceed now" can never both start the same task. claude_account
    records which Claude account this run is on."""
    with _lock:
        conn = get_conn()
        waiting = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND status IN ('queued', 'rate_limited') AND paused = 0", (task_id,)
        ).fetchone()
        if waiting:
            _auto_in_progress(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, resume_at = NULL, run_now = 0, "
            "stuck_since = NULL, stuck_reason = NULL, claude_account = COALESCE(?, claude_account) "
            "WHERE id = ? AND status IN ('queued', 'rate_limited') AND paused = 0",
            (_now(), claude_account, task_id),
        )
        conn.commit()
        return cur.rowcount > 0


def run_now_tasks() -> list[dict[str, Any]]:
    """Queued tasks the user asked to start right away, alongside whatever
    is already running (still one at a time within a chat)."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            f"SELECT t.* FROM tasks t JOIN projects p ON p.id = t.project_id "
            f"WHERE t.run_now = 1 AND t.status = 'queued' AND t.paused = 0 AND p.active = 1 AND {CHAT_FREE} ORDER BY t.id ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_run_now(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET run_now = 1, paused = 0 WHERE id = ? AND status = 'queued'", (task_id,))
        conn.commit()


def set_awaiting_reply(task_id: int, waiting: bool) -> None:
    """The run ended by asking the owner something (True), or the owner said it's fine as it is (False)."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET awaiting_reply = ? WHERE id = ?", (1 if waiting else 0, task_id))
        conn.commit()


def mark_done(task_id: int, result_summary: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'done', paused = 0, result_summary = ?, finished_at = ? WHERE id = ?",
            (result_summary, _now(), task_id),
        )
        conn.commit()
    for hook in task_completed_hooks:
        try:
            hook({"task_id": task_id, "result_summary": result_summary})
        except Exception:
            logging.getLogger(__name__).exception("task_completed hook failed")


def mark_read(task_id: int, read: bool = True) -> bool:
    """The owner opened a message's answer (or marks it unread again). A message is
    only "finished" for the owner once read; tasks have no read state, so False."""
    with _lock:
        conn = get_conn()
        cur = conn.execute("UPDATE tasks SET read_at = ? WHERE id = ? AND kind = 'message'",
                           (_now() if read else None, task_id))
        conn.commit()
        return cur.rowcount > 0


def set_chat_important(root_id: int, important: bool) -> None:
    """Star (or unstar) a whole chat. The mark lives on the chat's first message."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET important = ? WHERE id = ?", (1 if important else 0, root_id))
        conn.commit()


def mark_thread_read(root_id: int) -> int:
    """Everything answered in this chat counts as read. Returns how many answers that newly covered."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE tasks SET read_at = ? WHERE COALESCE(thread_id, id) = ? AND kind = 'message' "
            "AND status = 'done' AND read_at IS NULL", (_now(), root_id))
        conn.commit()
        return cur.rowcount


def unread_message_count() -> int:
    with _lock:
        return get_conn().execute(
            "SELECT COUNT(*) FROM tasks WHERE kind = 'message' AND status = 'done' AND read_at IS NULL "
            "AND hidden = 0").fetchone()[0]


def mark_error(task_id: int, error_message: str, needs_review: bool = True) -> None:
    with _lock:
        conn = get_conn()
        status = "needs_review" if needs_review else "error"
        conn.execute(
            "UPDATE tasks SET status = ?, paused = 0, error_message = ?, finished_at = ? WHERE id = ?",
            (status, error_message, _now(), task_id),
        )
        conn.commit()


def mark_rate_limited(task_id: int, resume_at_iso: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'rate_limited', paused = 0, resume_at = ? WHERE id = ?",
            (resume_at_iso, task_id),
        )
        conn.commit()


def resume_rate_limited_now(project_id: Optional[int] = None, default_account_only: bool = False) -> int:
    """Let tasks waiting out a usage limit try again now - after you move them to
    another Claude account, whose limit they haven't hit. project_id: only that
    project's (its account was changed). default_account_only: only projects
    without their own account (the default account was switched); a project
    with its own account keeps waiting for that one. Returns how many."""
    q, params = "UPDATE tasks SET resume_at = ? WHERE status = 'rate_limited'", [_now()]
    if project_id is not None:
        q += " AND project_id = ?"
        params.append(project_id)
    if default_account_only:
        q += (" AND project_id NOT IN (SELECT project_id FROM project_settings"
              " WHERE COALESCE(claude_account, '') != '')")
    with _lock:
        conn = get_conn()
        cur = conn.execute(q, params)
        conn.commit()
        return cur.rowcount


def claude_account_limits(default_account: str) -> dict[str, str]:
    """{Claude account id: ISO time its usage limit resets} for every account
    that is limited right now. Read from the agent tasks waiting it out, so
    "Retry now" or cancelling them ends the wait. A task from before accounts
    were recorded counts against default_account."""
    with _lock:
        rows = get_conn().execute(
            "SELECT COALESCE(claude_account, ?) AS account, MAX(resume_at) AS until FROM tasks "
            "WHERE status = 'rate_limited' AND lane = 'agent' AND resume_at > ? GROUP BY 1",
            (default_account, _now()),
        ).fetchall()
    return {r["account"]: r["until"] for r in rows}


def projects_on_account(account_id: str) -> list[dict[str, Any]]:
    """Projects that have this Claude account as their own."""
    with _lock:
        rows = get_conn().execute(
            "SELECT p.* FROM projects p JOIN project_settings ps ON ps.project_id = p.id "
            "WHERE ps.claude_account = ? AND p.deleted_at IS NULL ORDER BY p.name COLLATE NOCASE", (account_id,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def clear_project_account(account_id: str) -> list[int]:
    """The account was removed: its projects go back to the default account,
    and their tasks waiting on its limit may try again. Returns their ids."""
    ids = [p["id"] for p in projects_on_account(account_id)]
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE project_settings SET claude_account = NULL WHERE claude_account = ?", (account_id,))
        conn.commit()
    for pid in ids:
        resume_rate_limited_now(project_id=pid)
    return ids


PAUSABLE = ("queued", "rate_limited", "running")


def set_paused(task_id: int, paused: bool) -> bool:
    """Hold or release a task without changing its status or its place in the
    queue. False if it isn't in a state that can be held (it already finished)."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            f"UPDATE tasks SET paused = ? WHERE id = ? AND status IN ({','.join('?' * len(PAUSABLE))})",
            (int(paused), task_id, *PAUSABLE),
        )
        conn.commit()
        return cur.rowcount > 0


def cancel_task(task_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', paused = 0, finished_at = ? WHERE id = ? AND status IN ('queued', 'rate_limited')",
            (_now(), task_id),
        )
        conn.commit()


# Statuses a chat's messages may be in for it to move: nothing under way.
MOVABLE = ("queued", "cancelled", "done", "error", "needs_review")


def transfer_chat(task_id: int, project_id: int) -> list[int]:
    """Move a whole chat (whichever of its messages task_id is) to another
    project. Nothing in it may be under way. Its Claude Code session belongs to
    the old project's folder and can't be resumed from the new one, so the
    session is dropped and the next reply starts fresh there with the chat so
    far written out for it (see worker.catch_up_prompt). An environment of the
    old project is dropped too. Returns the ids moved; raises ValueError when
    it can't be moved."""
    with _lock:
        conn = get_conn()
        task = get_task(task_id)
        if not task or task["status"] == "deleted":
            raise ValueError("Task not found")
        root = task["thread_id"] or task_id
        # Replies sitting in the bin go along, so restoring one puts it back in its chat.
        rows = conn.execute("SELECT id, status FROM tasks WHERE id = ? OR thread_id = ?", (root, root)).fetchall()
        busy = [r for r in rows if r["status"] not in MOVABLE + ("deleted",)]
        if busy:
            raise ValueError(f"This chat has a {busy[0]['status']} message - wait for it to finish or stop it first")
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE tasks SET project_id = ?, env_id = NULL, session_id = NULL WHERE id IN ({marks})",
            (project_id, *ids),
        )
        conn.commit()
        return ids


# -------------------------------------------------------------- recycle bin
# Deleting a queue item doesn't erase it: it goes to the bin (status
# 'deleted', off every list and out of the worker's reach) and can be
# restored exactly as it was. The bin empties itself after BIN_DAYS.

DELETABLE = ("queued", "cancelled", "done", "error", "needs_review")
BIN_DAYS = 30


def delete_task(task_id: int) -> list[int]:
    """Move a task that isn't under way (queued, or finished one way or
    another) to the recycle bin. Deleting a chat's
    first message takes the rest of that chat with it (everything else in
    it must be deletable too). Returns the ids moved; raises ValueError
    when it can't be deleted."""
    with _lock:
        conn = get_conn()
        task = get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        if task["status"] not in DELETABLE:
            raise ValueError(f"A {task['status']} task can't be deleted" + (" - stop it first" if task["status"] == "running" else ""))
        ids = [task_id]
        if task["thread_id"] is None:
            rest = conn.execute("SELECT id, status FROM tasks WHERE thread_id = ? AND status != 'deleted'", (task_id,)).fetchall()
            if any(r["status"] not in DELETABLE for r in rest):
                raise ValueError("This chat has later messages, so its first one can't be deleted on its own")
            ids += [r["id"] for r in rest]
        now = _now()
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE tasks SET deleted_from = status, status = 'deleted', deleted_at = ?, paused = 0, run_now = 0 WHERE id IN ({marks})",
            (now, *ids),
        )
        conn.commit()
        return ids


def restore_task(task_id: int) -> list[int]:
    """Take a task (and whatever was deleted along with it) back out of the
    bin, in the state it was deleted from. A reply whose chat's first
    message is also in the bin brings that back too. Returns the ids restored."""
    with _lock:
        conn = get_conn()
        task = get_task(task_id)
        if not task or task["status"] != "deleted":
            return []
        roots = [task_id]
        if task["thread_id"] is not None:
            root = get_task(task["thread_id"])
            if root and root["status"] == "deleted":
                roots.append(root["id"])
        ids: list[int] = []
        for rid in roots:
            t = get_task(rid)
            ids.append(rid)
            if t["thread_id"] is None:   # a chat's first message: its replies deleted alongside it
                ids += [r["id"] for r in conn.execute(
                    "SELECT id FROM tasks WHERE thread_id = ? AND status = 'deleted' AND deleted_at = ?", (rid, t["deleted_at"])
                ).fetchall()]
        ids = sorted(set(ids))
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE tasks SET status = COALESCE(deleted_from, 'cancelled'), deleted_from = NULL, deleted_at = NULL "
            f"WHERE id IN ({marks}) AND status = 'deleted'",
            ids,
        )
        conn.commit()
        return ids


def _bin_batch(conn: sqlite3.Connection, task_id: int) -> list[int]:
    """A bin entry and the chat replies deleted along with it."""
    row = conn.execute("SELECT id, thread_id, deleted_at FROM tasks WHERE id = ? AND status = 'deleted'", (task_id,)).fetchone()
    if not row:
        return []
    ids = [row["id"]]
    if row["thread_id"] is None:
        ids += [r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE thread_id = ? AND status = 'deleted'", (task_id,)
        ).fetchall()]
    return ids


def _erase(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM task_events WHERE task_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM approvals WHERE task_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM tasks WHERE id IN ({marks})", ids)


def list_bin() -> list[dict[str, Any]]:
    """What's in the recycle bin, most recently deleted first - one entry per
    thing deleted (a chat's replies deleted with it ride along, counted in
    `replies`). Anything older than BIN_DAYS is erased first."""
    purge_bin(older_than_days=BIN_DAYS)
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT t.*, p.name AS project_name, "
            "(SELECT COUNT(*) FROM tasks x WHERE x.thread_id = t.id AND x.status = 'deleted') AS replies "
            "FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.status = 'deleted' AND NOT (t.thread_id IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM tasks r WHERE r.id = t.thread_id AND r.status = 'deleted')) "
            "ORDER BY t.deleted_at DESC, t.id DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def purge_task(task_id: int) -> int:
    """Erase one bin entry for good. Returns how many tasks went."""
    with _lock:
        conn = get_conn()
        ids = _bin_batch(conn, task_id)
        _erase(conn, ids)
        conn.commit()
        return len(ids)


def purge_bin(older_than_days: Optional[float] = None) -> int:
    """Empty the recycle bin - everything, or only what was deleted more
    than older_than_days ago. Returns how many tasks were erased."""
    with _lock:
        conn = get_conn()
        q, args = "SELECT id FROM tasks WHERE status = 'deleted'", []
        if older_than_days is not None:
            q += " AND deleted_at < ?"
            args.append(_utc_iso(datetime.now(timezone.utc) - timedelta(days=older_than_days)))
        ids = [r["id"] for r in conn.execute(q, args).fetchall()]
        _erase(conn, ids)
        conn.commit()
        return len(ids)


# -------------------------------------------------------------- approvals

def create_approval(task_id: int, tool_name: str, tool_input: str) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO approvals (task_id, tool_name, tool_input, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (task_id, tool_name, tool_input, _now()),
        )
        conn.commit()
        return get_approval(cur.lastrowid)


def get_approval(approval_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_pending_approvals() -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT a.*, t.prompt AS task_prompt, t.title AS task_title, t.project_id, t.model_id, t.lane FROM approvals a "
            "JOIN tasks t ON t.id = a.task_id "
            "WHERE a.status = 'pending' ORDER BY a.created_at ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def decide_approval(approval_id: int, approved: bool, note: Optional[str] = None) -> Optional[dict[str, Any]]:
    """No-op (returns the row unchanged) if this approval was already decided -
    e.g. it timed out server-side right as the dashboard's click came in."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, note = ? WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "denied", _now(), (note or "").strip()[:500] or None, approval_id),
        )
        conn.commit()
        return get_approval(approval_id)


def has_pending_approval(task_id: int) -> bool:
    with _lock:
        return get_conn().execute("SELECT 1 FROM approvals WHERE task_id = ? AND status = 'pending' LIMIT 1",
                                  (task_id,)).fetchone() is not None


def expire_pending_approvals(task_id: int) -> None:
    """A task that was stopped or killed can't use an answer any more."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE approvals SET status = 'timed_out', decided_at = ? WHERE task_id = ? AND status = 'pending'",
            (_now(), task_id),
        )
        conn.commit()


def timeout_approval(approval_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE approvals SET status = 'timed_out', decided_at = ? WHERE id = ? AND status = 'pending'",
            (_now(), approval_id),
        )
        conn.commit()


# -------------------------------------------------------------------- todos

_TODO_SELECT = """
    SELECT t.*, tk.status AS task_status, tk.result_summary AS task_result_summary,
           tk.error_message AS task_error_message
    FROM todos t LEFT JOIN tasks tk ON tk.id = t.task_id
"""


def add_todo(project_id: int, text: str, images: Optional[list[str]] = None, env_id: Optional[int] = None) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        next_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM todos WHERE project_id = ?",
            (project_id,),
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO todos (project_id, text, position, checked, status, created_at, images, env_id) "
            "VALUES (?, ?, ?, 0, 'pending', ?, ?, ?)",
            (project_id, text, next_pos, _now(), json.dumps(images) if images else None, env_id),
        )
        conn.commit()
        return get_todo(cur.lastrowid)


def get_todo(todo_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute(_TODO_SELECT + " WHERE t.id = ?", (todo_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_todos(project_id: int) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            _TODO_SELECT + " WHERE t.project_id = ? ORDER BY t.position ASC",
            (project_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_todo(todo_id: int, text: Optional[str] = None, checked: Optional[bool] = None,
                settings: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """`settings` holds any of model_id / permission_mode / priority / images -
    how the item will run once queued - and/or `important` (the backlog star).
    None as a value puts one back to its default."""
    with _lock:
        conn = get_conn()
        todo = get_todo(todo_id)
        if not todo:
            return None

        if text is not None:
            conn.execute("UPDATE todos SET text = ? WHERE id = ?", (text, todo_id))

        for k, v in (settings or {}).items():
            if k == "images":
                conn.execute("UPDATE todos SET images = ? WHERE id = ?", (json.dumps(v) if v else None, todo_id))
            elif k == "only_account":
                conn.execute("UPDATE todos SET only_account = ? WHERE id = ?", (v or None, todo_id))
            elif k == "important":
                conn.execute("UPDATE todos SET important = ? WHERE id = ?", (1 if v else 0, todo_id))
            elif k in ("model_id", "permission_mode", "priority"):
                conn.execute(f"UPDATE todos SET {k} = ? WHERE id = ?", (0 if k == "priority" and v is None else v, todo_id))

        if checked is not None and bool(checked) != bool(todo["checked"]):
            conn.execute("UPDATE todos SET checked = ? WHERE id = ?", (1 if checked else 0, todo_id))
            # Re-checking a todo that already ran (either way) stages a
            # fresh run next time "Run selected" is pressed, instead of
            # silently doing nothing because its status is no longer pending.
            if checked and todo["status"] != "pending":
                conn.execute(
                    "UPDATE todos SET status = 'pending', task_id = NULL WHERE id = ?",
                    (todo_id,),
                )

        conn.commit()
        return get_todo(todo_id)


def delete_todo(todo_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        conn.commit()


def reorder_todos(project_id: int, ordered_ids: list[int]) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        for position, todo_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE todos SET position = ? WHERE id = ? AND project_id = ?",
                (position, todo_id, project_id),
            )
        conn.commit()
        return list_todos(project_id)


# ------------------------------------------------------------- project notes

NOTE_KINDS = ("guideline", "requirement", "note")
_NOTE_HEADINGS = (("guideline", "Guidelines"), ("requirement", "Requirements"), ("note", "Notes"))


def list_project_notes(project_id: int) -> list[dict[str, Any]]:
    """Each with `used`: how many of the project's tasks have started since it was added."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT n.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = n.project_id AND t.started_at >= n.created_at) AS used "
            "FROM project_notes n WHERE n.project_id = ? ORDER BY n.id ASC", (project_id,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_project_note(note_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM project_notes WHERE id = ?", (note_id,)).fetchone()
        return _row_to_dict(row) if row else None


def add_project_note(project_id: int, text: str, kind: str = "note", images: Optional[list[str]] = None) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO project_notes (project_id, kind, text, created_at, images) VALUES (?, ?, ?, ?, ?)",
                           (project_id, kind if kind in NOTE_KINDS else "note", text, _now(), json.dumps(images) if images else None))
        conn.commit()
        return get_project_note(cur.lastrowid)


def update_project_note(note_id: int, text: Optional[str] = None, kind: Optional[str] = None,
                        images: Optional[list[str]] = None, active: Optional[bool] = None) -> Optional[dict[str, Any]]:
    """None leaves a field as it is; images=[] removes the note's pictures."""
    with _lock:
        conn = get_conn()
        if active is not None:
            conn.execute("UPDATE project_notes SET active = ? WHERE id = ?", (1 if active else 0, note_id))
        if text is not None:
            conn.execute("UPDATE project_notes SET text = ? WHERE id = ?", (text, note_id))
        if kind in NOTE_KINDS:
            conn.execute("UPDATE project_notes SET kind = ? WHERE id = ?", (kind, note_id))
        if images is not None:
            conn.execute("UPDATE project_notes SET images = ? WHERE id = ?", (json.dumps(images) if images else None, note_id))
        conn.commit()
        return get_project_note(note_id)


def delete_project_note(note_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM project_notes WHERE id = ?", (note_id,))
        conn.commit()


# ------------------------------------------------------------- project documents

def list_project_documents(project_id: int, with_stored: bool = False) -> list[dict[str, Any]]:
    """Newest first. `stored` (the on-disk name) is left out unless asked for -
    clients only need the id; documents.py needs it to find the files."""
    with _lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM project_documents WHERE project_id = ? ORDER BY id DESC", (project_id,)).fetchall()
        return [{k: v for k, v in _row_to_dict(r).items() if with_stored or k != "stored"} for r in rows]


def get_project_document(doc_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM project_documents WHERE id = ?", (doc_id,)).fetchone()
        return _row_to_dict(row) if row else None


def add_project_document(project_id: int, name: str, mime: str, size: int, stored: str) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO project_documents (project_id, name, mime, size, stored, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (project_id, name, mime, size, stored, _now()))
        conn.commit()
        doc = dict(get_project_document(cur.lastrowid))
        doc.pop("stored", None)
        return doc


def rename_project_document(doc_id: int, name: str) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE project_documents SET name = ? WHERE id = ?", (name, doc_id))
        conn.commit()
    doc = get_project_document(doc_id)
    return {k: v for k, v in doc.items() if k != "stored"} if doc else None


def delete_project_document(doc_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM project_documents WHERE id = ?", (doc_id,))
        conn.commit()


def project_notes_prompt(project_id: Optional[int]) -> str:
    """The project's notes as a block of instructions, grouped by kind, for
    whatever model is about to work on it. Empty when there are none."""
    notes = [n for n in (list_project_notes(project_id) if project_id else []) if n.get("active", 1)]
    sections = []
    for kind, heading in _NOTE_HEADINGS:
        if kind == "note":
            continue        # plain notes are the user's own - runs only get guidelines and requirements
        items = [_note_line(n) for n in notes if n["kind"] == kind]
        if items:
            sections.append(f"{heading}:\n" + "\n".join(f"- {t.replace(chr(10), chr(10) + '  ')}" for t in items))
    ps = get_project_settings(project_id) if project_id else {}
    rules = [line for on, line in (
        (ps.get("run_tests"), "Run the project's tests after you change code, and fix what you broke before calling it done."),
        (ps.get("auto_format"), "Format the files you changed with the project's own formatter / linter (e.g. prettier, eslint --fix, black)."),
    ) if on]
    if rules:
        sections.append("Project rules:\n" + "\n".join(f"- {r}" for r in rules))
    if not sections:
        return ""
    return ("The user has recorded these notes about this project. Treat them as standing "
            "instructions and keep them in mind for everything you do here.\n\n" + "\n\n".join(sections))


def _note_line(note: dict[str, Any]) -> str:
    """A note's text, then where its pictures are (Claude Code looks at them with the Read tool)."""
    paths = [str(p) for p in (uploads.path_of(i) for i in note.get("images") or []) if p]
    text = note["text"].strip()
    if paths:
        text = (text + "\n" if text else "") + "Pictures in this note (look at them with the Read tool):\n" + "\n".join(paths)
    return text


# ---------------------------------------------------- project events (timeline)

# Columns update_project_event may change (the rules - valid kinds, dates,
# alert bookkeeping - live in timeline.py).
_EVENT_FIELDS = ("kind", "title", "notes", "date", "time", "status", "remind_days", "alerts_sent", "done_at", "project_id",
                 "repeat_days", "repeat_until", "repeat_rule", "guests", "duration", "google_event_id", "meet_link",
                 "invite_link", "invite_error", "google_account")


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    for k, empty in (("remind_days", None), ("alerts_sent", []), ("repeat_rule", None), ("guests", [])):
        try:
            out[k] = json.loads(out[k]) if out.get(k) else empty
        except ValueError:
            out[k] = empty
    return out


_EVENT_SELECT = "SELECT e.*, p.name AS project_name FROM project_events e JOIN projects p ON p.id = e.project_id"


def _event_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """_event_row for each, plus contact_ids: who a meeting is with."""
    out = [_event_row(r) for r in rows]
    if out:
        ids = [e["id"] for e in out]
        people: dict[int, list[int]] = {}
        for r in conn.execute(f"SELECT event_id, contact_id FROM event_contacts WHERE event_id IN ({','.join('?' * len(ids))}) "
                              "ORDER BY contact_id", ids).fetchall():
            people.setdefault(r["event_id"], []).append(r["contact_id"])
        staff: dict[int, list[int]] = {}
        for r in conn.execute(f"SELECT event_id, employee_id FROM event_employees WHERE event_id IN ({','.join('?' * len(ids))}) "
                              "ORDER BY employee_id", ids).fetchall():
            staff.setdefault(r["event_id"], []).append(r["employee_id"])
        for e in out:
            e["contact_ids"] = people.get(e["id"], [])
            e["employee_ids"] = staff.get(e["id"], [])
    return out


def _set_event_contacts(conn: sqlite3.Connection, event_id: int, contact_ids: list[int]) -> None:
    conn.execute("DELETE FROM event_contacts WHERE event_id = ?", (event_id,))
    for cid in dict.fromkeys(contact_ids):
        conn.execute("INSERT INTO event_contacts (event_id, contact_id) VALUES (?, ?)", (event_id, cid))


def _set_event_employees(conn: sqlite3.Connection, event_id: int, employee_ids: list[int]) -> None:
    conn.execute("DELETE FROM event_employees WHERE event_id = ?", (event_id,))
    for eid in dict.fromkeys(employee_ids):
        conn.execute("INSERT INTO event_employees (event_id, employee_id) VALUES (?, ?)", (event_id, eid))


def add_project_event(project_id: int, kind: str, title: str, date: str, time: Optional[str] = None,
                      notes: Optional[str] = None, remind_days: Optional[list[int]] = None,
                      alerts_sent: Optional[list[str]] = None, source: Optional[str] = None,
                      contact_ids: Optional[list[int]] = None, repeat_days: Optional[int] = None,
                      repeat_until: Optional[str] = None,
                      repeat_rule: Optional[dict[str, Any]] = None, guests: Optional[list[str]] = None,
                      duration: Optional[int] = None, employee_ids: Optional[list[int]] = None) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        now = _now()
        cur = conn.execute(
            "INSERT INTO project_events (project_id, kind, title, notes, date, time, remind_days, alerts_sent, source, "
            "created_at, updated_at, repeat_days, repeat_until, repeat_rule, guests, duration) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, kind, title, notes, date, time, json.dumps(remind_days) if remind_days is not None else None,
             json.dumps(alerts_sent or []), source, now, now, repeat_days, repeat_until,
             json.dumps(repeat_rule) if repeat_rule else None, json.dumps(guests) if guests else None, duration))
        _set_event_contacts(conn, cur.lastrowid, contact_ids or [])
        _set_event_employees(conn, cur.lastrowid, employee_ids or [])
        conn.commit()
        return get_project_event(cur.lastrowid)


def get_project_event(event_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        rows = _event_rows(conn, conn.execute(_EVENT_SELECT + " WHERE e.id = ?", (event_id,)).fetchall())
        return rows[0] if rows else None


def list_project_events(project_id: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None,
                        statuses: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Events in date order (untimed ones first within a day). start / end are
    YYYY-MM-DD, both inclusive; a repeating item counts from its first day to
    its repeat_until (it is listed once, under its first day)."""
    where, args = [], []
    if project_id is not None:
        where.append("e.project_id = ?"); args.append(project_id)
    if start:
        where.append("(e.date >= ? OR ((e.repeat_days IS NOT NULL OR e.repeat_rule IS NOT NULL) AND (e.repeat_until IS NULL OR e.repeat_until >= ?)))")
        args += [start, start]
    if end:
        where.append("e.date <= ?"); args.append(end)
    if statuses:
        where.append(f"e.status IN ({','.join('?' * len(statuses))})"); args.extend(statuses)
    sql = _EVENT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY e.date, COALESCE(e.time, ''), e.id"
    with _lock:
        conn = get_conn()
        return _event_rows(conn, conn.execute(sql, args).fetchall())


def update_project_event(event_id: int, contact_ids: Optional[list[int]] = None,
                         employee_ids: Optional[list[int]] = None, **fields: Any) -> Optional[dict[str, Any]]:
    """contact_ids None leaves who a meeting is with as it is; a list replaces it."""
    sets, args = [], []
    for k, v in fields.items():
        if k not in _EVENT_FIELDS:
            raise ValueError(f"unknown event field {k}")
        sets.append(f"{k} = ?")
        args.append(json.dumps(v) if k in ("remind_days", "alerts_sent", "repeat_rule", "guests") and v is not None else v)
    if not sets and contact_ids is None and employee_ids is None:
        return get_project_event(event_id)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE project_events SET {''.join(s + ', ' for s in sets)}updated_at = ? WHERE id = ?", (*args, _now(), event_id))
        if contact_ids is not None:
            _set_event_contacts(conn, event_id, contact_ids)
        if employee_ids is not None:
            _set_event_employees(conn, event_id, employee_ids)
        conn.commit()
        return get_project_event(event_id)


def _untie_pad_notes(conn, where: str, args: tuple) -> None:
    """Pad notes tied to the calendar items matching `where` keep their text, just lose the tie
    (pad_notes is made on first use, and its event_id column may not exist yet)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pad_notes)").fetchall()}
    if "event_id" in cols:
        conn.execute(f"UPDATE pad_notes SET event_id = NULL WHERE event_id IN (SELECT id FROM project_events WHERE {where})", args)


def delete_project_event(event_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM event_contacts WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM event_employees WHERE event_id = ?", (event_id,))
        _untie_pad_notes(conn, "id = ?", (event_id,))
        conn.execute("DELETE FROM project_events WHERE id = ?", (event_id,))
        conn.commit()


# ------------------------------------------------------------------ phases

PHASE_STATUSES = ("planned", "active", "paused", "done")


def _phase_row(row: sqlite3.Row) -> dict[str, Any]:
    d = _row_to_dict(row)
    d["alerts_sent"] = json.loads(d.get("alerts_sent") or "[]")
    return d


def add_phase(project_id: int, name: str, goal: Optional[str] = None, start_date: Optional[str] = None,
              end_date: Optional[str] = None, description: Optional[str] = None, env_id: Optional[int] = None) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM phases WHERE project_id = ?",
                           (project_id,)).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO phases (project_id, name, goal, description, position, status, start_date, end_date, created_at, env_id) "
            "VALUES (?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?)",
            (project_id, name, goal, description, pos, start_date, end_date, _now(), env_id),
        )
        conn.commit()
        return get_phase(cur.lastrowid)


def get_phase(phase_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM phases WHERE id = ?", (phase_id,)).fetchone()
        return _phase_row(row) if row else None


def list_phases(project_id: Optional[int] = None) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        if project_id is None:
            rows = conn.execute("SELECT * FROM phases ORDER BY project_id, position").fetchall()
        else:
            rows = conn.execute("SELECT * FROM phases WHERE project_id = ? ORDER BY position",
                                (project_id,)).fetchall()
        return [_phase_row(r) for r in rows]


_PHASE_FIELDS = ("name", "goal", "description", "status", "start_date", "end_date", "blocked_note", "started_at", "finished_at")


def update_phase(phase_id: int, **fields: Any) -> Optional[dict[str, Any]]:
    """Set the given fields (None clears the nullable ones); alerts_sent takes a list."""
    with _lock:
        conn = get_conn()
        for k, v in fields.items():
            if k == "alerts_sent":
                conn.execute("UPDATE phases SET alerts_sent = ? WHERE id = ?", (json.dumps(list(v)), phase_id))
            elif k in _PHASE_FIELDS:
                conn.execute(f"UPDATE phases SET {k} = ? WHERE id = ?", (v, phase_id))
        conn.commit()
        return get_phase(phase_id)


def delete_phase(phase_id: int) -> None:
    """Remove a phase; its todos stay on the project, just unassigned."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE todos SET phase_id = NULL WHERE phase_id = ?", (phase_id,))
        conn.execute("DELETE FROM phases WHERE id = ?", (phase_id,))
        conn.commit()


def reorder_phases(project_id: int, ordered_ids: list[int]) -> list[dict[str, Any]]:
    with _lock:
        conn = get_conn()
        for pos, pid in enumerate(ordered_ids):
            conn.execute("UPDATE phases SET position = ? WHERE id = ? AND project_id = ?", (pos, pid, project_id))
        conn.commit()
    return list_phases(project_id)


def set_todo_phase(todo_id: int, phase_id: Optional[int]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE todos SET phase_id = ? WHERE id = ?", (phase_id, todo_id))
        # A phase's items run in the phase's environment.
        if phase_id:
            conn.execute("UPDATE todos SET env_id = (SELECT env_id FROM phases WHERE id = ?) WHERE id = ?", (phase_id, todo_id))
        conn.commit()


def projects_with_open_phases() -> list[dict[str, Any]]:
    """Active projects that have a phase which isn't done - what autopilot looks at."""
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM projects WHERE active = 1 AND path != '' AND id IN "
            "(SELECT project_id FROM phases WHERE status != 'done') ORDER BY id"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def run_checked_todos(project_id: int, decisions: Optional[dict[int, dict[str, Any]]] = None,
                      only_ids: Optional[list[int]] = None) -> list[dict[str, Any]]:
    """Convert every checked, not-yet-run todo (in position order) into a
    real queued task - see add_task. Already queued/done/error ones are
    left alone; re-check one (see update_todo) to stage it for a rerun.
    A todo with its own model, mode, priority or pictures carries them onto the task;
    `decisions` maps a todo id to the router's decision for its model (made
    in api.py - routing can call a model, so not under this lock). only_ids
    limits it to those todos (autopilot queues one at a time, checked or not)."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM todos WHERE project_id = ? AND status = 'pending' ORDER BY position ASC",
            (project_id,),
        ).fetchall()
        if only_ids is None:
            rows = [r for r in rows if r["checked"]]
        else:
            rows = [r for r in rows if r["id"] in set(only_ids)]
        for row in rows:
            decision = (decisions or {}).get(row["id"])
            task = add_task(
                row["project_id"], row["text"], priority=row["priority"] or 0,
                permission_mode=row["permission_mode"],
                images=json.loads(row["images"]) if row["images"] else None,
                # The item's account is the one it runs on (and the only one): picking it on the
                # backlog shouldn't leave it waiting because its project uses another.
                claude_account=row["only_account"], only_account=row["only_account"],
                env_id=row["env_id"],
                **({k: decision[k] for k in ("lane", "model_id", "category", "routed_by")}
                   | {"route_reason": decision["reason"]} if decision else {}),
            )
            conn.execute(
                "UPDATE todos SET status = 'queued', task_id = ? WHERE id = ?",
                (task["id"], row["id"]),
            )
        conn.commit()
        return list_todos(project_id)


def tasks_run_since(since: str) -> list[dict[str, Any]]:
    """Tasks that were running at some point from `since` on, with their project's folder
    (restart_control.needs_restart matches them against code edits)."""
    with _lock:
        rows = get_conn().execute(
            "SELECT t.id, t.started_at, t.finished_at, p.path FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.started_at IS NOT NULL AND (t.finished_at IS NULL OR t.finished_at >= ?) AND t.deleted_at IS NULL",
            (since,)).fetchall()
        return [dict(r) for r in rows]


def queue_status() -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        # One count per chat, by its latest message - a reply is part of its
        # chat, not another task. Except 'queued', which counts queue items
        # (a waiting reply is one), so it matches the queue the dashboard lists.
        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) as n FROM tasks "
                "WHERE id IN (SELECT MAX(id) FROM tasks WHERE status != 'deleted' GROUP BY COALESCE(thread_id, id)) GROUP BY status"
            ).fetchall()
        }
        counts.pop("queued", None)
        n_queued = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued'").fetchone()["n"]
        if n_queued:
            counts["queued"] = n_queued
        # "running" stays the agent lane's own task for older clients;
        # running_all adds whatever the api lane is doing alongside it.
        running = conn.execute(
            "SELECT * FROM tasks WHERE status = 'running' AND lane = 'agent' LIMIT 1"
        ).fetchone()
        running_all = conn.execute(
            "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC"
        ).fetchall()
        next_up = get_next_queued_task("agent") or get_next_queued_task("api")
        rate_limited = conn.execute(
            "SELECT * FROM tasks WHERE status = 'rate_limited' ORDER BY resume_at ASC LIMIT 1"
        ).fetchone()
        return {
            "counts": counts,
            "running": _row_to_dict(running) if running else None,
            "running_all": [_row_to_dict(r) for r in running_all],
            "next_up": next_up,
            "earliest_rate_limit_resume": _row_to_dict(rate_limited) if rate_limited else None,
            # Answers the owner hasn't opened yet - the message inbox's badge.
            "unread_messages": unread_message_count(),
        }


# -------------------------------------------------------------- model state

def set_model_rate_limited(model_id: str, until_iso: str, error: str = "") -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO model_state (model_id, rate_limited_until, last_error, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET rate_limited_until = excluded.rate_limited_until, "
            "last_error = excluded.last_error, updated_at = excluded.updated_at",
            (model_id, until_iso, error[:500], _now()),
        )
        conn.commit()


def clear_model_rate_limit(model_id: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE model_state SET rate_limited_until = NULL, updated_at = ? WHERE model_id = ?",
            (_now(), model_id),
        )
        conn.commit()


def model_rate_limited_until(model_id: str) -> Optional[str]:
    """ISO time this model is known to be rate-limited until, or None if it
    isn't (or the limit has already passed). For claude-code this is derived
    from the agent lane's own rate_limited tasks, which is where that limit shows up
    - the default Claude account's only: a project on another account just waits
    for its own (see claude_account_limits)."""
    with _lock:
        conn = get_conn()
        now = _now()
        if model_id == "claude-code":
            default = _get_setting(conn, "claude_account", SETTINGS_DEFAULTS["claude_account"])
            return claude_account_limits(default).get(default)
        row = conn.execute(
            "SELECT rate_limited_until FROM model_state WHERE model_id = ? AND rate_limited_until > ?",
            (model_id, now),
        ).fetchone()
        return row["rate_limited_until"] if row else None


def usage_summary(days: int = 30) -> dict[str, Any]:
    """Per-model and per-category totals over the last `days` - the data
    future routing decisions can learn from (success rate, cost, speed)."""
    with _lock:
        conn = get_conn()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def grouped(col: str) -> list[dict[str, Any]]:
            rows = conn.execute(
                f"""
                SELECT COALESCE({col}, 'unknown') AS key,
                       COUNT(*) AS tasks,
                       SUM(status = 'done') AS done,
                       SUM(status IN ('error', 'needs_review')) AS failed,
                       SUM(status = 'rate_limited') AS rate_limited,
                       AVG(duration_ms) AS avg_duration_ms,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cost_usd) AS cost_usd
                FROM tasks WHERE created_at >= ?
                GROUP BY key ORDER BY tasks DESC
                """,
                (since,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return {"days": days, "by_model": grouped("model_id"), "by_category": grouped("category")}


# ---------------------------------------------------------------- settings

# Global toggles the dashboard can change. monthly_budget_usd is the
# "of $100 this month" line - real (api lane) spend across all projects.
# Starter quick buttons (Home). Each fills the Ask box with `prompt`; `pic` also opens the
# picture picker. The list is edited from the dashboard and saved whole as the "quick_buttons" setting.
QUICK_BUTTON_DEFAULTS: list[dict[str, Any]] = [
    {"id": "q-sanskrit", "icon": "📜", "label": "Sanskrit exam paper", "pic": True,
     "prompt": "Convert the attached photo of a Sanskrit exam paper into clean, typed Devanagari text. "
               "Keep every question number, marks and instruction exactly as written, and lay it out like a printable question paper."},
    {"id": "q-photo-text", "icon": "📷", "label": "Photo to text", "pic": True,
     "prompt": "Read the attached photo and type out all the text in it, keeping the original layout and order."},
    {"id": "q-summarize", "icon": "📝", "label": "Summarize", "pic": False,
     "prompt": "Summarize this in a few short points:\n\n"},
    {"id": "q-reply", "icon": "💬", "label": "Write a reply", "pic": False,
     "prompt": "Write a polite, short reply to this message:\n\n"},
    {"id": "q-translate", "icon": "🌐", "label": "Translate", "pic": False,
     "prompt": "Translate this into English (or into Hindi if it is already in English):\n\n"},
    {"id": "q-fix", "icon": "✏️", "label": "Fix spelling", "pic": False,
     "prompt": "Fix the spelling and grammar in this, changing nothing else:\n\n"},
]

SETTINGS_DEFAULTS: dict[str, Any] = {
    "quick_buttons": QUICK_BUTTON_DEFAULTS,
    "notify_on_resume": False,       # ping once when a rate-limit pause ends
    "hold_low_when_busy": True,      # hold Low tasks while the 5-hour window is over 85%
    "sleep_mode": None,              # {"since", "until"} while "I'm going to sleep" is on - see sleep_mode_active
    "email_to": "tusshartg420@gmail.com",   # where the Den's emails go - see mailer.py
    "email_events_off": [],          # email events switched off (keys of email_templates.EVENTS) - see mailer.py
    "notify_weekly_pace": True,      # warn when the weekly limit is being used up too fast
    # Project calendar (see timeline.py): push deadline / milestone / meeting alerts, and the
    # "Good morning" briefing as a push once a day at briefing_time (local HH:MM).
    "notify_timeline": True,
    "briefing_push": True,
    "briefing_time": "08:30",
    "monthly_budget_usd": 100.0,
    "daily_budget_usd": None,        # the dashboard's "Today" API budget bar; None = no daily cap
    # Den 2.0 Settings: while "going to sleep" is on, an unanswered approval is denied after a few
    # minutes (approval_server.SLEEP_TIMEOUT_SECONDS); quiet hours for the whole workspace hold back
    # "done" pushes at night (failures and approvals still come through); the per-task time cap.
    "sleep_auto_deny": True,
    "quiet_hours": False,
    "task_timeout_min": None,        # None = TASK_TIMEOUT_SECONDS from the environment (an hour)
    "stuck_after_min": 10,           # a running task with no output for this long is flagged stuck (worker.py)
    # Token usage › Budget caps: a push at 80% of the daily / monthly API cap, and API tasks
    # held (not started) once a cap is reached - see worker.api_budget_hit.
    "budget_warn": True,
    "budget_pause": True,
    # AI Providers page: routing rules checked before Octopus picks ({when, kw, model, on}, see
    # router.matching_rule), the model for non-coding requests when no rule decides ("auto" = Octopus
    # picks), and what happens when a provider fails (see providers._each_key).
    "routing_rules": [],
    "default_model": "auto",
    "failover": {"retry": True, "other": True, "notify": True},
    # Dashboard layout that follows you across devices (Home widgets, pinned items,
    # view mode, project tab layouts...). Free-form; only the dashboard reads it.
    "ui_prefs": {},
    # Octopus (see octopus/autonomy.py). autonomy: assist | execute | autonomous.
    # plan_review: when the plan card is shown before anything runs -
    # risky (default: only for higher-risk requests) | always | never.
    "autonomy": "execute",
    "plan_review": "risky",
    # Voice (see octopus/voice/). Providers are ids from octopus.voice.
    "voice_stt": "browser",
    "voice_tts": "browser",
    "voice_tts_voice": "",           # a macOS voice name for tts 'macos'; "" = system default
    "voice_manual": "",              # a macOS voice picked by hand in the Ask box; beats the per-language voices. "" = automatic
    "voice_speak_replies": True,
    "voice_rate": 1.0,               # speaking speed, 0.5 - 2.0
    "voice_language_voices": {},     # {language code: macOS voice name} - see octopus/voice/languages.py
    # Which Claude subscription account Claude Code runs on - "mac" (this Mac's own login) or an id from accounts.json. See accounts.py.
    "claude_account": "mac",
    # Den 2.0 Smart devices: "Pause Octopus" - nothing new starts while on (see worker.queue_paused);
    # spoken commands of your own ({id, phrase, action, on, confirm}, see den_connections.run_command);
    # voice quiet hours (only pause / going to sleep work 23:00-07:00); the voice page's extra switches.
    "queue_paused": False,
    "voice_commands": [],
    "voice_quiet": False,
    "voice_readback": False,
    "voice_wake": "Octo",
    # Den 2.1 workspaces: working areas inside this one Den. A project's `category` is the id of its area;
    # the dashboard's switcher shows one area at a time (General belongs to all). See den_areas.py.
    "areas": [{"id": "work", "name": "Work", "color": "#7FB2FF"}, {"id": "personal", "name": "Personal", "color": "#FF7B5C"}],            # said before a command ("Octo, pause Octopus") - optional, stripped off
}


def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        return _get_setting(get_conn(), key, SETTINGS_DEFAULTS.get(key, default))


def set_setting(key: str, value: Any) -> None:
    with _lock:
        conn = get_conn()
        _set_setting(conn, key, value)
        conn.commit()


def get_settings() -> dict[str, Any]:
    return {k: get_setting(k) for k in SETTINGS_DEFAULTS}


# ------------------------------------------------------------ web push subscriptions

def add_push_subscription(endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> dict[str, Any]:
    """Subscribing again from the same browser just refreshes its keys."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, user_agent = excluded.user_agent",
            (endpoint, p256dh, auth, user_agent[:300], _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        return _row_to_dict(row)


def list_push_subscriptions() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM push_subscriptions ORDER BY id").fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_push_subscription(endpoint: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()


SLEEP_DEFAULT_HOURS = 8.0


def sleep_mode_active() -> bool:
    """True while "I'm going to sleep" is on. It ends by itself at "until", so a
    forgotten toggle can't leave the queue in overnight mode for good."""
    sm = get_setting("sleep_mode")
    if not sm or not sm.get("until"):
        return False
    return datetime.fromisoformat(sm["until"]) > datetime.now(timezone.utc)


def start_sleep_mode(hours: float = SLEEP_DEFAULT_HOURS) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    sm = {"since": now.isoformat(), "until": (now + timedelta(hours=hours)).isoformat()}
    set_setting("sleep_mode", sm)
    return sm


def end_sleep_mode() -> None:
    set_setting("sleep_mode", None)


# ------------------------------------------------------- project settings

PROJECT_SETTINGS_DEFAULTS: dict[str, Any] = {
    "default_priority": 0,
    "notify_approvals": True,
    "notify_failures": True,
    "notify_completions": True,
    "quiet_hours": False,
    "allow_commands": [],
    "budget_usd": 20.0,
    "auto_in_progress": True,
    "claude_account": None,   # None = the default account; "" when saving clears it back to that
    # Phase autopilot (phases.py): manual (only phases you start) | timeline (start each phase on its start date) |
    # capacity (work through the phases whenever the usage limit has room). The ceiling is
    # the weekly-usage percentage autopilot won't feed work past.
    "autopilot": "manual",
    "autopilot_ceiling": 85,
    "shortcuts": [],          # one-tap buttons: [{"label": "Deploy", "prompt": "Deploy this to ..."}]
    "deploy_targets": [],     # [{"name": "GitHub", "instructions": "push main to origin"}]
    # Defaults for new tasks in this project (the project Settings tab). None = the Den's defaults.
    "default_model": None,    # a models.json id, or "auto"
    "default_mode": None,     # a PERMISSION_MODES value
    # Standing instructions added to every run here (see project_rules_prompt).
    "run_tests": False,       # run the project's tests after editing, before calling it done
    "auto_format": False,     # format the files it changed with the project's formatter
    # Queue the backlog (pending to-dos) at 23:00 when nothing is running (worker.nightly_backlog).
    "nightly_backlog": False,
}
_BOOL_SETTINGS = ("notify_approvals", "notify_failures", "notify_completions", "quiet_hours", "auto_in_progress",
                  "run_tests", "auto_format", "nightly_backlog")


def get_project_settings(project_id: int) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM project_settings WHERE project_id = ?", (project_id,)).fetchone()
        out = dict(PROJECT_SETTINGS_DEFAULTS, allow_commands=[], deploy_targets=[], shortcuts=[])
        if row:
            for k in PROJECT_SETTINGS_DEFAULTS:
                out[k] = row[k]
            out["allow_commands"] = json.loads(row["allow_commands"] or "[]")
            out["deploy_targets"] = json.loads(row["deploy_targets"] or "[]")
            out["shortcuts"] = [t if isinstance(t, dict) else {"label": t[:40], "prompt": t}   # saved before labels existed
                                for t in json.loads(row["shortcuts"] or "[]")]
            for k in _BOOL_SETTINGS:
                out[k] = bool(out[k])
        out["project_id"] = project_id
        return out


def update_project_settings(project_id: int, **fields: Any) -> dict[str, Any]:
    fields = {k: v for k, v in fields.items() if k in PROJECT_SETTINGS_DEFAULTS and v is not None}
    with _lock:
        conn = get_conn()
        conn.execute("INSERT OR IGNORE INTO project_settings (project_id) VALUES (?)", (project_id,))
        for k, v in fields.items():
            if k == "allow_commands":
                v = json.dumps([str(c).strip() for c in v if str(c).strip()])
            elif k == "shortcuts":
                items = [t if isinstance(t, dict) else {"label": "", "prompt": t} for t in v]   # a bare string is just the prompt
                v = json.dumps([{"label": (str(t.get("label") or "").strip() or str(t.get("prompt", "")).strip())[:40],
                                 "prompt": str(t.get("prompt", "")).strip()[:2000]}
                                for t in items if str(t.get("prompt", "")).strip()][:30])
            elif k == "deploy_targets":
                v = json.dumps([{"name": str(t.get("name", "")).strip()[:60], "instructions": str(t.get("instructions", "")).strip()[:1000]}
                                for t in v if str(t.get("name", "")).strip()][:20])
            elif k in ("claude_account", "default_model", "default_mode"):
                v = str(v).strip() or None
            elif k in _BOOL_SETTINGS:
                v = 1 if v else 0
            conn.execute(f"UPDATE project_settings SET {k} = ? WHERE project_id = ?", (v, project_id))
        conn.commit()
        return get_project_settings(project_id)


def add_allowed_command(project_id: int, command: str) -> dict[str, Any]:
    current = get_project_settings(project_id)["allow_commands"]
    if command not in current:
        current = current + [command]
    return update_project_settings(project_id, allow_commands=current)


# ----------------------------------------------------------------- recipes

def list_recipes() -> list[dict[str, Any]]:
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute("SELECT * FROM recipes ORDER BY id ASC").fetchall()]


def get_recipe(recipe_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        return _row_to_dict(row) if row else None


def add_recipe(name: str, prompt: str, target: str = "any") -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO recipes (name, prompt, target, created_at) VALUES (?, ?, ?, ?)",
                           (name, prompt, target, _now()))
        conn.commit()
        return get_recipe(cur.lastrowid)


def delete_recipe(recipe_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        conn.commit()


# --------------------------------------------------------------- schedules

CADENCES = ("once", "daily", "weekdays", "weekly")


def next_run_after(cadence: str, time_of_day: str, weekday: Optional[int] = None,
                   after: Optional[datetime] = None, on_date: Optional[str] = None) -> datetime:
    """Next local-time occurrence strictly after `after` (default now), as an
    aware datetime. on_date ('YYYY-MM-DD') pins a one-off to that day, as long
    as that moment is still in the future."""
    now = (after or datetime.now()).astimezone()
    hh, mm = (int(x) for x in time_of_day.split(":"))
    if cadence == "once" and on_date:
        y, mo, d = (int(x) for x in on_date.split("-"))
        pinned = datetime(y, mo, d, hh, mm).astimezone()
        if pinned > now:
            return pinned
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    for _ in range(8):
        if cadence == "weekdays" and cand.weekday() >= 5:
            cand += timedelta(days=1)
            continue
        if cadence == "weekly" and weekday is not None and cand.weekday() != weekday:
            cand += timedelta(days=1)
            continue
        break
    # Rebuild from the naive wall-clock time so a DST change in between
    # doesn't shift it by an hour.
    return datetime(cand.year, cand.month, cand.day, hh, mm).astimezone()


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


_SCHEDULE_SELECT = "SELECT s.*, p.name AS project_name FROM schedules s LEFT JOIN projects p ON p.id = s.project_id"


def list_schedules() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            _SCHEDULE_SELECT + " ORDER BY s.enabled DESC, s.next_run_at IS NULL, s.next_run_at ASC, s.id ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_schedule(schedule_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute(_SCHEDULE_SELECT + " WHERE s.id = ?", (schedule_id,)).fetchone()
        return _row_to_dict(row) if row else None


def add_schedule(label: str, prompt: str, project_id: Optional[int], cadence: str, time_of_day: str,
                 weekday: Optional[int] = None, model: str = "auto", priority: int = 0,
                 on_date: Optional[str] = None) -> dict[str, Any]:
    nxt = next_run_after(cadence, time_of_day, weekday, on_date=on_date)
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO schedules (label, prompt, project_id, cadence, time_of_day, weekday, model, priority, "
            "enabled, next_run_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (label, prompt, project_id, cadence, time_of_day, weekday, model, priority, _utc_iso(nxt), _now()),
        )
        conn.commit()
        return get_schedule(cur.lastrowid)


def set_schedule_enabled(schedule_id: int, enabled: bool) -> Optional[dict[str, Any]]:
    sc = get_schedule(schedule_id)
    if not sc:
        return None
    nxt = _utc_iso(next_run_after(sc["cadence"], sc["time_of_day"], sc["weekday"])) if enabled else None
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE schedules SET enabled = ?, next_run_at = ? WHERE id = ?",
                     (1 if enabled else 0, nxt, schedule_id))
        conn.commit()
    return get_schedule(schedule_id)


SCHEDULE_FIELDS = ("label", "prompt", "project_id", "cadence", "time_of_day", "weekday", "model", "priority", "enabled")
_SCHEDULE_TIMING = ("cadence", "time_of_day", "weekday")


def update_schedule(schedule_id: int, changes: dict[str, Any], on_date: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Edit any of SCHEDULE_FIELDS (the caller validates them). When the time,
    cadence or "on" state changes, next_run_at is worked out again; a 'once'
    schedule keeps the day it was set for unless on_date gives a new one."""
    sc = get_schedule(schedule_id)
    if not sc:
        return None
    changes = {k: v for k, v in changes.items() if k in SCHEDULE_FIELDS}
    if "enabled" in changes:
        changes["enabled"] = 1 if changes["enabled"] else 0
    new = {**sc, **changes}
    if new["cadence"] != "weekly":
        new["weekday"] = changes["weekday"] = None
    if new["cadence"] != "once":
        on_date = None
    retime = on_date is not None or any(k in changes and changes[k] != sc[k] for k in (*_SCHEDULE_TIMING, "enabled"))
    if retime:
        if not new["enabled"]:
            changes["next_run_at"] = None
        else:
            if new["cadence"] == "once" and on_date is None and sc["next_run_at"]:
                on_date = datetime.fromisoformat(sc["next_run_at"]).astimezone().date().isoformat()
            nxt = next_run_after(new["cadence"], new["time_of_day"], new["weekday"], on_date=on_date)
            changes["next_run_at"] = _utc_iso(nxt)
    if changes:
        with _lock:
            conn = get_conn()
            conn.execute(f"UPDATE schedules SET {', '.join(k + ' = ?' for k in changes)} WHERE id = ?",
                         (*changes.values(), schedule_id))
            conn.commit()
    return get_schedule(schedule_id)


def delete_schedule(schedule_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        conn.commit()


def due_schedules() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            _SCHEDULE_SELECT + " WHERE s.enabled = 1 AND s.next_run_at IS NOT NULL AND s.next_run_at <= ?",
            (_now(),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def mark_schedule_fired(schedule_id: int) -> None:
    """Advance past now (not past the missed time), so a schedule that was
    due several times while the Mac slept only fires once on wake."""
    sc = get_schedule(schedule_id)
    if not sc:
        return
    with _lock:
        conn = get_conn()
        if sc["cadence"] == "once":
            conn.execute("UPDATE schedules SET enabled = 0, next_run_at = NULL, last_run_at = ? WHERE id = ?",
                         (_now(), schedule_id))
        else:
            nxt = next_run_after(sc["cadence"], sc["time_of_day"], sc["weekday"])
            conn.execute("UPDATE schedules SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                         (_utc_iso(nxt), _now(), schedule_id))
        conn.commit()


# ------------------------------------------------------------------- spend
# "Spend" is real money: api-lane tasks (OpenAI, Gemini...). Claude Code's
# own cost_usd is only what a run *would* cost at API prices - it's shown on
# the task itself as an estimate, but a subscription doesn't bill it, so it
# stays out of spend totals and budgets.

def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def spend_summary(days: int = 7) -> dict[str, Any]:
    now = datetime.now().astimezone()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = today0.replace(day=1)
    week0 = today0 - timedelta(days=days - 1)
    since = min(month0, week0)
    with _lock:
        rows = get_conn().execute(
            "SELECT t.project_id, p.name AS project_name, t.cost_usd, COALESCE(t.finished_at, t.created_at) AS at "
            "FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.lane IN ('api', 'assistant') AND t.cost_usd IS NOT NULL AND COALESCE(t.finished_at, t.created_at) >= ?",
            (_utc_iso(since),),
        ).fetchall()
    by_day = {(week0 + timedelta(days=i)).date().isoformat(): 0.0 for i in range(days)}
    by_project: dict[int, dict[str, Any]] = {}
    today = month = 0.0
    for r in rows:
        at = _parse_iso(r["at"]).astimezone()
        cost = r["cost_usd"] or 0.0
        key = at.date().isoformat()
        if key in by_day:
            by_day[key] += cost
        if at >= today0:
            today += cost
        if at >= month0:
            month += cost
            bp = by_project.setdefault(r["project_id"], {"project_id": r["project_id"],
                                                          "name": r["project_name"], "cost_usd": 0.0})
            bp["cost_usd"] += cost
    return {
        "today_usd": today,
        "month_usd": month,
        "monthly_budget_usd": get_setting("monthly_budget_usd"),
        "days": [{"date": d, "cost_usd": c} for d, c in by_day.items()],
        "by_project": sorted(by_project.values(), key=lambda x: -x["cost_usd"]),
    }


def log_usage(kind: str, model_id: Optional[str], input_tokens: Optional[int], output_tokens: Optional[int],
              cost_usd: Optional[float] = None, task_id: Optional[int] = None, project_id: Optional[int] = None) -> None:
    """Record a side model call (see the usage_log table). Never raises - a
    failed write must not break the call it describes."""
    try:
        with _lock:
            conn = get_conn()
            if project_id is None and task_id is not None:
                row = conn.execute("SELECT project_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
                project_id = row["project_id"] if row else None
            conn.execute(
                "INSERT INTO usage_log (created_at, kind, model_id, task_id, project_id, input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_now(), kind, model_id, task_id, project_id, input_tokens, output_tokens, cost_usd),
            )
            conn.commit()
    except sqlite3.Error:
        logging.getLogger(__name__).exception("Couldn't record %s usage", kind)


# ------------------------------------------------------------ token usage
# The Token usage page: every task that reported usage plus the side calls in
# usage_log, over a local date range, with optional project / model / lane
# filters. lane "background" means only the usage_log rows. Real spend is the
# non-Claude-Code cost; Claude Code's cost_usd is an API-price estimate (see
# "spend" above), so the two are kept apart.

TOKEN_USAGE_MAX_DAYS = 400
TOKEN_USAGE_MAX_ITEMS = 2000


def _local_day_bounds(start: str, end: str) -> tuple[datetime, datetime]:
    """Local midnight at the start of `start` and after the end of `end` (YYYY-MM-DD, inclusive)."""
    tz = datetime.now().astimezone().tzinfo
    d0 = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=tz)
    d1 = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
    return d0.astimezone(), d1.astimezone()


def _usage_rows(conn: sqlite3.Connection, since: datetime, until: datetime, project_id: Optional[int],
                model_id: Optional[str], lane: Optional[str], account: Optional[str] = None,
                default_account: str = "mac") -> list[dict[str, Any]]:
    """account: only Claude Code runs on that Claude account (a run from before
    accounts were recorded counts as default_account); API and background calls have none."""
    items: list[dict[str, Any]] = []
    if account:
        lane = "agent"
    if lane != "background":
        q = ("SELECT t.id, t.thread_id, CASE WHEN t.lane = 'agent' THEN COALESCE(t.claude_account, ?) END AS account, t.title, t.prompt, t.project_id, p.name AS project_name, t.model_id, t.lane, "
             "t.category, t.status, t.source, t.input_tokens, t.output_tokens, t.cache_tokens, t.cost_usd, t.duration_ms, "
             "COALESCE(t.finished_at, t.created_at) AS at "
             "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
             "WHERE COALESCE(t.finished_at, t.created_at) >= ? AND COALESCE(t.finished_at, t.created_at) < ? "
             "AND (t.input_tokens IS NOT NULL OR t.output_tokens IS NOT NULL OR t.cost_usd IS NOT NULL)")
        args: list[Any] = [default_account, _utc_iso(since), _utc_iso(until)]
        if account:
            q += " AND COALESCE(t.claude_account, ?) = ?"
            args += [default_account, account]
        if project_id is not None:
            q += " AND t.project_id = ?"
            args.append(project_id)
        if model_id:
            q += " AND t.model_id = ?"
            args.append(model_id)
        if lane:
            q += " AND t.lane = ?"
            args.append(lane)
        for r in conn.execute(q, args).fetchall():
            d = {k: r[k] for k in r.keys()}
            prompt = d.pop("prompt") or ""
            d.update(kind="task", title=d["title"] or prompt[:80], chat_id=d["thread_id"] or d["id"])
            items.append(d)
    if not account and (not lane or lane == "background"):
        q = ("SELECT u.id, u.task_id, u.kind, u.model_id, u.project_id, p.name AS project_name, u.input_tokens, "
             "u.output_tokens, u.cost_usd, u.created_at AS at, t.title AS task_title, t.thread_id "
             "FROM usage_log u LEFT JOIN projects p ON p.id = u.project_id LEFT JOIN tasks t ON t.id = u.task_id "
             "WHERE u.created_at >= ? AND u.created_at < ?")
        args = [_utc_iso(since), _utc_iso(until)]
        if project_id is not None:
            q += " AND u.project_id = ?"
            args.append(project_id)
        if model_id:
            q += " AND u.model_id = ?"
            args.append(model_id)
        for r in conn.execute(q, args).fetchall():
            d = {k: r[k] for k in r.keys()}
            task_id, thread_id = d.pop("task_id"), d.pop("thread_id")
            d.update(id="u" + str(d["id"]), lane="background", account=None, category=d["kind"], status="done", source=None,
                     cache_tokens=None, duration_ms=None, title=d.pop("task_title"),
                     chat_id=(thread_id or task_id) if task_id else None)
            items.append(d)
    for d in items:
        d["total_tokens"] = (d["input_tokens"] or 0) + (d["output_tokens"] or 0)
    return items


def _usage_totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    def s(k: str, rows: list[dict[str, Any]]) -> float:
        return sum(r[k] or 0 for r in rows)
    agent = [r for r in items if r["lane"] == "agent"]
    other = [r for r in items if r["lane"] != "agent"]
    return {
        "runs": len(items),
        "input_tokens": int(s("input_tokens", items)),
        "output_tokens": int(s("output_tokens", items)),
        "cache_tokens": int(s("cache_tokens", items)),
        "total_tokens": int(s("total_tokens", items)),
        "spend_usd": round(s("cost_usd", other), 6),
        "claude_estimate_usd": round(s("cost_usd", agent), 6),
    }


def _usage_group(items: list[dict[str, Any]], key: str, label_key: Optional[str] = None) -> list[dict[str, Any]]:
    groups: dict[Any, dict[str, Any]] = {}
    for r in items:
        k = r.get(key)
        g = groups.setdefault(k, {"key": k, "label": r.get(label_key) if label_key else k, "rows": []})
        g["rows"].append(r)
    out = [{"key": g["key"], "label": g["label"], **_usage_totals(g["rows"])} for g in groups.values()]
    return sorted(out, key=lambda x: (-x["total_tokens"], -(x["spend_usd"] + x["claude_estimate_usd"])))


def token_usage(start: Optional[str] = None, end: Optional[str] = None, project_id: Optional[int] = None,
                model_id: Optional[str] = None, lane: Optional[str] = None, account: Optional[str] = None,
                default_account: str = "mac") -> dict[str, Any]:
    """Everything the Token usage page shows for one slice. start/end are local
    YYYY-MM-DD days, both included (default: the last 30 days). Raises
    ValueError on a bad date or a range over TOKEN_USAGE_MAX_DAYS."""
    today = datetime.now().astimezone().date()
    end = end or today.isoformat()
    start = start or (datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=29)).isoformat()
    since, until = _local_day_bounds(start, end)
    n_days = (until.date() - since.date()).days
    if n_days < 1:
        raise ValueError("The start date is after the end date")
    if n_days > TOKEN_USAGE_MAX_DAYS:
        raise ValueError(f"Pick a range of at most {TOKEN_USAGE_MAX_DAYS} days")
    with _lock:
        conn = get_conn()
        items = _usage_rows(conn, since, until, project_id, model_id, lane, account, default_account)
        # The same length of time just before, for the "vs previous" line.
        prev = _usage_rows(conn, since - (until - since), since, project_id, model_id, lane, account, default_account)
        months = [r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(COALESCE(finished_at, created_at), 1, 7) AS m FROM tasks "
            "WHERE input_tokens IS NOT NULL OR output_tokens IS NOT NULL OR cost_usd IS NOT NULL "
            "UNION SELECT DISTINCT substr(created_at, 1, 7) FROM usage_log ORDER BY m DESC").fetchall()]
        projects = [dict(r) for r in conn.execute(
            "SELECT id, name FROM projects WHERE id IN (SELECT project_id FROM tasks WHERE input_tokens IS NOT NULL "
            "OR output_tokens IS NOT NULL OR cost_usd IS NOT NULL UNION SELECT project_id FROM usage_log) ORDER BY name COLLATE NOCASE"
        ).fetchall()]
        models = [r["m"] for r in conn.execute(
            "SELECT DISTINCT model_id AS m FROM tasks WHERE model_id IS NOT NULL AND (input_tokens IS NOT NULL "
            "OR output_tokens IS NOT NULL OR cost_usd IS NOT NULL) UNION SELECT DISTINCT model_id FROM usage_log "
            "WHERE model_id IS NOT NULL ORDER BY m").fetchall()]
        accounts_used = {r["a"] for r in conn.execute(
            "SELECT DISTINCT COALESCE(claude_account, ?) AS a FROM tasks WHERE lane = 'agent' AND (input_tokens IS NOT NULL "
            "OR output_tokens IS NOT NULL OR cost_usd IS NOT NULL)", (default_account,)).fetchall()}

    by_day = {(since.date() + timedelta(days=i)).isoformat(): [] for i in range(n_days)}
    for r in items:
        key = _parse_iso(r["at"]).astimezone().date().isoformat()
        if key in by_day:
            by_day[key].append(r)
    items.sort(key=lambda r: (-r["total_tokens"], -(r["cost_usd"] or 0)))
    return {
        "start": start, "end": end, "days": n_days,
        "filters": {"project_id": project_id, "model_id": model_id, "lane": lane, "account": account},
        "totals": _usage_totals(items),
        "previous": _usage_totals(prev),
        "by_day": [{"date": d, **_usage_totals(rows)} for d, rows in by_day.items()],
        "by_project": _usage_group(items, "project_id", "project_name"),
        "by_model": _usage_group(items, "model_id"),
        "by_lane": _usage_group(items, "lane"),
        "by_category": _usage_group(items, "category"),
        "by_account": _usage_group([r for r in items if r.get("account")], "account"),
        "items": items[:TOKEN_USAGE_MAX_ITEMS],
        "items_truncated": len(items) > TOKEN_USAGE_MAX_ITEMS,
        "options": {"months": [m for m in months if m], "projects": projects, "models": models,
                    "accounts": sorted(accounts_used | ({account} if account else set()))},
    }


def project_month_spend(project_id: int) -> float:
    month0 = datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with _lock:
        row = get_conn().execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM tasks WHERE project_id = ? AND lane IN ('api', 'assistant') "
            "AND COALESCE(finished_at, created_at) >= ?",
            (project_id, _utc_iso(month0)),
        ).fetchone()
        return float(row["s"])


# ------------------------------------------------------- personal assistant
# Storage for assistant/: connected accounts (no tokens), the activity log,
# per-chat working memory, reminders, and the few task/approval helpers the
# assistant lane needs. Approvals themselves reuse the `approvals` table:
# tool_name 'assistant:<tool>.<action>', tool_input a JSON payload.

def _connection(row: sqlite3.Row) -> dict[str, Any]:
    out = _row_to_dict(row)
    out["scopes"] = json.loads(out["scopes"] or "[]")
    return out


def get_connection(conn_id: str) -> Optional[dict[str, Any]]:
    """One connected account by its id ('google', 'google-1a2b3c4d')."""
    with _lock:
        row = get_conn().execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
        return _connection(row) if row else None


def list_connections(provider: Optional[str] = None) -> list[dict[str, Any]]:
    """Every connected account (of one provider), in the order they were first connected."""
    with _lock:
        sql = "SELECT * FROM connections" + (" WHERE provider = ?" if provider else "") + " ORDER BY rowid"
        return [_connection(r) for r in get_conn().execute(sql, (provider,) if provider else ()).fetchall()]


def save_connection(conn_id: str, account: Optional[str], status: str, scopes: Optional[list[str]] = None,
                    error: Optional[str] = None, provider: Optional[str] = None) -> dict[str, Any]:
    """provider is only needed for a new row; it defaults to the id's prefix ('google-1a2b' -> 'google')."""
    now = _now()
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO connections (id, provider, account, status, scopes, connected_at, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "account = COALESCE(excluded.account, account), status = excluded.status, "
            "scopes = CASE WHEN excluded.scopes = '[]' THEN scopes ELSE excluded.scopes END, "
            "connected_at = CASE WHEN excluded.status = 'connected' THEN excluded.connected_at ELSE connected_at END, "
            "last_error = excluded.last_error, updated_at = excluded.updated_at",
            (conn_id, provider or conn_id.split("-")[0], account, status, json.dumps(scopes or []),
             now if status == "connected" else None, (error or "")[:300] or None, now),
        )
        conn.commit()
        return get_connection(conn_id)  # type: ignore[return-value]


def delete_connection(conn_id: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
        conn.commit()


def add_activity(tool: str, action: str, target: Optional[str] = None, approval: str = "not_required",
                 result: str = "ok", error: Optional[str] = None, task_id: Optional[int] = None,
                 approval_id: Optional[int] = None, ref: Optional[str] = None) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO assistant_activity (created_at, tool, action, target, approval, result, error, "
            "task_id, approval_id, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), tool, action, (target or "")[:200] or None, approval, result, (error or "")[:300] or None,
             task_id, approval_id, (ref or "")[:200] or None),
        )
        conn.commit()
        return cur.lastrowid


def list_activity(limit: int = 100, tool: Optional[str] = None, action: Optional[str] = None) -> list[dict[str, Any]]:
    q, params = "SELECT * FROM assistant_activity WHERE 1=1", []  # type: (str, list[Any])
    if tool:
        q += " AND tool = ?"
        params.append(tool)
    if action:
        q += " AND action = ?"
        params.append(action)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute(q, params).fetchall()]


def get_assistant_context(thread_id: int) -> dict[str, Any]:
    with _lock:
        row = get_conn().execute("SELECT data FROM assistant_context WHERE thread_id = ?", (thread_id,)).fetchone()
        return json.loads(row["data"]) if row else {}


def set_assistant_context(thread_id: int, data: dict[str, Any]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("INSERT INTO assistant_context (thread_id, data, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(thread_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                     (thread_id, json.dumps(data), _now()))
        conn.commit()


def add_reminder(text: str, due_at_iso: str, task_id: Optional[int] = None) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute("INSERT INTO reminders (text, due_at, status, created_at, task_id) VALUES (?, ?, 'pending', ?, ?)",
                           (text.strip()[:300], due_at_iso, _now(), task_id))
        conn.commit()
        return get_reminder(cur.lastrowid)  # type: ignore[return-value]


def get_reminder(reminder_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_reminders(statuses: Optional[list[str]] = None, limit: int = 100) -> list[dict[str, Any]]:
    q, params = "SELECT * FROM reminders WHERE 1=1", []  # type: (str, list[Any])
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        params += statuses
    q += " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, due_at ASC LIMIT ?"
    params.append(limit)
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute(q, params).fetchall()]


def cancel_reminder(reminder_id: int) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (reminder_id,))
        conn.commit()
        return cur.rowcount == 1


def due_reminders() -> list[dict[str, Any]]:
    """Pending reminders whose time has come. A reminder missed while the Mac
    slept fires once, late, on wake."""
    with _lock:
        rows = get_conn().execute("SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at ASC",
                                  (_now(),)).fetchall()
        return [_row_to_dict(r) for r in rows]


def mark_reminder_fired(reminder_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE reminders SET status = 'fired', fired_at = ? WHERE id = ? AND status = 'pending'",
                     (_now(), reminder_id))
        conn.commit()


def claim_approval(approval_id: int, approved: bool, note: Optional[str] = None) -> bool:
    """Decide a pending approval and say whether *this* call is the one that
    decided it - so an action is executed exactly once even if two clicks race."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, note = ? WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "denied", _now(), (note or "").strip()[:500] or None, approval_id),
        )
        conn.commit()
        return cur.rowcount == 1


def set_approval_input(approval_id: int, tool_input: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE approvals SET tool_input = ? WHERE id = ?", (tool_input, approval_id))
        conn.commit()


def expire_assistant_approvals(older_than_iso: str) -> list[dict[str, Any]]:
    """Time out assistant approvals nobody answered. Returns the rows expired."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' AND tool_name LIKE 'assistant:%' AND created_at < ?",
            (older_than_iso,)).fetchall()
        for r in rows:
            conn.execute("UPDATE approvals SET status = 'timed_out', decided_at = ? WHERE id = ?", (_now(), r["id"]))
        conn.commit()
        return [_row_to_dict(r) for r in rows]


def append_task_result(task_id: int, text: str) -> None:
    """Add a line to a finished task's answer (e.g. "Sent" after an approval)."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE tasks SET result_summary = COALESCE(result_summary, '') || ? WHERE id = ?",
                     ("\n\n" + text.strip(), task_id))
        conn.commit()


def recent_assistant_thread(within_minutes: int = 15) -> Optional[int]:
    """Root task id of the assistant chat that was last active within the
    window - what a voice request with no thread attaches to."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    with _lock:
        row = get_conn().execute(
            "SELECT COALESCE(thread_id, id) AS root FROM tasks WHERE lane = 'assistant' AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1", (since,)).fetchone()
        return row["root"] if row else None


# ------------------------------------------------- Octopus: routing log & plans

def add_routing(request: str, plan: dict[str, Any], status: str = "proposed", source: str = "api",
                decided_by: str = "auto", task_id: Optional[int] = None) -> dict[str, Any]:
    """Log a request and the plan Octopus made for it (see octopus/plan.py)."""
    with _lock:
        conn = get_conn()
        now = _now()
        cur = conn.execute(
            "INSERT INTO routing (created_at, updated_at, source, request, status, category, agent_id, model_id, "
            "project_id, risk, plan, task_id, decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, source, request, status, plan.get("category"), (plan.get("agent") or {}).get("id"),
             (plan.get("model") or {}).get("id"), (plan.get("project") or {}).get("id"), plan.get("risk"),
             json.dumps(plan), task_id, decided_by))
        conn.commit()
        return get_routing(cur.lastrowid)


def _routing_row(row: sqlite3.Row) -> dict[str, Any]:
    out = _row_to_dict(row)
    try:
        out["plan"] = json.loads(out.get("plan") or "{}")
    except ValueError:
        out["plan"] = {}
    return out


def get_routing(routing_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM routing WHERE id = ?", (routing_id,)).fetchone()
        return _routing_row(row) if row else None


def update_routing(routing_id: int, plan: Optional[dict[str, Any]] = None, **fields: Any) -> Optional[dict[str, Any]]:
    """Change a logged plan's status / task / decided_by. A new `plan` replaces
    the stored one and refreshes the columns copied out of it."""
    allowed = {k: v for k, v in fields.items() if k in ("status", "task_id", "decided_by", "source")}
    if plan is not None:
        allowed.update(plan=json.dumps(plan), category=plan.get("category"), agent_id=(plan.get("agent") or {}).get("id"),
                       model_id=(plan.get("model") or {}).get("id"), project_id=(plan.get("project") or {}).get("id"),
                       risk=plan.get("risk"))
    if not allowed:
        return get_routing(routing_id)
    allowed["updated_at"] = _now()
    with _lock:
        conn = get_conn()
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE routing SET {sets} WHERE id = ?", (*allowed.values(), routing_id))
        conn.commit()
        return get_routing(routing_id)


def claim_routing(routing_id: int, to_status: str, from_statuses: tuple = ("proposed", "needs_input")) -> bool:
    """Move a plan out of 'waiting' exactly once - so a double-tapped Run
    (or a Run racing a Cancel) can't queue the same request twice."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            f"UPDATE routing SET status = ?, updated_at = ? WHERE id = ? AND status IN ({','.join('?' * len(from_statuses))})",
            (to_status, _now(), routing_id, *from_statuses))
        conn.commit()
        return cur.rowcount > 0


def list_routing(limit: int = 100, status: Optional[str] = None, category: Optional[str] = None) -> list[dict[str, Any]]:
    """Newest first, each row joined with what became of its task."""
    with _lock:
        q = ("SELECT r.*, t.status AS task_status, t.duration_ms, t.cost_usd, t.input_tokens, t.output_tokens, "
             "t.error_message AS task_error, t.title AS task_title, t.lane AS task_lane, "
             "SUBSTR(COALESCE(t.result_summary, ''), 1, 240) AS result_excerpt, p.name AS project_name "
             "FROM routing r LEFT JOIN tasks t ON t.id = r.task_id LEFT JOIN projects p ON p.id = r.project_id WHERE 1=1")
        params: list[Any] = []
        if status:
            q += " AND r.status = ?"
            params.append(status)
        if category:
            q += " AND r.category = ?"
            params.append(category)
        q += " ORDER BY r.id DESC LIMIT ?"
        params.append(limit)
        return [_routing_row(r) for r in get_conn().execute(q, params).fetchall()]


def pending_plan(within_minutes: int = 30) -> Optional[dict[str, Any]]:
    """The newest plan still waiting on the human (Run / Cancel, or an answer
    to "which project?") - what a spoken "yes, go ahead" refers to."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM routing WHERE status IN ('proposed', 'needs_input') AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1", (since,)).fetchone()
        return _routing_row(row) if row else None


def expire_old_plans(older_than_minutes: int = 120) -> int:
    since = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    with _lock:
        conn = get_conn()
        cur = conn.execute("UPDATE routing SET status = 'expired', updated_at = ? "
                           "WHERE status IN ('proposed', 'needs_input') AND created_at < ?", (_now(), since))
        conn.commit()
        return cur.rowcount


def _set_task_json(task_id: int, column: str, value: Any) -> None:
    assert column in _JSON_COLUMNS
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (json.dumps(value), task_id))
        conn.commit()


def set_task_plan_steps(task_id: int, steps: list[dict[str, Any]]) -> None:
    """The live plan an agent reported (Claude Code's todo list): [{content, status}]."""
    _set_task_json(task_id, "plan_steps", steps)


def set_task_git_base(task_id: int, base: dict[str, Any]) -> None:
    _set_task_json(task_id, "git_base", base)


def set_task_artifacts(task_id: int, artifacts: list[dict[str, Any]]) -> None:
    _set_task_json(task_id, "artifacts", artifacts)


def model_category_stats(days: int = 30) -> dict[str, dict[str, int]]:
    """{"model_id|category": {"n", "done", "failed"}} over the last `days` -
    the "historical success" the router weighs."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock:
        rows = get_conn().execute(
            "SELECT model_id, category, COUNT(*) AS n, SUM(status = 'done') AS done, "
            "SUM(status IN ('error', 'needs_review')) AS failed FROM tasks "
            "WHERE created_at >= ? AND model_id IS NOT NULL AND status IN ('done', 'error', 'needs_review') "
            "GROUP BY model_id, category", (since,)).fetchall()
        return {f"{r['model_id']}|{r['category']}": {"n": r["n"], "done": r["done"] or 0, "failed": r["failed"] or 0}
                for r in rows}


def recent_thread(within_minutes: int = 15, lanes: Optional[tuple] = None) -> Optional[int]:
    """Root task id of the chat that saw the most recent activity within the
    window, in any lane (or only `lanes`) - what a follow-up like "fix the
    second one" attaches to. Instant control replies never count."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    lane_sql = f" AND lane IN ({','.join('?' * len(lanes))})" if lanes else " AND lane != 'control'"
    with _lock:
        row = get_conn().execute(
            "SELECT COALESCE(thread_id, id) AS root, COALESCE(finished_at, started_at, created_at) AS at FROM tasks "
            f"WHERE COALESCE(finished_at, started_at, created_at) >= ?{lane_sql} ORDER BY at DESC, id DESC LIMIT 1",
            (since, *(lanes or ()))).fetchone()
        return row["root"] if row else None


def list_task_approvals(task_id: int) -> list[dict[str, Any]]:
    with _lock:
        return [_row_to_dict(r) for r in get_conn().execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()]


def list_recent_events(limit: int = 60) -> list[dict[str, Any]]:
    """What Octopus did lately, across every task, newest first - the raw
    material for the Den Home's activity feed."""
    with _lock:
        rows = get_conn().execute(
            "SELECT e.*, t.title AS task_title, t.project_id, t.lane, t.model_id, p.name AS project_name "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id JOIN projects p ON p.id = t.project_id "
            "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
