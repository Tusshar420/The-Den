"""
Fixed email templates: every email the Den sends is filled in from here, so writing one
never costs a model call. One shared HTML layout plus one small entry per event.

An event is (kind, subject, headline, lines): `subject`, `headline` and each line are
str.format templates over the fields the caller passes in. A line starting with "- " is a
bullet. Values are HTML-escaped in the HTML part and used as-is in the plain-text part.

To add an email: add an entry to EVENTS, then call mailer.send("<key>", **fields).
"""

import html
from string import Formatter

# kind -> accent colour of the header bar
KINDS = {
    "info": "#2f6fed",
    "good": "#1f9d57",
    "warn": "#d98a00",
    "bad": "#d64545",
}

EVENTS: dict[str, dict] = {
    "restart_needed": {
        "kind": "warn",
        "subject": "The Den needs a restart",
        "headline": "The Den needs a restart",
        "lines": [
            "The code on disk changed after the service started, so it is still running the old version.",
            "Changed: {changed}",
            "Restart it from the dashboard (Settings > Restart) when nothing important is running.",
        ],
    },
    "restart_requested": {
        "kind": "info",
        "subject": "The Den restart is scheduled ({mode})",
        "headline": "Restart {mode} requested",
        "lines": [
            "{detail}",
            "Waiting tasks are held and let go again once it is back.",
        ],
    },
    "restart_completed": {
        "kind": "good",
        "subject": "The Den restarted",
        "headline": "The Den is back up",
        "lines": [
            "The restart finished{took}.",
            "Running since {started_at}.",
            "{released}",
        ],
    },
    "unexpected_restart": {
        "kind": "bad",
        "subject": "The Den restarted on its own",
        "headline": "The Den restarted unexpectedly",
        "lines": [
            "The service started without a restart being requested - it crashed, was killed, or the Mac rebooted.",
            "Started: {started_at}.",
            "Tasks that were running were marked interrupted; Retry continues them.",
        ],
    },
    "project_created": {
        "kind": "good",
        "subject": "New project: {name}",
        "headline": "Project created",
        "lines": ["- Name: {name}", "- Category: {category}", "- Folder: {folder}"],
    },
    "project_deleted": {
        "kind": "bad",
        "subject": "Project deleted: {name}",
        "headline": "Project deleted",
        "lines": ["{name} and its to-dos, phases, notes, documents and calendar were deleted.",
                  "Its folder on disk was not touched."],
    },
    "project_archived": {
        "kind": "warn",
        "subject": "Project archived: {name}",
        "headline": "Project archived",
        "lines": ["{name} was moved to Archived.", "It keeps all its data and can be moved back any time."],
    },
    "project_completed": {
        "kind": "good",
        "subject": "Project completed: {name}",
        "headline": "Project completed",
        "lines": ["{name} was marked Completed."],
    },
    "test": {
        "kind": "info",
        "subject": "The Den test email",
        "headline": "Email is working",
        "lines": ["This is a test email from The Den."],
    },
}

_FIELDS = Formatter()


class _Blank(dict):
    def __missing__(self, key):
        return ""


def _fill(template: str, fields: dict, escape: bool) -> str:
    values = _Blank({k: (html.escape(str(v)) if escape else str(v)) for k, v in fields.items()})
    return template.format_map(values)


def render(event: str, fields: dict) -> tuple[str, str, str]:
    """(subject, plain text, html) for an event. Unknown field names render as blank."""
    spec = EVENTS[event]
    subject = _fill(spec["subject"], fields, escape=False).replace("\n", " ")
    headline = _fill(spec["headline"], fields, escape=False)
    plain, rows = [headline, ""], []
    for template in spec["lines"]:
        text = _fill(template, fields, escape=False).strip()
        if not text:
            continue
        plain.append(text)
        safe = _fill(template, fields, escape=True).strip()
        if safe.startswith("- "):
            rows.append(f'<div style="margin:2px 0 2px 12px">&bull; {safe[2:]}</div>')
        else:
            rows.append(f'<p style="margin:0 0 12px">{safe}</p>')
    plain.append("\n-- The Den")
    page = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:520px;'
        'margin:0 auto;border:1px solid #e3e3e3;border-radius:10px;overflow:hidden;color:#222">'
        f'<div style="background:{KINDS[spec["kind"]]};color:#fff;padding:14px 18px;font-size:17px;'
        f'font-weight:600">{html.escape(headline)}</div>'
        f'<div style="padding:18px;font-size:14px;line-height:1.5">{"".join(rows)}</div>'
        '<div style="padding:10px 18px;background:#f6f6f6;color:#888;font-size:12px">The Den</div></div>'
    )
    return subject, "\n".join(plain), page
