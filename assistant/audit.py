"""
The assistant's activity log: one row per external action, attempted,
approved, refused or failed. Timestamp, tool, action, target, approval
state, result and error - and nothing else. Message bodies never come here,
and anything token-shaped is scrubbed before it is written.
"""

from typing import Optional

import db
from assistant import net


def log(tool: str, action: str, target: Optional[str] = None, approval: str = "not_required", result: str = "ok",
        error: Optional[str] = None, task_id: Optional[int] = None, approval_id: Optional[int] = None,
        ref: Optional[str] = None) -> int:
    return db.add_activity(tool, action, target=net.scrub(target) if target else None, approval=approval,
                           result=result, error=net.scrub(error) if error else None, task_id=task_id,
                           approval_id=approval_id, ref=ref)
