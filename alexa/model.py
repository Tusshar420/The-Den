"""
The skill's interaction model - what Alexa listens for. Generated, because the PROJECT slot lists your
registered projects (with spoken forms, so "code aura seven" resolves to KodeAura7):

    .venv/bin/python -m alexa.model > setup/alexa/interaction-model.json
    .venv/bin/python -m alexa.model --invocation "my octopus"

Paste the output into the developer console (Build > Interaction Model > JSON Editor) for each locale you
use (English (IN), (US), (UK) all take the same file), then Save and Build. Run it again after adding a
project; the skill still understands unlisted names, just less reliably.

ChatIntent's "{utterance}" with a CATCH_ALL slot is what lets you speak freely: Alexa hands over whatever
you said when no other intent fits.
"""

import argparse
import json
import re
from typing import Any

INVOCATION = "my octopus"

SAMPLES: dict[str, list[str]] = {
    "QueueIntent": [
        "what's in the queue", "what is in the queue", "what's currently in the queue", "what is currently in the queue",
        "what's queued", "what is queued up", "what's waiting in the queue", "what's next", "what is next in the queue",
        "read me the queue", "read the queue", "how long is the queue", "queue status", "what's in line", "show the queue",
    ],
    "WorkingOnIntent": [
        "what is octopus working on", "what's octopus working on", "what are you working on", "what is octopus doing",
        "what's octopus doing", "what are you doing", "what is running", "what's running right now", "what's running",
        "what is octopus working on right now", "how is the current task going", "what's the current task",
        "status of the current task", "how far along is it", "what's claude working on", "what is claude code doing",
    ],
    "ApprovalsIntent": [
        "what approvals are waiting", "what approvals are waiting for me", "any approvals", "do i have any approvals",
        "what needs my approval", "what needs my attention", "approvals", "read my approvals", "what's waiting for me",
        "is anything waiting for me", "does anything need my approval", "check approvals", "what needs me",
    ],
    "ApproveIntent": [
        "approve", "approve it", "approve that", "approve this", "approved", "allow it", "allow that", "let it run",
        "go ahead and approve", "approve with code {code}", "approve it with code {code}", "approve code {code}",
    ],
    "DenyIntent": [
        "deny", "deny it", "deny that", "reject", "reject it", "don't allow it", "do not allow it", "block it", "decline",
        "decline it", "refuse it",
    ],
    "PinIntent": ["my code is {code}", "the code is {code}", "code {code}", "approval code {code}", "it's {code}"],
    "SkipIntent": ["skip", "skip it", "skip that one", "next", "next one", "ask me later", "later", "not now", "leave it for later"],
    "AddTaskIntent": [
        "add a new task", "add a task", "add a new task to the queue", "add a task to the queue", "queue a task",
        "queue a new task", "new task", "create a task", "create a new task", "add a task for {project}",
        "add a task to {project}", "add a new task for {project}", "add a new task to {project}", "queue a task in {project}",
        "queue a task for {project}", "create a task for {project}", "new task for {project}",
    ],
    "PushIntent": [
        "push the current code to github", "push the code to github", "push to github", "push my code", "push the code",
        "push the changes", "push my changes", "push my changes to github", "commit and push", "push {project} to github",
        "push the {project} code to github", "push the current code for {project}", "push the code in {project} to github",
        "commit and push {project}", "push {project}",
    ],
    "DiscussIntent": [
        "let's discuss a task", "let's talk about a task", "i want to discuss a task", "let's plan a task",
        "let's plan a new feature", "i want to talk about a new feature", "let's talk", "let's discuss something",
        "let's brainstorm", "i want to talk something through", "let's talk about {project}", "let's discuss {project}",
        "i want to discuss a task for {project}", "let's plan a task for {project}", "let's plan something for {project}",
        "i want to talk about {project}", "let's discuss a task for {project}", "let's talk about a new feature for {project}",
    ],
    "QueueItIntent": [
        "queue it", "queue that", "add it to the queue", "make it a task", "make that a task", "send it to claude code",
        "go ahead and build it", "build it", "start working on it", "turn this into a task", "hand it over",
        "that's it queue it", "okay queue it",
    ],
    "EndDiscussionIntent": ["stop discussing", "end the discussion", "end the conversation", "leave the discussion",
                            "exit the discussion", "we're done discussing"],
    "ContinueIntent": ["go on", "what did you say", "what did you find", "are you done", "tell me", "is it ready",
                       "what's the answer", "what did octopus say", "did you finish", "and"],
    "InboxIntent": ["what did i miss", "what's new", "any news", "what happened while i was away", "read my messages",
                    "read my updates", "catch me up"],
    "StopTaskIntent": ["stop the current task", "cancel the current task", "stop the running task", "abort the current task",
                       "kill the current task", "stop what you're doing", "stop claude code"],
    "ChatIntent": ["{utterance}"],
}
SLOTS: dict[str, dict[str, str]] = {
    "ApproveIntent": {"code": "AMAZON.NUMBER"},
    "PinIntent": {"code": "AMAZON.NUMBER"},
    "AddTaskIntent": {"project": "PROJECT"},
    "PushIntent": {"project": "PROJECT"},
    "DiscussIntent": {"project": "PROJECT"},
    "ChatIntent": {"utterance": "CATCH_ALL"},
}
BUILT_IN = ["AMAZON.YesIntent", "AMAZON.NoIntent", "AMAZON.HelpIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent",
            "AMAZON.NavigateHomeIntent", "AMAZON.FallbackIntent"]

