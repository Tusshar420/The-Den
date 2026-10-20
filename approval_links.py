"""
Approve / Deny buttons on approval pushes.

ActivitySmith's `open_url` buttons are opened by the phone, so they reach The
Den over Tailscale (DEN_URL, e.g. http://100.72.208.3:8787) and nothing is
exposed publicly. Safari can't send the X-API-Key header, so each link carries
its own token instead: an HMAC of the approval id and the decision, keyed on
API_KEY. A token only works for that one approval and that one decision, and a
decided approval can't be decided again, so a used link does nothing.

Stdlib only - approval_server.py imports this from its own venv.
"""

import hashlib
import hmac
import os
from typing import Any, Optional

DECISIONS = ("approve", "deny")


def den_url() -> Optional[str]:
    return (os.environ.get("DEN_URL") or "").rstrip("/") or None


def token(approval_id: int, decision: str) -> str:
    key = (os.environ.get("API_KEY") or "").encode()
    return hmac.new(key, f"approval:{approval_id}:{decision}".encode(), hashlib.sha256).hexdigest()[:32]


def valid(approval_id: int, decision: str, t: str) -> bool:
    if not os.environ.get("API_KEY") or decision not in DECISIONS:
        return False
    return hmac.compare_digest(token(approval_id, decision).encode(), (t or "").encode())


def decide_path(approval_id: int, decision: str) -> str:
    """Same link without the host - for the dashboard's own service worker (webpush), which is already on it."""
    return f"/approvals/{approval_id}/quick/{decision}?t={token(approval_id, decision)}"


def decide_url(approval_id: int, decision: str) -> str:
    return f"{den_url()}{decide_path(approval_id, decision)}"


def actions(approval_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Buttons for a push: Approve / Deny when there's an approval to decide
    (and an API_KEY to sign with), plus Open dashboard. None without DEN_URL."""
    base = den_url()
    if not base:
        return []
    out = []
    if approval_id is not None and os.environ.get("API_KEY"):
        out += [{"title": "Approve", "type": "open_url", "url": decide_url(approval_id, "approve")},
                {"title": "Deny", "type": "open_url", "url": decide_url(approval_id, "deny")}]
    out.append({"title": "Open dashboard", "type": "open_url", "url": f"{base}/dashboard"})
    return out
