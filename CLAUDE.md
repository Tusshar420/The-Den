# The Den - rules for Claude

These rules are for changing **The Den's own code** (this folder), whether you are Claude Code in
an editor or Octopus running a task for the project "The Den". If you are working somewhere under
`workspace/` (the General project, the assistant's folder, uploads...), you're not changing the Den:
ignore this file.

## New features go in a module, so the Den never needs a restart

The Den has a module system (`den_modules/`, guide: `docs/MODULES.md`) built so new features can
be added and changed while it keeps running. Use it.

- **Before building anything, look at what's already there:** `ls modules/` and `docs/MODULES.md`.
  A feature may already exist, or one module may already do something close to it.
- **Build new features as a module:** `modules/<name>/module.json` + `module.py`.
  - New module: switch it on live with `POST /modules/rescan`.
  - Changed module: `POST /modules/<name>/reload`. Changing its config
    (`PATCH /modules/<name>/config`) reloads it by itself.
  - The Den does not watch module files. After editing one, always rescan or reload it,
    then check `GET /modules/<name>` says `"status": "running"`.
- **Core files only load at startup**, so editing them means a restart: `db.py`, `api.py`,
  `worker.py`, `main.py`, `mailer.py`, `email_templates.py`, `approval_links.py`, `den_modules/`,
  `assistant/`, `octopus/` and the other top-level `.py` files. Only edit them when a module truly
  can't do the job, and **tell the user before you start** that it will need a restart, and why.
- A module may only call what the running core already has. Don't add a core helper for a
  module to use; that brings the restart back.
- Page files in `static/den/` are read fresh on every load, so they need no restart.

Patterns (see `modules/approval_reminder/` for a working example):
- Background job: start a thread in `start()`, stop and join it in `stop()`.
- Per-item memory (e.g. "already sent"): `ctx.get_state` / `ctx.set_state`, not a new DB column.
- A setting: the manifest's `config`, not a new key in `db.SETTINGS_DEFAULTS`.
- A new email: add its entry to `email_templates.EVENTS` in `initialize()` and remove it with
  `ctx.add_cleanup`. That also gives it an on/off switch (`GET/PATCH /email`).

API calls need the key: see "Tushar's Workspace" in the user's global CLAUDE.md for how to read it.

## Tests

`.venv/bin/python -m unittest discover -s tests -t .` (about 3 minutes; run it in the background).
Test a module by loading its file directly with a fake `ctx`, like `tests/test_approval_reminder.py`.
