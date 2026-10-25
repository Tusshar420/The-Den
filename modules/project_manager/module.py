"""
Project manager: an AI that runs a project the way a human project manager would.

1. interview  - asks you what you want, one question at a time, with a suggested answer.
2. brief      - writes a project brief from your answers; you edit and confirm it. It is saved
                as a project Guideline, so every task in the project sees it.
3. plan       - splits the work into phases (design, frontend, backend, testing...), each task
                given to a specialist role from roles.json. You edit and approve it.
4. building   - the phases become normal Den phases (phases.py) in manual mode, so Octopus works
                through one phase's tasks one at a time and never starts the next phase by itself.
                Each task carries its role's instructions in its text.
5. review     - when a phase is done, the manager reads its report and writes a short review.
                You start the next phase or ask for changes (a fix-up task in the same phase).
6. finished   - the last phase was reviewed.

The dashboard's "Project Manager" project tab (static/den/DenProjectManager.dc.html) drives it
through the pm-* commands; each takes a JSON string with at least {"project_id": n}.
Commands never raise: a problem comes back as {"error": "why"} so the page can say it.

A module, so a change here needs POST /modules/project_manager/reload, never a restart.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db
import phases
import providers
import router

ROLES_FILE = Path(__file__).with_name("roles.json")
CHECK_SECONDS = 30
STAGES = ("interview", "brief", "plan", "building", "review", "finished")
MAX_PHASES, MAX_TASKS = 8, 12
BRIEF_TITLE = "Project brief (from the Project Manager)"

INTERVIEW_PROMPT = """You are an experienced project manager starting a new project with its owner.
The owner is not technical. Your job right now is to understand what they want before any work starts.

Ask ONE short, plain-English question at a time. Cover what matters for this kind of project, for a
website usually: what it is for and who will use it, the pages and main features, sign-in or accounts,
payments, content (who writes it, any images), look and feel (examples they like, colours, brand),
any technology or hosting they already have, and the deadline. Skip anything they already told you.
With each question give a sensible suggestion they can just accept.

You have asked {asked} of at most {limit} questions. When you know enough to write a clear brief
(or you reach the limit), stop asking.

Reply with ONLY a JSON object, no other text:
{{"question": "<your next question>", "suggestion": "<a suggested answer, or empty>"}}
or, when you have enough:
{{"ready": true}}"""

BRIEF_PROMPT = """You are an experienced project manager. From the conversation with the owner, write the project brief
that the whole team will work from. Use these markdown sections:

# <project name>
## Goal
## Who it is for
## Pages and features
## Look and feel
## Technology
## Out of scope
## Done means

Be specific and short. Where the owner said "not sure" or gave no answer, write the sensible default
you chose and mark it "(assumed)". Reply with ONLY the brief."""

PLAN_PROMPT = """You are an experienced project manager planning the work for a team of specialist AI agents.
Each task goes to an AI coding agent that works inside the project's folder, one task at a time,
top to bottom, and can read and write files and run commands. It sees the project brief but NOT the
other tasks, so every task must be a complete, self-contained instruction someone could finish in one
sitting. Order tasks so earlier ones unblock later ones.

Split the work into {min_phases} to {max_phases} phases in order (for a website, typically: setup and
design, frontend, backend, testing and polish, launch prep). Give each phase 2 to {max_tasks} tasks.
End with a testing phase. Never plan anything that deploys to a live server or spends money; plan a
task that writes the steps down instead.

Give every task to exactly one of these roles (use the id):
{roles}

Reply with ONLY a JSON object, no other text:
{{"phases": [{{"name": "...", "goal": "<one sentence>", "tasks": [{{"role": "<role id>", "text": "<the instruction>"}}]}}]}}"""

REVIEW_PROMPT = """You are the project manager. A phase of the project just finished. Below are the project brief and
the phase's report: what each task did, and anything that failed.

