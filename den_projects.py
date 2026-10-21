"""
The project page of The Den 2.0 dashboard (static/den/DenProjects.dc.html):
what it shows that no other endpoint gives.

- /den/projects: every project with its live state (running / needs you /
  asleep / idle), git branch, queue counts and what is running now.
- Files tab: the project folder as a tree (with git markers), reading and
  saving one file. Every path is resolved and must stay inside the project's
  folder; .git and dependency folders are never listed or written.
- Changes tab: git status with +/- counts, recent commits, commit selected
  files, and push - which is queued as a Claude Code task, so the push goes
  through the usual approval like any other protected command.
- Ideas tab: parked thoughts that never run on their own (project_ideas).
- /den/fs/dirs: browse folders on this Mac (under your home folder) to pick a
  project's folder - a browser can't hand over a real path itself.
"""

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db
import den_platform
import providers
import router

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", "build", ".cache", ".idea", ".DS_Store",
             ".mypy_cache", ".pytest_cache", ".turbo", "coverage", ".gradle", "Pods", "DerivedData", ".svelte-kit"}
MAX_TREE = 3000
MAX_READ = 512 * 1024
MAX_WRITE = 1024 * 1024
GIT_TIMEOUT = 15
HOME = Path.home()


# ------------------------------------------------------------------ helpers

def _project(project_id: int) -> dict[str, Any]:
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


def _root(p: dict[str, Any], env_id: Optional[int] = None) -> Path:
    """The project's folder, or one of its environments' (project_envs) when env_id is given."""
    if env_id is not None:
        env = db.get_env(env_id)
        if not env or env["project_id"] != p["id"]:
            raise HTTPException(status_code=404, detail="Environment not found")
        path = env["path"]
    else:
        if not db.has_folder(p):
            raise HTTPException(status_code=400, detail="This project has no folder yet - link one in Overview")
        path = p["path"]
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail=f"The folder {path} doesn't exist")
    return root


def _inside(root: Path, rel: str) -> Path:
    """rel resolved under root; refuses anything that lands outside it or inside a skipped folder."""
    rel = (rel or "").strip().lstrip("/")
    if not rel or "\x00" in rel:
        raise HTTPException(status_code=400, detail="Name a file inside the project")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="That path is outside the project folder")
    if any(part in SKIP_DIRS for part in target.relative_to(root).parts):
        raise HTTPException(status_code=400, detail="That folder isn't editable here")
    return target


def _git(root: Path, *args: str, check: bool = False, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, input=input_text)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(status_code=500, detail=f"git didn't answer: {e}")
    if check and r.returncode != 0:
        raise HTTPException(status_code=400, detail=(r.stderr or r.stdout).strip()[:400] or "git failed")
    return r


def _is_git(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def _branch(root: Path) -> Optional[str]:
    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _status_map(root: Path) -> dict[str, str]:
    """{relative path: 'M' | 'A' | 'D' | 'U' | 'R'} from git status (U = untracked)."""
    out: dict[str, str] = {}
    r = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if r.returncode != 0:
        return out
    items = r.stdout.split("\x00")
    i = 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] == "R" or xy[1] == "R":
            i += 1          # -z puts the old name next
            out[path] = "R"
        elif xy == "??":
            out[path] = "U"
        elif "D" in xy:
            out[path] = "D"
        elif "A" in xy:
            out[path] = "A"
        else:
            out[path] = "M"
    return out


def short_path(path: Optional[str]) -> str:
    if not path:
        return "no folder"
    p = str(Path(path).expanduser())
    home = str(HOME)
    return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p


# ------------------------------------------------------------------ project list

def _head_branch(path: Optional[str]) -> Optional[str]:
    """The checked-out branch, read from .git/HEAD directly - no subprocess per card."""
    if not path:
        return None
    try:
        ref = (Path(path).expanduser() / ".git" / "HEAD").read_text().strip()
    except OSError:
        return None
    return ref.split("refs/heads/", 1)[1] if "refs/heads/" in ref else ref[:7]


