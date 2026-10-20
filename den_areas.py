"""
Workspaces (Den 2.1): working areas inside this one Den - Work and Personal to start, plus any you add
(a client like Cmentor). Each project belongs to one (its `category` is the area's id); the dashboard's
switcher shows one area at a time, and the General project belongs to all of them. Octopus, the queue,
accounts and settings are shared.

The list lives in settings["areas"]; deleting an area moves its projects to another one first.
"""

import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PALETTE = ("#7FB2FF", "#FF7B5C", "#45E0C8", "#A58BFF", "#FFB547", "#6FCF97", "#E6C07B", "#7FD1B9")
MAX_AREAS = 20


def areas() -> list[dict[str, Any]]:
    return [dict(a) for a in (db.get_setting("areas") or []) if a.get("id")]


def _save(items: list[dict[str, Any]]) -> None:
    db.set_setting("areas", items)


def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in db.list_projects():
        if not db.is_general_project(p):
            out[p.get("category") or "personal"] = out.get(p.get("category") or "personal", 0) + 1
    return out


def view() -> list[dict[str, Any]]:
    n = _counts()
    return [{**a, "projects": n.get(a["id"], 0)} for a in areas()]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "area"


def _name(name: str, skip: Optional[str] = None) -> str:
    name = " ".join((name or "").split())[:40]
    if not name:
        raise HTTPException(status_code=400, detail="Give the workspace a name")
    if any(a["name"].lower() == name.lower() and a["id"] != skip for a in areas()):
        raise HTTPException(status_code=409, detail=f"There's already a workspace called {name}")
    return name


def _color(c: Optional[str], fallback: str) -> str:
    if c is None:
        return fallback
    if not COLOR_RE.match(c):
        raise HTTPException(status_code=400, detail="Colour must look like #7fb2ff")
    return c.lower()


class AreaIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    color: Optional[str] = None


class AreaPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=40)
    color: Optional[str] = None


def add(body: AreaIn) -> dict[str, Any]:
    items = areas()
    if len(items) >= MAX_AREAS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_AREAS} workspaces")
    name = _name(body.name)
    base = _slug(name); aid, i = base, 2
    while any(a["id"] == aid for a in items):
        aid, i = f"{base}-{i}", i + 1
    new = {"id": aid, "name": name, "color": _color(body.color, PALETTE[len(items) % len(PALETTE)])}
    _save(items + [new])
    return {**new, "projects": 0}


def update(area_id: str, body: AreaPatch) -> dict[str, Any]:
    items = areas()
    a = next((x for x in items if x["id"] == area_id), None)
    if not a:
        raise HTTPException(status_code=404, detail="No such workspace")
    if body.name is not None:
        a["name"] = _name(body.name, area_id)
    if body.color is not None:
        a["color"] = _color(body.color, a.get("color") or PALETTE[0])
    _save(items)
    return next(v for v in view() if v["id"] == area_id)


def remove(area_id: str, move_to: Optional[str]) -> dict[str, Any]:
    items = areas()
    if not any(x["id"] == area_id for x in items):
        raise HTTPException(status_code=404, detail="No such workspace")
    rest = [x for x in items if x["id"] != area_id]
    if not rest:
        raise HTTPException(status_code=400, detail="The Den needs at least one workspace")
    target = move_to or rest[0]["id"]
    if target == area_id or not any(x["id"] == target for x in rest):
        raise HTTPException(status_code=400, detail="Pick another workspace for its projects")
    moved = 0
    for p in db.list_projects(include_deleted=True):
        if p.get("category") == area_id and not db.is_general_project(p):
            db.set_project_category(p["id"], target)
            moved += 1
    _save(rest)
    return {"ok": True, "moved": moved, "to": target}


def build_router(require_api_key: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_api_key)])

    @r.get("/den/areas")
    def list_areas():
        return view()

    @r.post("/den/areas")
    def add_area(body: AreaIn):
        return add(body)

    @r.patch("/den/areas/{area_id}")
    def edit_area(area_id: str, body: AreaPatch):
        return update(area_id, body)

    @r.delete("/den/areas/{area_id}")
    def delete_area(area_id: str, move_to: Optional[str] = None):
        return remove(area_id, move_to)

    return r
