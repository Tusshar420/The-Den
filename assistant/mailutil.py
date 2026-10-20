"""
Provider-independent email helpers: addresses, reply / forward composition
(so a reply keeps its thread and quotes the original), MIME building and
HTML-to-text. Pure functions - no network - so they are easy to test.
"""

import base64
import re
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any, Optional, Union

_ADDR_RE = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
MAX_BODY_CHARS = 20000


def valid_email(address: str) -> bool:
    return bool(_ADDR_RE.match(address or ""))


def parse_address(value: str) -> tuple[str, str]:
    """('Rahul Sharma', 'rahul@example.com') from 'Rahul Sharma <Rahul@Example.com>'."""
    name, addr = parseaddr(value or "")
    return name.strip(), addr.strip().lower()


def split_addresses(header: Optional[str]) -> list[tuple[str, str]]:
    return [(n.strip(), a.strip().lower()) for n, a in getaddresses([header or ""]) if a]


def recipients(value: Union[str, list, None], field: str = "recipient") -> list[str]:
    """Validated, de-duplicated addresses from 'a@x.com, b@y.com' or a list."""
    if value is None or value == "" or value == []:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        for _, addr in split_addresses(str(item)):
            if not valid_email(addr):
                raise ValueError(f"'{addr}' is not a valid email address")
            if addr not in out:
                out.append(addr)
        if not split_addresses(str(item)):
            raise ValueError(f"'{item}' is not a valid email address - give a full address like name@example.com")
    return out


def _no_newlines(label: str, value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{label} can't contain line breaks")
    return value


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if re.match(r"^(re|aw|sv):\s", subject, re.I) else f"Re: {subject}".strip()


def forward_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if re.match(r"^(fwd?|fw):\s", subject, re.I) else f"Fwd: {subject}".strip()


def quote_original(original: dict[str, Any]) -> str:
    who = original.get("from_name") or original.get("from_email") or "the sender"
    if original.get("from_name") and original.get("from_email"):
        who = f"{original['from_name']} <{original['from_email']}>"
    when = original.get("date_text") or original.get("date") or ""
    head = f"On {when}, {who} wrote:" if when else f"{who} wrote:"
    quoted = "\n".join("> " + line if line else ">" for line in (original.get("body") or "").splitlines())
    return f"{head}\n{quoted}"


def reply_recipients(original: dict[str, Any], me: str, reply_all: bool) -> tuple[list[str], list[str]]:
    me = (me or "").lower()
    sender = original.get("reply_to") or original.get("from_email") or ""
    to = [sender] if sender else []
    cc: list[str] = []
    if reply_all:
        for addr in original.get("to", []) + original.get("cc", []):
            if addr != me and addr not in to and addr not in cc:
                cc.append(addr)
    return [a for a in to if a != me] or to, cc


def compose_reply(original: dict[str, Any], body: str, me: str, reply_all: bool = False) -> dict[str, Any]:
    to, cc = reply_recipients(original, me, reply_all)
    if not to:
        raise ValueError("The original message has no sender address to reply to")
    return {"to": to, "cc": cc, "subject": reply_subject(original.get("subject", "")),
            "body": f"{body.rstrip()}\n\n{quote_original(original)}"}


def compose_forward(original: dict[str, Any], note: str) -> dict[str, Any]:
    who = original.get("from_name") or ""
    who = f"{who} <{original.get('from_email', '')}>" if who else original.get("from_email", "")
    head = "\n".join([
        "---------- Forwarded message ---------",
        f"From: {who}",
        f"Date: {original.get('date_text') or original.get('date') or ''}",
        f"Subject: {original.get('subject', '')}",
        f"To: {', '.join(original.get('to', []))}",
    ])
    tail = "\n\n(Attachments on the original message are not included.)" if original.get("attachments") else ""
    return {"subject": forward_subject(original.get("subject", "")),
            "body": (f"{note.rstrip()}\n\n" if note.strip() else "") + f"{head}\n\n{original.get('body', '')}{tail}"}


def build_mime(to: list[str], subject: str, body: str, cc: Optional[list[str]] = None,
               in_reply_to: Optional[str] = None, references: Optional[str] = None,
               html: Optional[str] = None) -> str:
    """base64url of an RFC 5322 message, the form Gmail's `raw` field takes.
    From and Date are left for the provider to fill in."""
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = _no_newlines("The subject", subject)
    if in_reply_to:
        msg["In-Reply-To"] = _no_newlines("In-Reply-To", in_reply_to)
        msg["References"] = _no_newlines("References", " ".join(filter(None, [references, in_reply_to])))
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class _Text(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(p.parts))).strip()