# Varied lengths and shapes, so the catch-all takes whole sentences rather than a word or two.
CATCH_ALL_VALUES = [
    "hello", "tell me a joke", "what's on my calendar tomorrow", "list my projects",
    "good morning", "what deadlines are coming up", "continue the previous task",
    "the login page should use google sign in and remember the user for thirty days",
    "also add tests for it", "make the button blue and move it to the right",
    "i think we should store it in the database instead of a file",
    "what happens if the payment fails halfway through",
    "keep it simple for now we can add the admin screen later",
    "add fix the footer links to the backlog of my website",
    "send the weekly report to rahul",
    "how much have i spent on ai this month",
    "the api should return an error when the email is missing",
    "use the same colours as the dashboard",
    "yes but only for logged in users",
    "no the other one",
    "write a short summary of what changed today and email it to me",
    "which tasks failed today",
    "can you explain why the build broke",
    "research the best way to cache images on the phone and compare the options",
    "remind me to call the client at five",
    "it needs to work offline and sync when the connection comes back",
]

DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
          "8": "eight", "9": "nine"}


def spoken_forms(name: str) -> list[str]:
    """"KodeAura7" -> ["kode aura 7", "kode aura seven", "code aura seven", ...] - what Alexa may hear."""
    words = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", name)
    words = re.sub(r"[_\-./]+", " ", words).lower().strip()
    spelled = " ".join(DIGITS.get(w, w) if w.isdigit() and len(w) == 1 else w for w in words.split())
    forms = {words, spelled, spelled.replace("k", "c", 1) if spelled.startswith("k") else spelled}
    return sorted(f for f in forms if f and f != name.lower())


def project_values() -> list[dict[str, Any]]:
    from octopus import plan
    out = [{"id": str(p["id"]), "name": {"value": p["name"], "synonyms": spoken_forms(p["name"])}} for p in plan.real_projects()]
    general = plan.general_project()
    out.append({"id": str(general["id"]), "name": {"value": "general", "synonyms": ["the general workspace", "general workspace"]}})
    return out


def build(invocation: str = INVOCATION, projects: Any = None) -> dict[str, Any]:
    intents = []
    for name, samples in SAMPLES.items():
        intent: dict[str, Any] = {"name": name, "samples": samples}
        if name in SLOTS:
            intent["slots"] = [{"name": s, "type": t} for s, t in SLOTS[name].items()]
        intents.append(intent)
    intents += [{"name": n, "samples": []} for n in BUILT_IN]
    return {"interactionModel": {"languageModel": {
        "invocationName": invocation.lower(),
        "intents": intents,
        "types": [
            {"name": "PROJECT", "values": projects if projects is not None else project_values()},
            {"name": "CATCH_ALL", "values": [{"name": {"value": v}} for v in CATCH_ALL_VALUES]},
        ],
    }}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--invocation", default=INVOCATION, help=f'what you say after "Alexa, open" (default "{INVOCATION}")')
    args = ap.parse_args()
    print(json.dumps(build(args.invocation), indent=2))