Write a short review for the owner, who is not technical, in plain text (no markdown, no lists):
what got done, anything that looks wrong, missing or risky compared with the brief, and what the next
phase will do. 4 to 8 short sentences. Address the owner as "you". Use only facts from the report;
if something isn't there, don't guess."""


class PMError(ValueError):
    pass


def roles() -> list[dict[str, str]]:
    return json.loads(ROLES_FILE.read_text())["roles"]


def role_map() -> dict[str, dict[str, str]]:
    return {r["id"]: r for r in roles()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model's reply, even inside a code fence or with words around it."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise PMError("The manager's answer wasn't in the expected shape - try again")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise PMError("The manager's answer wasn't in the expected shape - try again")
    if not isinstance(data, dict):
        raise PMError("The manager's answer wasn't in the expected shape - try again")
    return data


def clean_plan(plan: Any) -> dict[str, Any]:
    """A plan made safe to store: known roles only, no empty phases or tasks, sensible sizes."""
    known = role_map()
    raw = (plan or {}).get("phases") if isinstance(plan, dict) else None
    if not isinstance(raw, list) or not raw:
        raise PMError("The plan has no phases")
    out = []
    for p in raw[:MAX_PHASES]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()[:phases.NAME_MAX]
        tasks = []
        for t in (p.get("tasks") or [])[:MAX_TASKS]:
            if not isinstance(t, dict):
                continue
            text = str(t.get("text") or "").strip()
            role = str(t.get("role") or "").strip()
            if not text:
                continue
            if role not in known:
                raise PMError(f"The plan gives a task to a role I don't know: '{role}'")
            tasks.append({"role": role, "text": text[:3000]})
        if name and tasks:
            out.append({"name": name, "goal": str(p.get("goal") or "").strip()[:phases.GOAL_MAX], "tasks": tasks})
    if not out:
        raise PMError("The plan has no phases with tasks in them")
    return {"phases": out}


def todo_text(task: dict[str, str]) -> str:
    """What a task's todo says: the role and the task on top (what lists show), its instructions below."""
    role = role_map()[task["role"]]
    return f"[{role['label']}] {task['text']}\n\n{role['instructions']}"