def project_states() -> list[dict[str, Any]]:
    """Every project with what the cards on the Projects page show."""
    projects = db.list_projects()
    with db._lock:
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT project_id, status, COUNT(*) AS n, MAX(COALESCE(finished_at, started_at, created_at)) AS last "
            "FROM tasks WHERE status NOT IN ('deleted') GROUP BY project_id, status").fetchall()
        running = {r["project_id"]: dict(r) for r in conn.execute(
            "SELECT id, project_id, env_id, title, prompt, started_at, current_step, lane, model_id, claude_account FROM tasks WHERE status = 'running'").fetchall()}
        env_rows = conn.execute("SELECT * FROM project_envs ORDER BY id").fetchall()
        env_counts = {(r["project_id"], r["env_id"]): r["n"] for r in conn.execute(
            "SELECT project_id, env_id, COUNT(*) AS n FROM tasks WHERE status IN ('queued', 'rate_limited') GROUP BY project_id, env_id").fetchall()}
        approvals = {r["project_id"] for r in conn.execute(
            "SELECT t.project_id FROM approvals a JOIN tasks t ON t.id = a.task_id WHERE a.status = 'pending'").fetchall()}
        ideas = {r["project_id"]: r["n"] for r in conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM project_ideas WHERE status = 'parked' GROUP BY project_id").fetchall()}
    counts: dict[int, dict[str, int]] = {}
    last: dict[int, str] = {}
    for r in rows:
        counts.setdefault(r["project_id"], {})[r["status"]] = r["n"]
        if r["last"] and r["last"] > last.get(r["project_id"], ""):
            last[r["project_id"]] = r["last"]
    out = []
    for p in projects:
        c = counts.get(p["id"], {})
        run = running.get(p["id"])
        needs = p["id"] in approvals or c.get("needs_review", 0) > 0
        state = "running" if run else "needs" if needs else "asleep" if p.get("status") == "on_hold" else "idle"
        branch = _head_branch(p["path"]) if db.has_folder(p) else None
        ps = db.get_project_settings(p["id"])
        # Environments: the project's own folder first (env id None), then project_envs.
        mine = [e for e in env_rows if e["project_id"] == p["id"]]
        envs = [{"id": None, "name": p.get("env_label") or "Main", "branch": branch, "short_path": short_path(p.get("path")),
                 "queued": env_counts.get((p["id"], None), 0), "running": bool(run and not run["env_id"])}] if mine else []
        envs += [{"id": e["id"], "name": e["name"], "branch": _head_branch(e["path"]), "short_path": short_path(e["path"]),
                  "queued": env_counts.get((p["id"], e["id"]), 0), "running": bool(run and run["env_id"] == e["id"])} for e in mine]
        out.append({"id": p["id"], "state": state, "branch": branch, "short_path": short_path(p.get("path")),
                    "queued": c.get("queued", 0) + c.get("rate_limited", 0), "done": c.get("done", 0) + c.get("needs_review", 0),
                    "failed": c.get("error", 0), "review": c.get("needs_review", 0), "last_at": last.get(p["id"]),
                    "ideas": ideas.get(p["id"], 0), "envs": envs, "default_model": ps.get("default_model"), "claude_account": ps.get("claude_account"),
                    "running": {"id": run["id"], "title": run["title"] or (run["prompt"] or "")[:80], "started_at": run["started_at"],
                                "step": run["current_step"], "lane": run["lane"], "model_id": run["model_id"], "env_id": run["env_id"]} if run else None})
    return out


# ------------------------------------------------------------------ files

def tree(project_id: int, env_id: Optional[int] = None) -> dict[str, Any]:
    root = _root(_project(project_id), env_id)
    entries: list[dict[str, Any]] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        for d in dirnames:
            entries.append({"path": str(rel_dir / d) + "/", "dir": True})
        for f in sorted(filenames):
            if f in SKIP_DIRS:
                continue
            fp = Path(dirpath) / f
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            entries.append({"path": str(rel_dir / f), "dir": False, "size": size})
        if len(entries) > MAX_TREE:
            truncated = True
            break
    for e in entries:
        if e["path"].startswith("./"):
            e["path"] = e["path"][2:]
    git = _status_map(root) if _is_git(root) else {}
    return {"root": root.name, "path": short_path(str(root)), "branch": _branch(root) if git or _is_git(root) else None,
            "entries": entries[:MAX_TREE], "truncated": truncated, "git": git}


def read_file(project_id: int, rel: str, env_id: Optional[int] = None) -> dict[str, Any]:
    root = _root(_project(project_id), env_id)
    target = _inside(root, rel)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="No such file")
    size = target.stat().st_size
    if size > MAX_READ:
        return {"path": rel, "size": size, "binary": True, "text": None, "note": f"Too big to edit here ({size // 1024} KB)"}
    data = target.read_bytes()
    if b"\x00" in data[:8000]:
        return {"path": rel, "size": size, "binary": True, "text": None, "note": "Binary file - not shown"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return {"path": rel, "size": size, "binary": False, "text": text}


class FileIn(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    text: str = Field(max_length=MAX_WRITE)
    create: bool = False      # a new file: refuse to overwrite one that exists


def write_file(project_id: int, body: FileIn, env_id: Optional[int] = None) -> dict[str, Any]:
    root = _root(_project(project_id), env_id)
    target = _inside(root, body.path)
    if target.exists() and not target.is_file():
        raise HTTPException(status_code=400, detail="That's a folder")
    if body.create and target.exists():
        raise HTTPException(status_code=409, detail="A file with that name already exists")
    if not body.create and not target.exists():
        raise HTTPException(status_code=404, detail="No such file - create it first")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + target.name + ".den-tmp")
    tmp.write_text(body.text, encoding="utf-8")
    os.replace(tmp, target)
    return {"path": body.path, "size": target.stat().st_size, "saved": True}


# ------------------------------------------------------------------ git (Changes tab)

def changes(project_id: int, env_id: Optional[int] = None) -> dict[str, Any]:
    p = _project(project_id)
    root = _root(p, env_id)
    if not _is_git(root):
        return {"is_git": False, "files": [], "commits": [], "branch": None, "adds": 0, "dels": 0}
    status = _status_map(root)
    nums: dict[str, tuple[int, int]] = {}
    for args in (("diff", "--numstat"), ("diff", "--numstat", "--cached")):
        for line in _git(root, *args).stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d = (int(x) if x.isdigit() else 0 for x in parts[:2])
                pa, pd = nums.get(parts[2], (0, 0))
                nums[parts[2]] = (pa + a, pd + d)
    files = []
    for path, kind in sorted(status.items()):
        if any(part in SKIP_DIRS for part in Path(path).parts):
            continue        # same folders the Files tab leaves out (and commit refuses)
        a, d = nums.get(path, (0, 0))
        if kind == "U":
            try:
                fp = _inside(root, path)
                a = sum(1 for _ in fp.open(errors="ignore")) if fp.is_file() and fp.stat().st_size < MAX_READ else 0
            except (HTTPException, OSError):
                a = 0
        files.append({"path": path, "kind": "A" if kind == "U" else kind, "untracked": kind == "U", "adds": a, "dels": d})
    log = _git(root, "log", "-8", "--pretty=format:%h%x09%s%x09%cI").stdout.splitlines()
    commits = [dict(zip(("sha", "msg", "at"), line.split("\t", 2))) for line in log if line.count("\t") >= 2]
    ahead = _git(root, "rev-list", "--count", "@{u}..HEAD")
    return {"is_git": True, "branch": _branch(root), "files": files, "commits": commits,
            "adds": sum(f["adds"] for f in files), "dels": sum(f["dels"] for f in files),
            "ahead": int(ahead.stdout.strip()) if ahead.returncode == 0 and ahead.stdout.strip().isdigit() else None}


class CommitIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    paths: list[str] = Field(min_length=1, max_length=500)


def commit(project_id: int, body: CommitIn, env_id: Optional[int] = None) -> dict[str, Any]:
    root = _root(_project(project_id), env_id)
    if not _is_git(root):
        raise HTTPException(status_code=400, detail="This folder isn't a git repository")
    status = _status_map(root)
    paths = [p for p in body.paths if p in status]
    if not paths:
        raise HTTPException(status_code=400, detail="None of those files have changes")
    for p in paths:
        _inside(root, p) if status[p] != "D" else None
    _git(root, "add", "-A", "--", *paths, check=True)
    r = _git(root, "commit", "-F", "-", "--", *paths, check=True, input_text=body.message.strip() + "\n")
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"sha": sha, "files": len(paths), "output": r.stdout.strip()[-400:]}


