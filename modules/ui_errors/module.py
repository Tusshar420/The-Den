"""
Dashboard errors, kept on the server.

When a dashboard page fails to draw, the runtime (support.js) shows a red line with the message and
den.js sends the details here: which page, the message, the stack (where in the code it failed), the
address and the browser. Without this they only lived in that one browser tab.

Two commands (POST /modules/commands/<name>, {"arg": "<json>"}):
  den-ui-error   {"page","message","stack","hash","agent"}   the dashboard calls this
  den-ui-errors  ""                                          the last errors, newest first

Kept in this module's own state (ctx.set_state), the last MAX_KEEP only.
A module, so a change here needs POST /modules/ui_errors/reload, never a restart.
"""

import json
from datetime import datetime, timezone

MAX_KEEP = 30
LIMITS = {"page": 80, "message": 500, "stack": 6000, "hash": 300, "agent": 300}


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        ctx.register_command("den-ui-error", self.record, "Record a dashboard error")
        ctx.register_command("den-ui-errors", self.recent, "The last dashboard errors, newest first")

    def record(self, arg: str) -> str:
        try:
            v = json.loads(arg or "{}")
        except ValueError:
            return "bad json"
        if not isinstance(v, dict) or not v.get("message"):
            return "nothing to record"
        item = {k: str(v.get(k) or "")[:n] for k, n in LIMITS.items()}
        item["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        items = [item] + list(self.ctx.get_state("items", []) or [])
        self.ctx.set_state("items", items[:MAX_KEEP])
        return "ok"

    def recent(self, arg: str = "") -> str:
        return json.dumps(self.ctx.get_state("items", []) or [])
