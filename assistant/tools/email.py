"""
Email tool: search, read, find a contact's address, draft, send, reply,
forward. Works with any EmailProvider; nothing here is Gmail-specific.

Reading and drafting run on their own; anything that leaves the mailbox
(send / reply / forward / send_draft) needs EMAIL_SEND, which asks first by
default. Deleting mail is deliberately not an action at all.
"""

from typing import Any

from assistant import mailutil, timeutil
from assistant.tools import ActionSpec, Prepared, Tool, ToolError, preview, register

BODY_FOR_USER = 3000
BODY_FOR_MODEL = 6000


def _short(m: dict[str, Any]) -> dict[str, Any]:
    who = m.get("from_name") or m.get("from_email") or "?"
    return {"id": m["id"], "thread_id": m.get("thread_id"), "from": who, "from_email": m.get("from_email"),
            "subject": m.get("subject"), "date": m.get("date"), "snippet": (m.get("snippet") or "")[:160],
            "unread": m.get("unread", False)}


def _when(iso: Any) -> str:
    return timeutil.fmt_when(timeutil.parse_iso(iso)) if iso else ""


def _to_list(value: Any, what: str) -> list[str]:
    try:
        return mailutil.recipients(value, what)
    except ValueError as e:
        raise ToolError(str(e))


def _need_text(args: dict[str, Any], key: str) -> str:
    v = str(args.get(key) or "").strip()
    if not v:
        raise ToolError(f"The email needs a {key}")
    if key == "subject" and ("\n" in v or "\r" in v):
        # Caught here, before anyone is asked to approve it - a line break in a
        # header is how extra recipients get smuggled into a message.
        raise ToolError("The subject can't contain line breaks")
    return v


def _email_preview(title: str, action: str, to: list[str], cc: list[str], subject: str, message: str,
                   approve: str, extra: list = (), warnings: list = ()) -> dict[str, Any]:
    fields = [("To", ", ".join(to)), ("Cc", ", ".join(cc)), ("Subject", subject), ("Message", message), *extra]
    return preview("email", title, action, fields, approve, list(warnings), blocks=("Message",))


