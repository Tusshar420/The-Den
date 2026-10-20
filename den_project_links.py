"""
A project's links with a "pinned" flag (Den 2.1 Projects: the pill row under the project header and
Overview › Links with pin / edit / remove).

The links themselves stay where they always were - the project record's `links` column, a JSON list of
{label, url} checked by crm.clean - so the CRM, reports and the old PATCH /projects/{id} keep working
unchanged. The label doubles as the link's kind (GitHub, Live site, Staging, Docs, Design; anything else
shows as "Other"). Only the pin is new, kept beside the record in den_project_link_pins keyed by URL:
crm.clean only keeps label + url, and a sidecar table means nothing that reads the record has to change.

A link nobody has pinned or unpinned yet follows its kind: code, live site, staging and design links
show in the header, docs and others don't - so existing projects get a useful row without any setup.
"""

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import crm
import db

KINDS = ("GitHub", "Live site", "Staging", "Docs", "Design", "Other")
PINNED_BY_DEFAULT = {"github", "repo", "live site", "staging", "design"}
_ready = False


def _init() -> None:
    global _ready
    if _ready:
        return
    with db._lock:
        conn = db.get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS den_project_link_pins (project_id INTEGER NOT NULL, url TEXT NOT NULL, "
                     "pinned INTEGER NOT NULL, PRIMARY KEY (project_id, url))")
        conn.commit()
    _ready = True


def _project(project_id: int) -> dict[str, Any]:
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


def default_pinned(label: Optional[str]) -> bool:
    return (label or "").strip().lower() in PINNED_BY_DEFAULT


def _pins(project_id: int) -> dict[str, bool]:
    _init()
    with db._lock:
        rows = db.get_conn().execute("SELECT url, pinned FROM den_project_link_pins WHERE project_id = ?", (project_id,)).fetchall()
    return {r["url"]: bool(r["pinned"]) for r in rows}


def links(project_id: int) -> dict[str, Any]:
    p = _project(project_id)
    pins = _pins(project_id)
    out = []
    for link in p.get("links") or []:
        label, url = link.get("label") or "", link.get("url") or ""
        kind = next((k for k in KINDS if k.lower() == label.strip().lower()), "GitHub" if label.strip().lower() == "repo" else "Other")
        out.append({"label": label, "url": url, "kind": kind, "pinned": pins.get(url, default_pinned(label))})
    return {"links": out, "kinds": list(KINDS)}


class LinkIn(BaseModel):
    label: Optional[str] = Field(None, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    pinned: Optional[bool] = None         # null = follow the kind's default


class LinksIn(BaseModel):
    links: list[LinkIn] = Field(max_length=crm.MAX_LINKS)


def save(project_id: int, body: LinksIn) -> dict[str, Any]:
    """Replace the project's whole link list (the page sends it after every pin, edit, add or remove).
    Checked by the same rules as the project record, so a bad URL changes nothing."""
    current = _project(project_id)
    if db.is_general_project(current):
        raise HTTPException(status_code=400, detail="General has no project record")
    try:
        record = crm.clean({"links": [{"label": x.label, "url": x.url} for x in body.links]}, current)
    except crm.RecordError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cleaned = record["links"]
    _init()
    db.update_project_record(project_id, {"links": cleaned})
    with db._lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM den_project_link_pins WHERE project_id = ?", (project_id,))
        # crm.clean keeps the order and only normalises each URL, so the pins line up with what was sent.
        for sent, link in zip(body.links, cleaned):
            if sent.pinned is not None:
                conn.execute("INSERT OR REPLACE INTO den_project_link_pins (project_id, url, pinned) VALUES (?, ?, ?)",
                             (project_id, link["url"], 1 if sent.pinned else 0))
        conn.commit()
    return links(project_id)


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/projects/{project_id}/links")
    def project_links(project_id: int):
        return links(project_id)

    @r.put("/den/projects/{project_id}/links")
    def project_links_save(project_id: int, body: LinksIn):
        return save(project_id, body)

    return r
