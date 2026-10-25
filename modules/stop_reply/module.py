"""
Stop an AI mid-reply, like ChatGPT's stop button, so it burns no more tokens.

One command (POST /modules/commands/stop-reply, {"arg": "{\"task_id\": 123}"}) stops everything still
under way in the chat that task belongs to:
  - a running Claude Code task (agent lane): its process is killed, as POST /tasks/{id}/stop does;
    the steps it already showed stay in the chat;
  - a running model answer (api lane): its run is cancelled on the worker's event loop and the task
    marked stopped, so the chat is free at once. The call already sent to the provider can't be
    pulled back, so that one answer may still be billed; nothing after it is;
  - messages still waiting in line (queued, or held on a usage limit): cancelled before they start.
An assistant-lane request (mail, calendar) can't be stopped midway - it runs as one call in a
thread and finishes within seconds - so it is reported back as not stopped.
Returns {"stopped": [ids], "cancelled": [ids]}, or {"error": "<why, in words>"} when nothing could be
stopped. (An exception would reach the page only as its type name, so errors come back as text.)

The API runs in the same process as the worker (main.py), so this reaches worker's live runs
directly. A module, so a change here needs POST /modules/stop_reply/reload, never a restart.
"""

import asyncio
import json

import db
import worker

WAITING = ("queued", "rate_limited")


def _stop_api_run(task: dict) -> bool:
    """Cancel the api lane's run of this task on the worker's event loop. Done on the loop itself,
    so it can't land between the answer arriving and it being saved: either the run is cancelled
    before it saves, or it has already finished and there is nothing to stop."""
    lane = worker._api_lane_wake
    run = worker._api_runs.get(task.get("model_id") or "")
    if not lane or not run:
        return False
    loop = lane[0]
    try:
        if asyncio.get_running_loop() is loop:
            return False     # called on the loop itself: waiting below would never end
    except RuntimeError:
        pass                 # the usual case: an API request's worker thread

    async def cancel() -> bool:
        if run.done():
            return False
        run.cancel()
        db.mark_stopped(task["id"])
        return True

    try:
        return asyncio.run_coroutine_threadsafe(cancel(), loop).result(timeout=5)
    except Exception:
        return False


def stop_one(task: dict) -> str:
    """'stopped', 'cancelled', or why it couldn't be."""
    if task["status"] in WAITING:
        db.cancel_task(task["id"])
        return "cancelled"
    lane = task.get("lane") or "agent"
    if lane == "agent":
        if task.get("paused"):
            worker.resume_task(task["id"])    # a frozen process can't act on the kill
        return "stopped" if worker.stop_task(task["id"]) else "It is just starting - try Stop again in a moment"
    if lane == "api":
        return "stopped" if _stop_api_run(task) else "It had already finished"
    return "Requests about mail and calendar finish in a few seconds and can't be stopped midway"


def stop_chat(arg: str) -> dict:
    try:
        task_id = int(json.loads(arg or "{}")["task_id"])
    except (ValueError, KeyError, TypeError):
        raise ValueError('Send {"task_id": <id of any message in the chat>}')
    if not db.get_task(task_id):
        raise ValueError("That message isn't there any more")
    busy = [t for t in db.get_thread(task_id) if t["status"] == "running" or t["status"] in WAITING]
    if not busy:
        raise ValueError("Nothing is running in this chat")
    out: dict = {"stopped": [], "cancelled": []}
    why = []
    # Newest first, so a waiting message can't start in the gap after the running one is stopped.
    for t in reversed(busy):
        res = stop_one(t)
        if res in out:
            out[res].append(t["id"])
        else:
            why.append(res)
    if not out["stopped"] and not out["cancelled"]:
        raise ValueError(why[0] if why else "Nothing could be stopped")
    return out


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        ctx.register_command("stop-reply", self.run, "Stop whatever AI work is running or waiting in a chat")

    def run(self, arg: str) -> dict:
        try:
            out = stop_chat(arg)
        except ValueError as e:
            return {"error": str(e)}
        self.ctx.log.info("stopped %s, cancelled %s", out["stopped"], out["cancelled"])
        return out
