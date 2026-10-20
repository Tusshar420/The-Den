"""
A very small event dispatcher for modules. The Den has no general event bus,
only plain hook lists in db.py (task_added_hooks, and task_completed_hooks added
alongside it). This bridges those lists to module listeners, and nothing more:

    TASK_ADDED      a task/message was queued          payload: the task dict
    TASK_COMPLETED  a task finished successfully       payload: {"task_id", "result_summary"}

Listeners are called synchronously on the thread that raised the event, so they
must be quick. A listener that raises never affects the event's source or the
other listeners; the exception goes to `on_error` (the owning module's error log).
"""

import threading
from typing import Any, Callable, Optional

import db

TASK_ADDED = "TASK_ADDED"
TASK_COMPLETED = "TASK_COMPLETED"
EVENTS = (TASK_ADDED, TASK_COMPLETED)

_lock = threading.RLock()
_listeners: list[tuple[str, str, Callable[[Any], None], Optional[Callable]]] = []   # event, owner, fn, on_error
_bridged = False


def subscribe(event: str, owner: str, fn: Callable[[Any], None], on_error: Optional[Callable] = None) -> Callable[[], None]:
    """-> a function that removes exactly this listener."""
    if event not in EVENTS:
        raise ValueError(f"unknown event '{event}' (known: {', '.join(EVENTS)})")
    entry = (event, owner, fn, on_error)
    with _lock:
        _listeners.append(entry)
    _bridge()

    def remove() -> None:
        with _lock:
            if entry in _listeners:
                _listeners.remove(entry)
    return remove


def unsubscribe_owner(owner: str) -> int:
    with _lock:
        mine = [e for e in _listeners if e[1] == owner]
        for e in mine:
            _listeners.remove(e)
    return len(mine)


def count(owner: Optional[str] = None) -> int:
    with _lock:
        return sum(1 for e in _listeners if owner is None or e[1] == owner)


def emit(event: str, payload: Any) -> None:
    with _lock:
        todo = [e for e in _listeners if e[0] == event]    # snapshot: listeners may be removed meanwhile
    for _, owner, fn, on_error in todo:
        try:
            fn(payload)
        except Exception as e:
            if on_error:
                try:
                    on_error(e)
                except Exception:
                    pass


def _bridge() -> None:
    """Hook into db once per process, and only once a module has asked for an event."""
    global _bridged
    with _lock:
        if _bridged:
            return
        _bridged = True
    if _on_added not in db.task_added_hooks:
        db.task_added_hooks.append(_on_added)
    if _on_completed not in db.task_completed_hooks:
        db.task_completed_hooks.append(_on_completed)


def _on_added(task: dict) -> None:
    emit(TASK_ADDED, task)


def _on_completed(payload: dict) -> None:
    emit(TASK_COMPLETED, payload)