COMMIT_MSG_PROMPT = ("Write a git commit message for this diff: a subject line of at most 60 characters in the "
                     "imperative mood (conventional-commit prefix like feat:/fix:/docs: when it fits), then a blank line "
                     "and up to 4 short bullet lines. Reply with the message only.")


def suggest_message(project_id: int, paths: list[str], env_id: Optional[int] = None) -> dict[str, Any]:
    root = _root(_project(project_id), env_id)
    diff = _git(root, "diff", "HEAD", "--", *paths).stdout if paths else _git(root, "diff", "HEAD").stdout
    untracked = [p for p, k in _status_map(root).items() if k == "U" and (not paths or p in paths)]
    if untracked:
        diff += "\nNew files: " + ", ".join(untracked[:40])
    if not diff.strip():
        raise HTTPException(status_code=400, detail="Nothing has changed")
    model = router.cheapest_api_model()
    if not model:
        raise HTTPException(status_code=503, detail="No answer-only model is available to write with")
    try:
        out = providers.complete(model, [{"role": "user", "content": diff[:24000]}], COMMIT_MSG_PROMPT, 60)
    except providers.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    db.log_usage("commit-message", model["id"], out.input_tokens, out.output_tokens,
                 providers.estimate_cost(model, out.input_tokens, out.output_tokens))
    return {"text": out.text.strip().strip("`").strip(), "model_id": model["id"]}


