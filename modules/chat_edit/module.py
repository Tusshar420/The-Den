"""
Edit a sent chat message and run it again, like ChatGPT.

The dashboard does it in two steps: this module's command "rewinds" the chat to just before the
message, then the page sends the edited text the usual way (POST /projects/{id}/tasks), so routing,
model, mode and account work exactly as for any other message.

One command (POST /modules/commands/chat-rewind, {"arg": "{\"task_id\": 123}"}):
  - the message and every later one in its chat go to the recycle bin (restorable, nothing erased);
  - if it was the chat's first message the whole chat goes, and the page starts it again as a new chat;
  - otherwise every message left loses its Claude Code session, so the next run doesn't --resume a
    conversation that still holds the replaced turns. It starts fresh and is given the chat so far
    in writing instead (worker.catch_up_prompt), the same as when a chat moves to another project.
Returns {"root", "whole_chat", "binned": [ids]}, or {"error": "<why, in words>"} - refused while
anything in the chat is running or paused on a usage limit. (An exception would reach the page only as
"ValueError": the module registry keeps just the exception's type.)

A module, so a change here needs POST /modules/chat_edit/reload, never a restart.
"""

import json

import db

BUSY = ("running", "rate_limited")


def rewind(arg: str) -> dict:
    try:
        task_id = int(json.loads(arg or "{}")["task_id"])
    except (ValueError, KeyError, TypeError):
        raise ValueError('Send {"task_id": <id of the message to edit>}')
    task = db.get_task(task_id)
    if not task or task["status"] == "deleted":
        raise ValueError("That message isn't there any more")
    root = task["thread_id"] or task["id"]
    thread = db.get_thread(task_id)
    busy = next((t for t in thread if t["status"] in BUSY), None)
    if busy:
        raise ValueError(f"Message #{busy['id']} in this chat is still under way - wait for it to finish or stop it first")
    if task_id == root:
        return {"root": root, "whole_chat": True, "binned": db.delete_task(root)}
    binned: list[int] = []
    for t in reversed([t for t in thread if t["id"] >= task_id]):
        binned += db.delete_task(t["id"])
    for t in thread:      # the binned ones too: the worker resumes the newest session in the chat, binned or not
        if t.get("session_id"):
            db.set_task_session(t["id"], None)
    return {"root": root, "whole_chat": False, "binned": binned}


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        ctx.register_command("chat-rewind", self.run, "Bin a chat message and everything after it, so it can be sent again edited")

    def run(self, arg: str) -> dict:
        try:
            out = rewind(arg)
        except ValueError as e:
            return {"error": str(e)}
        self.ctx.log.info("rewound chat %s: binned %s", out["root"], out["binned"])
        return out
