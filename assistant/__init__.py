"""
Octopus Personal Assistant layer.

Models think, agents specialize, tools perform actions, Octopus orchestrates.
This package is the "tools" side for everyday tasks (email, calendar,
reminders), wired into the existing router / queue / approvals rather than
beside them:

    request -> intent.py      deterministic router: intent, tool, capabilities,
                              approval policy, whether a model is needed
            -> router.py      sends it to the 'assistant' lane (worker.py)
            -> orchestrator   runs it: a plain tool call when no model is
                              needed, otherwise planner.py (a model that only
                              *proposes* tool calls)
            -> actions.py     the one gate every tool call passes: capability
                              policy (ALLOW / ASK / DENY), approval, audit log
            -> tools/         provider-independent Tool classes
            -> providers/     Gmail / Google Calendar (Microsoft next)
            -> connections    OAuth accounts; tokens live in the Keychain

Adding a tool later (Slack, Drive, GitHub...): write a Tool subclass in
tools/, register it in tools/__init__.py, add its capabilities to
capabilities.py. Routing, approvals, the activity log and the dashboard's
approval cards pick it up without further changes.

Submodules are imported explicitly by their users - nothing is imported here
so router.py can use intent.py without pulling in the provider code.
"""