class EmailTool(Tool):
    id = "email"
    label = "Email"
    actions = {
        "search": ActionSpec(
            "search", "Search the mailbox, newest first. Uses Gmail search syntax (from:rahul subject:proposal newer_than:7d).",
            {"query": "search query", "max_results?": "1-10, default 5"}, reads_untrusted=True),
        "read": ActionSpec("read", "Open one message in full.", {"message_id": "id from search"}, reads_untrusted=True),
        "find_contact": ActionSpec(
            "find_contact", "Find someone's email address from recent mail. Use this when a person is named without an "
            "address - never guess one.", {"name": "the person's name"}, reads_untrusted=True),
        "create_draft": ActionSpec(
            "create_draft", "Save a draft. Nothing is sent.",
            {"to": "recipient address(es), comma separated", "subject": "subject line", "body": "plain-text body",
             "cc?": "cc address(es)", "reply_to_message_id?": "keep the draft in that message's thread"}),
        "send": ActionSpec(
            "send", "Send a new email. Needs the owner's approval unless they trust it.",
            {"to": "recipient address(es)", "subject": "subject line", "body": "plain-text body", "cc?": "cc address(es)"}),
        "reply": ActionSpec(
            "reply", "Reply in the original thread, quoting the original message. Needs approval.",
            {"message_id": "id of the message being answered", "body": "your reply text",
             "reply_all?": "true to include everyone on the original"}),
        "forward": ActionSpec(
            "forward", "Forward a message. Needs approval.",
            {"message_id": "id of the message", "to": "recipient address(es)", "note?": "text to put above it"}),
        "send_draft": ActionSpec("send_draft", "Send a draft that was saved earlier. Needs approval.",
                                 {"draft_id": "id of the draft"}),
    }

    # ---------------------------------------------------------------- reads

    def prepare_search(self, args, ctx):
        try:
            n = max(1, min(int(args.get("max_results", 5)), 10))
        except (TypeError, ValueError):
            n = 5
        q = args["query"]
        return Prepared({"query": q, "max_results": n}, ["EMAIL_READ"], f"search: {q[:80]}", f"Searching your mail for {q}")

    def run_search(self, args, ctx):
        msgs = ctx.email().search(args["query"], args["max_results"])
        if not msgs:
            return {"text": f'No emails matched "{args["query"]}".', "data": {"messages": []}}
        lines = [f'Found {len(msgs)} email{"s" if len(msgs) != 1 else ""} for "{args["query"]}":']
        for i, m in enumerate(msgs, 1):
            who = f"{m['from_name']} <{m['from_email']}>" if m.get("from_name") else m.get("from_email", "?")
            lines.append(f"{i}. {who} · {_when(m.get('date'))} · {m.get('subject')}")
            if m.get("snippet"):
                lines.append(f"   “{m['snippet'][:140]}”")
        return {"text": "\n".join(lines), "data": {"messages": [_short(m) for m in msgs]}}

    def prepare_read(self, args, ctx):
        return Prepared({"message_id": args["message_id"]}, ["EMAIL_READ"], "open message", "Opening an email")

    def run_read(self, args, ctx):
        m = ctx.email().get(args["message_id"])
        body = m.get("body", "")
        head = [f"From: {m['from_name']} <{m['from_email']}>" if m.get("from_name") else f"From: {m.get('from_email')}",
                f"To: {', '.join(m.get('to', []))}", f"Date: {m.get('date_text')}", f"Subject: {m.get('subject')}"]
        att = f"\n\nAttachments: {', '.join(m['attachments'])}" if m.get("attachments") else ""
        shown = body[:BODY_FOR_USER] + ("\n… (message continues)" if len(body) > BODY_FOR_USER else "")
        data = {**_short(m), "to": m.get("to"), "cc": m.get("cc"), "body": body[:BODY_FOR_MODEL],
                "attachments": m.get("attachments", [])}
        return {"text": "\n".join(head) + "\n\n" + shown + att, "data": data}

    def prepare_find_contact(self, args, ctx):
        return Prepared({"name": args["name"]}, ["EMAIL_READ"], f"contact: {args['name'][:60]}",
                        f"Looking for {args['name']}'s address in your mail")

    def run_find_contact(self, args, ctx):
        provider = ctx.email()
        name = args["name"].strip().strip('"')
        needle, me = name.lower(), provider.account().lower()
        found: dict[str, dict[str, Any]] = {}
        for m in provider.search(f'from:"{name}" OR to:"{name}"', 20):
            pairs = [(m.get("from_name", ""), m.get("from_email", ""))] + [("", a) for a in m.get("to", []) + m.get("cc", [])]
            for display, addr in pairs:
                if not addr or addr == me:
                    continue
                if needle in display.lower() or needle in addr.split("@")[0].lower():
                    c = found.setdefault(addr, {"name": display, "email": addr, "count": 0})
                    c["count"] += 1
                    c["name"] = c["name"] or display
        ranked = sorted(found.values(), key=lambda c: -c["count"])[:5]
        if not ranked:
            return {"text": f"I couldn't find anyone called {name} in your recent mail.", "data": {"contacts": []}}
        lines = [f"Addresses that match “{name}”:"] + [
            f"• {c['name'] + ' ' if c['name'] else ''}<{c['email']}> ({c['count']} recent message{'s' if c['count'] != 1 else ''})"
            for c in ranked]
        return {"text": "\n".join(lines), "data": {"contacts": ranked}}

    # --------------------------------------------------------------- drafts

    def prepare_create_draft(self, args, ctx):
        to, cc = _to_list(args["to"], "recipient"), _to_list(args.get("cc"), "cc")
        if not to:
            raise ToolError("The draft needs at least one recipient address")
        subject, body = _need_text(args, "subject"), _need_text(args, "body")
        resolved = {"to": to, "cc": cc, "subject": subject, "body": body,
                    "reply_to_message_id": args.get("reply_to_message_id")}
        return Prepared(resolved, ["EMAIL_DRAFT"], f"draft to {', '.join(to)}: {subject[:60]}",
                        f"Saving a draft to {', '.join(to)}",
                        _email_preview("EMAIL DRAFT", "Save a draft (nothing is sent)", to, cc, subject, body, "Save draft"))

    def run_create_draft(self, args, ctx):
        d = ctx.email().create_draft(args["to"], args["subject"], args["body"], args.get("cc") or None,
                                     args.get("reply_to_message_id"))
        text = (f"Draft saved to {', '.join(args['to'])} — “{args['subject']}”.\n\n{args['body']}\n\n"
                "Nothing has been sent. Tell me to send it when you're happy with it.")
        return {"text": text, "data": {**d, "to": args["to"], "subject": args["subject"]}, "ref": d.get("draft_id"),
                "url": d.get("url")}

    # ----------------------------------------------------------------- sends

    def prepare_send(self, args, ctx):
        to, cc = _to_list(args["to"], "recipient"), _to_list(args.get("cc"), "cc")
        if not to:
            raise ToolError("The email needs at least one recipient address")
        subject, body = _need_text(args, "subject"), _need_text(args, "body")
        return Prepared({"to": to, "cc": cc, "subject": subject, "body": body, "reply_to_message_id": None},
                        ["EMAIL_SEND"], f"{', '.join(to)}: {subject[:60]}", f"Send an email to {', '.join(to)}: {subject}",
                        _email_preview("EMAIL APPROVAL", "Send email", to, cc, subject, body, "Send"))

    def prepare_reply(self, args, ctx):
        provider = ctx.email()
        original = provider.get(args["message_id"])
        try:
            composed = mailutil.compose_reply(original, _need_text(args, "body"), provider.account(),
                                              str(args.get("reply_all", "")).lower() in ("true", "1", "yes"))
        except ValueError as e:
            raise ToolError(str(e))
        who = original.get("from_name") or original.get("from_email")
        extra = [("In reply to", f"{who} — {original.get('subject')} ({_when(original.get('date'))})"),
                 ("Includes", "The original message, quoted below your reply")]
        return Prepared({**composed, "reply_to_message_id": args["message_id"]}, ["EMAIL_SEND"],
                        f"reply to {composed['to'][0]}: {composed['subject'][:60]}",
                        f"Reply to {composed['to'][0]}: {composed['subject']}",
                        _email_preview("EMAIL APPROVAL", "Send reply", composed["to"], composed["cc"], composed["subject"],
                                       args["body"], "Send reply", extra))

    def prepare_forward(self, args, ctx):
        original = ctx.email().get(args["message_id"])
        to = _to_list(args["to"], "recipient")
        if not to:
            raise ToolError("Forwarding needs a recipient address")
        composed = mailutil.compose_forward(original, args.get("note", ""))
        warn = ["Attachments on the original message are not forwarded."] if original.get("attachments") else []
        return Prepared({"to": to, "cc": [], "subject": composed["subject"], "body": composed["body"],
                         "reply_to_message_id": None}, ["EMAIL_SEND"], f"forward to {', '.join(to)}: {composed['subject'][:60]}",
                        f"Forward “{original.get('subject')}” to {', '.join(to)}",
                        _email_preview("EMAIL APPROVAL", "Forward email", to, [], composed["subject"],
                                       args.get("note") or "(no note)", "Forward",
                                       [("Forwarding", f"{original.get('subject')} — from {original.get('from_email')}")], warn))

    def prepare_send_draft(self, args, ctx):
        d = ctx.email().get_draft(args["draft_id"])
        return Prepared({"draft_id": args["draft_id"], "to": d["to"]}, ["EMAIL_SEND"],
                        f"draft to {', '.join(d['to'])}: {d['subject'][:60]}", f"Send the draft to {', '.join(d['to'])}: {d['subject']}",
                        _email_preview("EMAIL APPROVAL", "Send saved draft", d["to"], d["cc"], d["subject"], d["body"], "Send"))

    def _sent(self, args, sent):
        to = ", ".join(args["to"])
        return {"text": f"Sent to {to}: “{args['subject']}”.", "data": {**sent, "to": args["to"]}}

    def run_send(self, args, ctx):
        return self._sent(args, ctx.email().send(args["to"], args["subject"], args["body"], args.get("cc") or None, None))

    def run_reply(self, args, ctx):
        return self._sent(args, ctx.email().send(args["to"], args["subject"], args["body"], args.get("cc") or None,
                                                 args.get("reply_to_message_id")))

    def run_forward(self, args, ctx):
        return self._sent(args, ctx.email().send(args["to"], args["subject"], args["body"], None, None))

    def run_send_draft(self, args, ctx):
        sent = ctx.email().send_draft(args["draft_id"])
        return {"text": f"Sent the draft to {', '.join(args.get('to') or ['the recipient'])}.", "data": sent}

    # ---------------------------------------------------------------- memory

    def remember(self, action, result, memory):
        data = result.get("data") or {}
        if action in ("search",):
            found = [{k: v for k, v in m.items() if k != "snippet"} for m in data.get("messages", [])]
            keep = [m for m in memory.get("emails", []) if m["id"] not in {f["id"] for f in found}]
            memory["emails"] = (found + keep)[:10]
        elif action == "read" and data.get("id"):
            keep = [m for m in memory.get("emails", []) if m["id"] != data["id"]]
            brief = {k: data.get(k) for k in ("id", "thread_id", "from", "from_email", "subject", "date", "unread")}
            memory["emails"] = ([brief] + keep)[:10]
            memory["open_email"] = data["id"]
        elif action == "find_contact":
            memory["contacts"] = data.get("contacts", [])
        elif action == "create_draft" and data.get("draft_id"):
            memory["drafts"] = ([{"draft_id": data["draft_id"], "to": data.get("to"), "subject": data.get("subject")}]
                                + memory.get("drafts", []))[:5]


register(EmailTool())