def task_view(todo: dict[str, Any]) -> dict[str, Any]:
    """A phase's todo as the tab lists it: todo_text split back into the role and the task, plus how it went."""
    text, role = todo["text"], ""
    for r in roles():
        head = f"[{r['label']}] "
        if text.startswith(head):
            role, text = r["label"], text[len(head):]
            tail = "\n\n" + r["instructions"]
            if text.endswith(tail):
                text = text[:-len(tail)]
            break
    return {"id": todo["id"], "task_id": todo.get("task_id"), "role": role, "text": text.strip(),
            "state": phases.todo_state(todo), "result": (todo.get("task_result_summary") or "")[:4000],
            "error": (todo.get("task_error_message") or "")[:1000]}


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        self._locks: dict[int, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        for name, fn, desc in (
            ("pm-status", self.status, "Where the project manager is for a project"),
            ("pm-chat", self.chat, "Answer the project manager's question"),
            ("pm-brief", self.brief, "Stop the questions and write the brief"),
            ("pm-confirm-brief", self.confirm_brief, "Save the brief and draft the plan"),
            ("pm-plan", self.plan, "Draft the plan again, optionally with a note"),
            ("pm-save-plan", self.save_plan, "Save your edits to the plan"),
            ("pm-approve", self.approve, "Approve the plan: make the phases and start the first"),
            ("pm-next", self.next_phase, "Start the next phase after a review"),
            ("pm-changes", self.changes, "Add a fix-up task to the phase just reviewed and run it"),
            ("pm-reset", self.reset, "Start over with the project manager (phases already made stay)"),
        ):
            ctx.register_command(name, self._command(fn), desc)

    def start(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="den-project-manager")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=30)

    def _loop(self):
        while not self._stop.wait(CHECK_SECONDS):
            try:
                self.check()
            except Exception:
                self.ctx.log.exception("Project manager check failed")

    # ----------------------------------------------------------------- plumbing

    def _lock(self, pid: int) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(pid, threading.Lock())

    def _command(self, fn):
        def run(arg: str):
            try:
                data = json.loads(arg or "{}")
                pid = int(data.get("project_id"))
            except (ValueError, TypeError, AttributeError):
                return {"error": "Which project? Send {\"project_id\": n}"}
            if not db.get_project(pid):
                return {"error": "Project not found"}
            try:
                with self._lock(pid):
                    return fn(pid, data)
            except (PMError, phases.PhaseError, providers.ProviderError) as e:
                return {"error": str(e)}
            except Exception as e:
                self.ctx.log.exception("Project manager command failed")
                return {"error": f"Something went wrong: {e}"}
        return run

    def load(self, pid: int) -> dict[str, Any]:
        return self.ctx.get_state(f"p{pid}") or {"stage": "interview", "chat": [], "brief": "", "brief_note_id": None,
                                                  "plan": None, "phase_ids": [], "current": None, "reviews": {}}

    def save(self, pid: int, state: dict[str, Any]) -> None:
        self.ctx.set_state(f"p{pid}", state)
        index = self.ctx.get_state("projects", [])
        if pid not in index:
            self.ctx.set_state("projects", index + [pid])

    def model(self) -> dict[str, Any]:
        """The model picked in config if it can answer now, else the cheapest one that can."""
        picked = router.get_model(self.ctx.config.get("model") or None)
        if picked and picked.get("provider") in providers.COMPLETERS and router.availability(picked)[0]:
            return picked
        fallback = router.cheapest_api_model()
        if not fallback:
            raise PMError("No AI model is available right now - try again in a bit")
        return fallback

    def ask(self, pid: int, system: str, messages: list[dict[str, str]]) -> str:
        model = self.model()
        out = providers.complete(model, messages, system, 300)
        db.log_usage("project_manager", model["id"], out.input_tokens, out.output_tokens,
                     providers.estimate_cost(model, out.input_tokens, out.output_tokens), project_id=pid)
        return out.text

    def view(self, pid: int, state: dict[str, Any], **extra: Any) -> dict[str, Any]:
        project = db.get_project(pid)
        views = []
        todos = {t["id"]: t for t in db.list_todos(pid)} if state.get("phase_ids") else {}
        reviews = state.get("reviews") or {}
        for i in state.get("phase_ids") or []:
            p = db.get_phase(i)
            if p:
                v = phases.view(p)
                v["tasks"] = [task_view(todos[t]) for t in v["todo_ids"] if t in todos]
                v["review"] = reviews.get(str(i))
                views.append(v)
        current = next((v for v in views if v["id"] == state.get("current")), None)
        review = (state.get("reviews") or {}).get(str(state.get("current"))) if state.get("current") else None
        return {**state, "project": {"id": pid, "name": project["name"], "has_folder": db.has_folder(project)},
                "roles": [{"id": r["id"], "label": r["label"]} for r in roles()],
                "phases": views, "current_phase": current, "review": review,
                "max_questions": int(self.ctx.config.get("max_questions", 8)), **extra}

    def _stage(self, state: dict[str, Any], *allowed: str) -> None:
        if state["stage"] not in allowed:
            raise PMError(f"That can't be done now - the project manager is at the '{state['stage']}' step")

    # ----------------------------------------------------------------- commands

    def status(self, pid: int, data: dict) -> dict[str, Any]:
        return self.view(pid, self.load(pid))

    def chat(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "interview")
        text = str(data.get("text") or "").strip()
        if not text:
            raise PMError("Type an answer first")
        chat = state["chat"] + [{"role": "user", "text": text[:4000]}]
        asked = sum(1 for m in chat if m["role"] == "assistant")
        limit = max(1, int(self.ctx.config.get("max_questions", 8)))
        reply: dict[str, Any] = {"ready": True}
        if asked < limit:
            reply = parse_json(self.ask(pid, INTERVIEW_PROMPT.format(asked=asked, limit=limit), self._messages(chat)))
        if reply.get("ready") or not str(reply.get("question") or "").strip():
            state["chat"] = chat
            return self._write_brief(pid, state)
        question = str(reply["question"]).strip()
        suggestion = str(reply.get("suggestion") or "").strip()
        chat.append({"role": "assistant", "text": question, "suggestion": suggestion})
        state["chat"] = chat
        self.save(pid, state)
        return self.view(pid, state)

    def brief(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "interview", "brief")
        if not any(m["role"] == "user" for m in state["chat"]):
            raise PMError("Tell the project manager what you want to build first")
        return self._write_brief(pid, state)

    def _write_brief(self, pid: int, state: dict[str, Any]) -> dict[str, Any]:
        text = self.ask(pid, BRIEF_PROMPT, self._messages(state["chat"]) + [
            {"role": "user", "content": "That's everything - please write the brief now."}]).strip()
        if not text:
            raise PMError("The manager didn't write a brief - try again")
        state.update(stage="brief", brief=text)
        self.save(pid, state)
        return self.view(pid, state)

    def confirm_brief(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "brief", "plan")
        text = str(data.get("brief") or state["brief"] or "").strip()
        if not text:
            raise PMError("The brief is empty")
        note_text = f"{BRIEF_TITLE}\n\n{text}"
        note = db.get_project_note(state["brief_note_id"]) if state.get("brief_note_id") else None
        if note and note["project_id"] == pid:
            db.update_project_note(note["id"], text=note_text)
        else:
            state["brief_note_id"] = db.add_project_note(pid, note_text, kind="requirement")["id"]
        state.update(stage="plan", brief=text, plan=None)
        self.save(pid, state)
        try:
            return self._write_plan(pid, state)
        except (PMError, providers.ProviderError) as e:   # the brief is saved; the page offers "try again"
            return self.view(pid, state, error=str(e))

    def plan(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "plan")
        return self._write_plan(pid, state, str(data.get("note") or "").strip())

    def _write_plan(self, pid: int, state: dict[str, Any], note: str = "") -> dict[str, Any]:
        role_lines = "\n".join(f"- {r['id']}: {r['label']} - {r['instructions'].split('. ')[0]}" for r in roles())
        system = PLAN_PROMPT.format(min_phases=3, max_phases=6, max_tasks=MAX_TASKS, roles=role_lines)
        ask = f"Project: {db.get_project(pid)['name']}\n\nBrief:\n{state['brief']}"
        if note and state.get("plan"):
            ask += f"\n\nYour previous plan:\n{json.dumps(state['plan'])}\n\nThe owner wants this changed: {note}"
        state["plan"] = clean_plan(parse_json(self.ask(pid, system, [{"role": "user", "content": ask}])))
        self.save(pid, state)
        return self.view(pid, state)

    def save_plan(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "plan")
        state["plan"] = clean_plan(data.get("plan"))
        self.save(pid, state)
        return self.view(pid, state)

    def approve(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "plan")
        if not state.get("plan"):
            raise PMError("There is no plan to approve yet")
        if not db.has_folder(db.get_project(pid)):
            raise PMError(db.NO_FOLDER_HINT)
        plan = clean_plan(state["plan"])
        phases.set_mode(pid, "manual")       # the next phase only starts when you say so
        ids = []
        for p in plan["phases"]:
            ids.append(phases.create(pid, p["name"], p["goal"] or None, todos=[todo_text(t) for t in p["tasks"]])["id"])
        state.update(stage="building", plan=plan, phase_ids=ids, current=ids[0])
        self.save(pid, state)
        phases.start(ids[0])
        return self.view(pid, state)

    def next_phase(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "review")
        nxt = self._next_after(state)
        if not nxt:
            state["stage"] = "finished"
            self.save(pid, state)
            return self.view(pid, state)
        phases.start(nxt["id"])
        state.update(stage="building", current=nxt["id"])
        self.save(pid, state)
        return self.view(pid, state)

    def changes(self, pid: int, data: dict) -> dict[str, Any]:
        state = self.load(pid)
        self._stage(state, "review", "finished")
        note = str(data.get("note") or "").strip()
        if not note:
            raise PMError("Say what should change")
        phase_id = state["current"]
        role = str(data.get("role") or "")
        if role not in role_map():
            n = state["phase_ids"].index(phase_id) if phase_id in state["phase_ids"] else 0
            tasks = state["plan"]["phases"][n]["tasks"] if n < len(state["plan"]["phases"]) else []
            role = tasks[0]["role"] if tasks else "frontend"
        phases.reopen(phase_id)
        db.set_todo_phase(db.add_todo(pid, todo_text({"role": role, "text": "Changes the owner asked for after "
                                                      "reviewing this phase: " + note}))["id"], phase_id)
        phases.start(phase_id)
        state["stage"] = "building"
        self.save(pid, state)
        return self.view(pid, state)

    def reset(self, pid: int, data: dict) -> dict[str, Any]:
        self.ctx.set_state(f"p{pid}", None)
        self.ctx.set_state("projects", [p for p in self.ctx.get_state("projects", []) if p != pid])
        return self.view(pid, self.load(pid))

    # ------------------------------------------------------------- background

    def check(self) -> int:
        """Review each finished phase once (again if it was reopened and finished later). Returns how many."""
        written = 0
        for pid in list(self.ctx.get_state("projects", [])):
            if not db.get_project(pid):
                continue
            with self._lock(pid):
                state = self.load(pid)
                phase = db.get_phase(state["current"]) if state["stage"] == "building" and state.get("current") else None
                if not phase or phase["status"] != "done":
                    continue
                if (state["reviews"].get(str(phase["id"])) or {}).get("finished_at") == phase["finished_at"]:
                    continue
            text = self._review_text(pid, state, phase)     # slow: outside the lock, so the tab stays usable
            with self._lock(pid):
                state = self.load(pid)
                if state["stage"] != "building" or state.get("current") != phase["id"]:
                    continue                                # you moved on while it was being written
                state["reviews"][str(phase["id"])] = {"phase": phase["name"], "finished_at": phase["finished_at"],
                                                      "at": _now(), "text": text}
                state["stage"] = "review" if self._next_after(state) else "finished"
                self.save(pid, state)
                written += 1
        return written

    def _review_text(self, pid: int, state: dict[str, Any], phase: dict[str, Any]) -> str:
        nxt = self._next_after(state)
        try:
            report = phases.report(pid, [phase["id"]], overview=False)["markdown"]
            ask = (f"Brief:\n{state['brief']}\n\nReport on the phase '{phase['name']}':\n{report[:30000]}\n\n"
                   + (f"The next phase is '{nxt['name']}': {nxt.get('goal') or ''}" if nxt else "This was the last phase."))
            return self.ask(pid, REVIEW_PROMPT, [{"role": "user", "content": ask}]).strip()
        except Exception as e:   # the owner must never be stuck waiting on a review that can't be written
            self.ctx.log.warning("Project manager review not written: %s", e)
            return (f"The phase '{phase['name']}' is done. I couldn't write a review this time ({e}), "
                    "so please look at the Report tab for what was done.")

    def _next_after(self, state: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The first of this plan's phases, after the current one, that isn't done."""
        ids = state.get("phase_ids") or []
        after = ids[ids.index(state["current"]) + 1:] if state.get("current") in ids else ids
        for i in after:
            p = db.get_phase(i)
            if p and p["status"] != "done":
                return p
        return None

    @staticmethod
    def _messages(chat: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{"role": m["role"], "content": m["text"]} for m in chat]
