"""
The coding agent as a tool: how a request that starts in the mailbox ("fix
the Salesforce bug mentioned in this email") crosses into Claude Code. The
assistant reads and understands; Claude Code does the work in a registered
project, through the normal queue and its normal Bash approvals.

Handing work to an agent that can edit files is outbound: it needs AGENT_CODING
(ask first by default), and the approval card shows the exact task text.
"""

import db
from assistant.tools import ActionSpec, Prepared, Tool, preview, register
from assistant.tools.workspace import find_project


class AgentTool(Tool):
    id = "agent"
    label = "Coding agent"
    actions = {
        "code": ActionSpec(
            "code", "Queue a coding task for Claude Code in one of the owner's registered projects. Needs approval. "
            "Write the task so it stands alone - Claude Code will not see this conversation.",
            {"project": "name of a registered project", "prompt": "the complete task"}),
    }

    def prepare_code(self, args, ctx):
        project = find_project(args["project"])
        prompt = args["prompt"]
        fields = [("Project", project["name"]), ("Task", prompt),
                  ("Action", f"Queue a Claude Code task in {project['name']} - it can edit files there")]
        return Prepared({"project_id": project["id"], "project": project["name"], "prompt": prompt}, ["AGENT_CODING"],
                        f"{project['name']}: {prompt[:60]}", f"Hand a task to Claude Code in {project['name']}",
                        preview("agent", "CODING TASK APPROVAL", "Queue coding task", fields, "Queue task", blocks=("Task",)))

    def run_code(self, args, ctx):
        task = db.add_task(args["project_id"], args["prompt"], 0, None, lane="agent", model_id="claude-code",
                           category="coding", route_reason="handed off by the personal assistant", routed_by="assistant")
        return {"text": f"Queued as task #{task['id']} in {args['project']}. Claude Code will pick it up from the queue.",
                "data": {"task_id": task["id"]}, "ref": str(task["id"])}


register(AgentTool())
