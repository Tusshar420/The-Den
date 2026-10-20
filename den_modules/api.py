"""
Module status and control API, for the dashboard. Same API-key auth as every other router.

    GET    /modules                      every module: name, version, status, enabled, tool/command counts, last error
    GET    /modules/{name}               one module, with config and its recent errors (stage, time, stack)
    POST   /modules/{name}/enable | disable | reload
    PATCH  /modules/{name}/config        {"config": {...}} - validated, then applied by reloading that module
    POST   /modules/rescan               re-read modules/, reload the ones whose files changed
    GET    /modules/commands             commands modules have registered
    POST   /modules/commands/{name}      {"arg": "..."} run one
"""

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from den_modules import manifest as mf
from den_modules.manager import manager


class ConfigIn(BaseModel):
    config: dict[str, Any]


class CommandIn(BaseModel):
    arg: str = ""


def build_router(auth: Callable) -> APIRouter:
    router = APIRouter(prefix="/modules")
    dep = [Depends(auth)]

    def known(name: str) -> None:
        if name not in manager.records:
            raise HTTPException(status_code=404, detail=f"No module called '{name}'")

    @router.get("", dependencies=dep)
    def list_modules():
        return {"modules": manager.status(), "commands": manager.commands()}

    # Fixed paths first, so "rescan" and "commands" are never read as module names.
    @router.post("/rescan", dependencies=dep)
    def rescan():
        reloaded = manager.rescan()
        return {"reloaded": reloaded, "modules": manager.status()}

    @router.get("/commands", dependencies=dep)
    def commands():
        return {"commands": manager.commands()}

    @router.post("/commands/{name}", dependencies=dep)
    def run_command(name: str, body: CommandIn = CommandIn()):
        try:
            return {"command": name, "result": manager.run_command(name, body.arg)}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No command called '/{name}'")
        except Exception as e:     # the command's own failure is already logged against its module
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{name}", dependencies=dep)
    def one(name: str):
        known(name)
        return manager.status(name, detail=True)

    @router.post("/{name}/enable", dependencies=dep)
    def enable(name: str):
        known(name)
        manager.enable(name)
        return manager.status(name)

    @router.post("/{name}/disable", dependencies=dep)
    def disable(name: str):
        known(name)
        manager.disable(name)
        return manager.status(name)

    @router.post("/{name}/reload", dependencies=dep)
    def reload(name: str):
        known(name)
        manager.reload(name)
        return manager.status(name)

    @router.patch("/{name}/config", dependencies=dep)
    def config(name: str, body: ConfigIn):
        known(name)
        try:
            manager.set_config(name, body.config)
        except mf.ManifestError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return manager.status(name, detail=True)

    return router