def push(project_id: int, env_id: Optional[int] = None) -> dict[str, Any]:
    """Queued as a Claude Code task: git push is a protected command, so it stops
    for your approval like every other push."""
    p = _project(project_id)
    root = _root(p, env_id)
    branch = _branch(root) or "the current branch"
    task = db.add_task(project_id, f"Push {branch} to its remote (git push). Don't change any files or commit anything - "
                                   f"only push what is already committed, then say what was pushed.",
                       0, None, routed_by="direct", route_reason="Changes tab: Push", permission_mode="acceptEdits", env_id=env_id)
    return {"task_id": task["id"], "branch": branch}


# ------------------------------------------------------------------ ideas

def list_ideas(project_id: int) -> list[dict[str, Any]]:
    with db._lock:
        return [db._row_to_dict(r) for r in db.get_conn().execute(
            "SELECT * FROM project_ideas WHERE project_id = ? ORDER BY id DESC", (project_id,)).fetchall()]


def _idea(idea_id: int) -> dict[str, Any]:
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM project_ideas WHERE id = ?", (idea_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Idea not found")
    return db._row_to_dict(r)


class IdeaIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    size: str = "Medium"


class IdeaPatch(BaseModel):
    text: Optional[str] = Field(None, min_length=1, max_length=4000)
    size: Optional[str] = None


SIZES = ("Small", "Medium", "Big")


def add_idea(project_id: int, body: IdeaIn) -> dict[str, Any]:
    _project(project_id)
    now = db._now()
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute("INSERT INTO project_ideas (project_id, text, size, status, created_at, updated_at) VALUES (?, ?, ?, 'parked', ?, ?)",
                           (project_id, body.text.strip(), body.size if body.size in SIZES else "Medium", now, now))
        conn.commit()
    return _idea(cur.lastrowid)


def update_idea(idea_id: int, body: IdeaPatch) -> dict[str, Any]:
    _idea(idea_id)
    with db._lock:
        conn = db.get_conn()
        if body.text is not None:
            conn.execute("UPDATE project_ideas SET text = ?, updated_at = ? WHERE id = ?", (body.text.strip(), db._now(), idea_id))
        if body.size in SIZES:
            conn.execute("UPDATE project_ideas SET size = ?, updated_at = ? WHERE id = ?", (body.size, db._now(), idea_id))
        conn.commit()
    return _idea(idea_id)


def delete_idea(idea_id: int) -> dict[str, Any]:
    _idea(idea_id)
    with db._lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM project_ideas WHERE id = ?", (idea_id,))
        conn.commit()
    return {"ok": True}


