"""
Analytics for the owner.

One command, run from the dashboard (POST /modules/commands/den-analytics, {"arg": "{\"days\": 30}"}):
it returns a read-only report of how the Den is being used - which features and AI models did the
work, which Claude accounts and outside providers are connected and used, how many emails went out,
how many tasks ran and what they cost, which modules are on, and (on the owner's Den) which
given-away Dens are checking in. report.py builds it; this just wires the command in.

A module, so a change here needs POST /modules/den_analytics/reload, never a restart.
"""

import json
from typing import Any

from . import report


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        ctx.register_command("den-analytics", self.run, "How the Den is being used")

    def run(self, arg_text: str) -> dict[str, Any]:
        days = self.ctx.config.get("default_days", 30)
        if (arg_text or "").strip():
            try:
                body = json.loads(arg_text)
                if isinstance(body, dict) and body.get("days") is not None:
                    days = int(body["days"])
            except (ValueError, TypeError):
                pass
        return report.build(days)
