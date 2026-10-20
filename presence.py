"""
Is Tushar using The Den right now?

The dashboard sends POST /presence every ~20 seconds, but only while its tab is
visible and someone has touched it (tap, key, scroll) in the last few minutes -
an open tab on an idle laptop doesn't count. Anything that wants to reach him
somewhere else (Alexa, alexa/notifier.py) checks here first.

Kept in memory: after a restart he counts as away until the dashboard's next
heartbeat, which is the safe side to err on for approvals.
"""

import time
from typing import Any, Optional

AWAY_AFTER_SECONDS = 90

_last_seen: Optional[float] = None
_last_device: str = ""


def seen(device: str = "") -> None:
    global _last_seen, _last_device
    _last_seen, _last_device = time.time(), (device or "")[:80]


def is_present(now: Optional[float] = None) -> bool:
    return _last_seen is not None and (now or time.time()) - _last_seen < AWAY_AFTER_SECONDS


def status() -> dict[str, Any]:
    return {"present": is_present(), "last_seen_seconds_ago": round(time.time() - _last_seen) if _last_seen else None,
            "device": _last_device or None, "away_after_seconds": AWAY_AFTER_SECONDS}


def reset() -> None:
    global _last_seen, _last_device
    _last_seen, _last_device = None, ""
