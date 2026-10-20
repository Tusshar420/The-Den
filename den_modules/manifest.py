"""
Module manifests (module.json) and version-range checks. Pure data, no side
effects: parsing a manifest never imports or runs module code.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
CONFIG_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


class ManifestError(Exception):
    """module.json is unreadable or invalid; the message says what to fix."""


def parse_version(text: str) -> tuple[int, ...]:
    m = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", str(text))
    if not m:
        raise ValueError(f"'{text}' is not a version like 1.2.3")
    return tuple(int(p or 0) for p in m.groups())


_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
        "<": lambda a, b: a < b, "==": lambda a, b: a == b, "=": lambda a, b: a == b}


def satisfies(version: str, spec: str) -> bool:
    """spec is comma-separated comparisons, e.g. ">=1.0,<2". "" or "*" accepts anything."""
    spec = (spec or "").strip()
    if spec in ("", "*"):
        return True
    have = parse_version(version)
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(>=|<=|==|=|>|<)?\s*(\S+)\s*", part)
        if not m:
            raise ValueError(f"'{part.strip()}' is not a version comparison like >=1.0")
        if not _OPS[m.group(1) or "=="](have, parse_version(m.group(2))):
            return False
    return True


@dataclass
class Manifest:
    name: str
    version: str
    description: str = ""
    enabled: bool = True                      # default; the owner's switch (db setting) overrides it
    entry: str = "module.py"
    requires: dict[str, str] = field(default_factory=dict)   # module name -> version spec
    optional: dict[str, str] = field(default_factory=dict)
    config: dict[str, dict[str, Any]] = field(default_factory=dict)   # key -> {type, required, default, restart}
    secrets: list[str] = field(default_factory=list)         # environment variables the module may read
    capabilities: list[str] = field(default_factory=list)    # capabilities its tools may ask for (assistant/capabilities.py)
    hot_reload: bool = True                   # False: owns something that can't be unloaded - restart to change it
    path: Optional[Path] = None


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ManifestError(msg)


def _deps(raw: Any, label: str) -> dict[str, str]:
    _need(isinstance(raw, dict), f"'{label}' must be an object of module name -> version range")
    for n, spec in raw.items():
        _need(NAME_RE.match(n) is not None, f"'{label}' names an invalid module '{n}'")
        _need(isinstance(spec, str), f"'{label}.{n}' must be a version range string")
        try:
            satisfies("0.0.0", spec)
        except ValueError as e:
            raise ManifestError(f"'{label}.{n}': {e}")
    return dict(raw)


def parse(data: Any, path: Optional[Path] = None) -> Manifest:
    _need(isinstance(data, dict), "module.json must contain a JSON object")
    name = data.get("name")
    _need(isinstance(name, str) and NAME_RE.match(name) is not None,
          "'name' is required: lowercase letters, digits, - or _, starting with a letter")
    _need(isinstance(data.get("version"), str), "'version' is required, e.g. \"1.0.0\"")
    try:
        parse_version(data["version"])
    except ValueError as e:
        raise ManifestError(f"'version': {e}")
    entry = data.get("entry", "module.py")
    _need(isinstance(entry, str) and entry.endswith(".py") and "/" not in entry and "\\" not in entry and not entry.startswith("."),
          "'entry' must be a .py file inside the module's own folder")
    _need(isinstance(data.get("enabled", True), bool), "'enabled' must be true or false")
    _need(isinstance(data.get("hot_reload", True), bool), "'hot_reload' must be true or false")
    cfg = data.get("config", {})
    _need(isinstance(cfg, dict), "'config' must be an object of key -> {type, required, default}")
    for k, spec in cfg.items():
        _need(isinstance(spec, dict) and spec.get("type", "string") in CONFIG_TYPES,
              f"config '{k}' needs an object with type one of {', '.join(CONFIG_TYPES)}")
        if "default" in spec:
            _need(_type_ok(spec["default"], spec.get("type", "string")), f"config '{k}' default is not a {spec.get('type', 'string')}")
    secrets = data.get("secrets", [])
    _need(isinstance(secrets, list) and all(isinstance(s, str) and ENV_RE.match(s) for s in secrets),
          "'secrets' must be a list of environment variable names like MY_API_KEY")
    caps = data.get("capabilities", [])
    _need(isinstance(caps, list) and all(isinstance(c, str) for c in caps), "'capabilities' must be a list of names")
    from assistant import capabilities as known
    unknown = [c for c in caps if c not in known.CAPABILITIES]
    _need(not unknown, f"unknown capabilities: {', '.join(unknown)} (see assistant/capabilities.py)")
    m = Manifest(name=name, version=data["version"], description=str(data.get("description", "")),
                 enabled=data.get("enabled", True), entry=entry, requires=_deps(data.get("requires", {}), "requires"),
                 optional=_deps(data.get("optional", {}), "optional"), config=cfg, secrets=secrets, capabilities=caps,
                 hot_reload=data.get("hot_reload", True), path=path)
    _need(name not in m.requires and name not in m.optional, "a module cannot depend on itself")
    _need(not set(m.requires) & set(m.optional), "a module can't be both required and optional")
    return m


def _type_ok(value: Any, typ: str) -> bool:
    if typ in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, CONFIG_TYPES[typ])


def load(folder: Path) -> Manifest:
    f = folder / "module.json"
    try:
        data = json.loads(f.read_text())
    except OSError as e:
        raise ManifestError(f"can't read {f.name}: {e.strerror or e}")
    except ValueError as e:
        raise ManifestError(f"{f.name} is not valid JSON: {e}")
    m = parse(data, folder)
    if m.name != folder.name:
        raise ManifestError(f"'name' is \"{m.name}\" but the folder is called \"{folder.name}\" - they must match")
    return m


def resolve_config(m: Manifest, values: dict[str, Any]) -> dict[str, Any]:
    """Defaults + owner's values, validated against the manifest. Unknown keys are rejected."""
    out: dict[str, Any] = {}
    unknown = [k for k in values if k not in m.config]
    if unknown:
        raise ManifestError(f"unknown config keys: {', '.join(unknown)}")
    for k, spec in m.config.items():
        typ = spec.get("type", "string")
        if k in values:
            if not _type_ok(values[k], typ):
                raise ManifestError(f"config '{k}' must be a {typ}")
            out[k] = values[k]
        elif "default" in spec:
            out[k] = spec["default"]
        elif spec.get("required"):
            raise ManifestError(f"required config '{k}' is not set")
    return out