def send_idea(idea_id: int, to: str) -> dict[str, Any]:
    """to = 'queue' (a task, Up next) or 'backlog' (a to-do)."""
    idea = _idea(idea_id)
    p = _project(idea["project_id"])
    if to == "queue":
        if not db.has_folder(p) and not db.is_general_project(p):
            raise HTTPException(status_code=400, detail=db.NO_FOLDER_HINT)
        mode = db.get_project_settings(p["id"]).get("default_mode")
        task = db.add_task(p["id"], idea["text"], 0, None, routed_by="direct", route_reason="Ideas: Run", permission_mode=mode)
        fields = ("status = 'queued', task_id = ?", task["id"])
    elif to == "backlog":
        todo = db.add_todo(p["id"], idea["text"])
        fields = ("status = 'backlog', todo_id = ?", todo["id"])
    else:
        raise HTTPException(status_code=400, detail="Send it to 'queue' or 'backlog'")
    with db._lock:
        conn = db.get_conn()
        conn.execute(f"UPDATE project_ideas SET {fields[0]}, updated_at = ? WHERE id = ?", (fields[1], db._now(), idea_id))
        conn.commit()
    return _idea(idea_id)


# ------------------------------------------------------------------ folder browser

def list_dirs(path: str = "~") -> dict[str, Any]:
    """Folders inside `path` (under your home folder), for picking a project's folder."""
    base = Path(os.path.expanduser(path or "~")).resolve()
    if base != HOME and HOME not in base.parents:
        raise HTTPException(status_code=400, detail="Pick a folder inside your home folder")
    if not base.is_dir():
        raise HTTPException(status_code=404, detail="No such folder")
    dirs = []
    try:
        for e in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if e.name.startswith(".") or not e.is_dir() or e.name in SKIP_DIRS or e.name == "Library":
                continue
            dirs.append({"name": e.name, "path": short_path(str(e)), "git": (e / ".git").exists()})
    except PermissionError:
        pass
    return {"path": short_path(str(base)), "parent": short_path(str(base.parent)) if base != HOME else None,
            "git": (base / ".git").exists(), "dirs": dirs[:400]}


# ------------------------------------------------------------------ small actions

def run_todo(todo_id: int) -> dict[str, Any]:
    """Board: drag one backlog item to Up next - queue just that to-do."""
    with db._lock:
        row = db.get_conn().execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="To-do not found")
    todo = db._row_to_dict(row)
    if todo["status"] != "pending":
        raise HTTPException(status_code=409, detail="That item is already queued or done")
    p = _project(todo["project_id"])
    if not db.has_folder(p) and not db.is_general_project(p):
        raise HTTPException(status_code=400, detail=db.NO_FOLDER_HINT)
    import phases
    decisions = phases.todo_decisions(p, [todo])
    db.run_checked_todos(p["id"], decisions=decisions, only_ids=[todo_id])
    with db._lock:
        row = db.get_conn().execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return db._row_to_dict(row)


def reveal(project_id: int, env_id: Optional[int] = None) -> dict[str, Any]:
    """Open the project's folder in Finder on this Mac."""
    root = _root(_project(project_id), env_id)
    try:
        den_platform.open_path(str(root))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(status_code=500, detail=f"Couldn't open {den_platform.file_manager_name()}: {e}")
    return {"ok": True, "path": short_path(str(root))}


# ------------------------------------------------------------------ routes

def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    r.get("/den/projects")(project_states)
    r.get("/den/projects/{project_id}/tree")(tree)

    @r.get("/den/projects/{project_id}/file")
    def get_file(project_id: int, path: str, env_id: Optional[int] = None):
        return read_file(project_id, path, env_id)

    r.put("/den/projects/{project_id}/file")(write_file)
    r.get("/den/projects/{project_id}/changes")(changes)
    r.post("/den/projects/{project_id}/commit")(commit)

    class SuggestIn(BaseModel):
        paths: list[str] = []

    @r.post("/den/projects/{project_id}/commit-message")
    def commit_message(project_id: int, body: SuggestIn, env_id: Optional[int] = None):
        return suggest_message(project_id, body.paths, env_id)

    r.post("/den/projects/{project_id}/push")(push)
    r.get("/den/projects/{project_id}/ideas")(list_ideas)
    r.post("/den/projects/{project_id}/ideas")(add_idea)
    r.patch("/den/ideas/{idea_id}")(update_idea)
    r.delete("/den/ideas/{idea_id}")(delete_idea)

    @r.post("/den/ideas/{idea_id}/send")
    def send(idea_id: int, to: str):
        return send_idea(idea_id, to)

    r.post("/den/todos/{todo_id}/run")(run_todo)
    r.post("/den/projects/{project_id}/reveal")(reveal)

    @r.get("/den/fs/dirs")
    def dirs(path: str = "~"):
        return list_dirs(path)

    return r
