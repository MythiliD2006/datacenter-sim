"""Loading, validating and saving settings, interfaces and profiles."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any

import yaml

from . import paths

PHASES = ["idle", "ramp_up", "spike", "peak", "steady", "cooldown"]
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class ConfigError(ValueError):
    pass


# ── generic yaml helpers ────────────────────────────────────────────────

def _read_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class _BlockDumper(yaml.SafeDumper):
    """Sections in block style, short lists of plain values on one line."""


def _repr_list(dumper, data):
    flow = all(not isinstance(x, (dict, list)) for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_BlockDumper.add_representer(list, _repr_list)


def _write_yaml(path: Path, data: Any, header: str = "", flow: bool | None = None) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if header:
            f.write(header)
        if flow is False:
            yaml.dump(data, f, Dumper=_BlockDumper, sort_keys=False, default_flow_style=False, width=140, allow_unicode=True)
        else:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=flow, width=140, allow_unicode=True)
    tmp.replace(path)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ── settings ────────────────────────────────────────────────────────────

def default_config() -> dict:
    return _read_yaml(paths.DEFAULTS_DIR / "config.yaml")


def env_overrides(environ=None) -> dict:
    """Settings from environment variables: DCSIM__TARGET__PORT=8000 sets target.port.

    Values are read as YAML, so numbers, true/false and lists work. Handy for
    containers and CI, where editing config.yaml is awkward.
    """
    import os
    environ = os.environ if environ is None else environ
    out: dict = {}
    for k, v in environ.items():
        if not k.startswith("DCSIM__"):
            continue
        keys = [x.lower() for x in k[7:].split("__") if x]
        if not keys:
            continue
        node = out
        for x in keys[:-1]:
            node = node.setdefault(x, {})
        try:
            node[keys[-1]] = yaml.safe_load(v)
        except yaml.YAMLError:
            node[keys[-1]] = v
    return out


def load_config() -> dict:
    """Packaged defaults, then the user's config.yaml, then DCSIM__* environment variables."""
    user = _read_yaml(paths.config_file()) if paths.config_file().exists() else {}
    return deep_merge(deep_merge(default_config(), user), env_overrides())


def _check_types(default: Any, value: Any, where: str) -> None:
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ConfigError(f"{where} must be a section")
        for k, v in value.items():
            if k in default:
                _check_types(default[k], v, f"{where}.{k}" if where else k)
        return
    if default is None or value is None:
        return
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false")
    elif isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if not (isinstance(default, str) or (isinstance(value, str) and value == "auto")):
                raise ConfigError(f"{where} must be a number")
    elif isinstance(default, list):
        if not isinstance(value, list):
            raise ConfigError(f"{where} must be a list")


def save_config(cfg: dict) -> dict:
    merged = deep_merge(default_config(), cfg)
    _check_types(default_config(), merged, "")
    if merged["target"]["mode"] not in ("builtin", "external"):
        raise ConfigError("target.mode must be builtin or external")
    if merged["target"]["mode"] == "external" and not str(merged["target"]["external_url"]).startswith(("http://", "https://")):
        raise ConfigError("target.external_url must start with http:// or https:// when mode is external")
    if merged["capacity"]["mode"] not in ("auto", "measured", "fixed"):
        raise ConfigError("capacity.mode must be auto, measured or fixed")
    if merged["server"]["rate_limit"]["key"] not in ("username", "ip"):
        raise ConfigError("server.rate_limit.key must be username or ip")
    sd = merged["load"].get("seed")
    if sd not in (None, "", "null"):
        try:
            merged["load"]["seed"] = int(sd)
        except (TypeError, ValueError):
            raise ConfigError("load.seed must be a whole number, or empty for a new random seed each run")
    else:
        merged["load"]["seed"] = None
    w = merged["load"]["workers"]
    if w != "auto" and (not isinstance(w, int) or w < 1):
        raise ConfigError("load.workers must be auto or a whole number of 1 or more")
    # lists like [10, 100] stay on one line; sections stay in block style
    _write_yaml(paths.config_file(), merged, header="# datacenter-sim settings - edit here or in the control panel (Settings).\n"
                "# Each option is explained in the panel and in the packaged defaults/config.yaml.\n\n", flow=False)
    return merged


def set_value(dotted: str, value: Any) -> dict:
    cfg = load_config()
    node = cfg
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    return save_config(cfg)


# ── interfaces ──────────────────────────────────────────────────────────

def load_interfaces() -> dict:
    p = paths.interfaces_file()
    data = _read_yaml(p) if p.exists() else _read_yaml(paths.DEFAULTS_DIR / "interfaces.yaml")
    return data or {}


def _range(v: Any, where: str, allow_none: bool = False) -> list | None:
    if v is None and allow_none:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = [v, v]
    if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and x >= 0 for x in v)):
        raise ConfigError(f"{where} must be [min, max] with numbers >= 0")
    lo, hi = sorted(float(x) for x in v)
    return [lo, hi]


def validate_interface(name: str, spec: dict) -> dict:
    if not PROFILE_NAME_RE.match(name):
        raise ConfigError(f"interface name '{name}' may only use lowercase letters, digits, _ and -")
    if not isinstance(spec, dict):
        raise ConfigError(f"interface {name} must be a section")
    out = dict(spec)
    out.setdefault("label", name)
    out.setdefault("description", "")
    out["login"] = bool(out.get("login", True))
    out["wait_sec"] = _range(out.get("wait_sec", [1, 3]), f"{name}.wait_sec")
    out["session_lifetime_sec"] = _range(out.get("session_lifetime_sec"), f"{name}.session_lifetime_sec", allow_none=True)
    out["heavy_user_fraction"] = float(out.get("heavy_user_fraction", 0) or 0)
    out["heavy_wait_multiplier"] = float(out.get("heavy_wait_multiplier", 1) or 1)
    if not 0 <= out["heavy_user_fraction"] <= 1:
        raise ConfigError(f"{name}.heavy_user_fraction must be between 0 and 1")
    tasks = out.get("tasks") or []
    if not tasks:
        raise ConfigError(f"interface {name} needs at least one task")
    clean = []
    for i, t in enumerate(tasks):
        where = f"{name}.tasks[{i + 1}]"
        if not isinstance(t, dict):
            raise ConfigError(f"{where} must be a section")
        protocol = str(t.get("protocol", "http")).lower()
        if protocol not in ("http", "grpc"):
            raise ConfigError(f"{where}.protocol must be http or grpc")
        method = str(t.get("method", "GET" if protocol == "http" else "CALL")).upper()
        if protocol == "http" and method not in METHODS:
            raise ConfigError(f"{where}.method must be one of {sorted(METHODS)}")
        path = str(t.get("path", ""))
        if not path.startswith("/"):
            raise ConfigError(f"{where}.path must start with /")
        if protocol == "grpc" and path.count("/") != 2:
            raise ConfigError(f"{where}.path for gRPC must look like /package.Service/Method")
        weight = t.get("weight", 1)
        if not isinstance(weight, (int, float)) or weight <= 0:
            raise ConfigError(f"{where}.weight must be a positive number")
        task = {"name": t.get("name") or f"{method} {path}", "method": method, "path": path,
                "weight": weight, "auth": bool(t.get("auth", True))}
        if protocol == "grpc":
            task["protocol"] = "grpc"
        assets = t.get("assets")
        if assets:
            if isinstance(assets, str):
                assets = [a.strip() for a in assets.split(",") if a.strip()]
            if not all(isinstance(a, str) and a.startswith("/") for a in assets):
                raise ConfigError(f"{where}.assets must be paths starting with /")
            task["assets"] = list(assets)
        for opt in ("params", "json"):
            if t.get(opt) not in (None, {}, ""):
                if not isinstance(t[opt], dict):
                    raise ConfigError(f"{where}.{opt} must be a set of key: value pairs")
                task[opt] = t[opt]
        if t.get("body_kb"):
            task["body_kb"] = int(t["body_kb"])
        if t.get("stream"):
            task["stream"] = True
        clean.append(task)
    out["tasks"] = clean
    return out


def save_interfaces(data: dict) -> dict:
    if not isinstance(data, dict) or not data:
        raise ConfigError("at least one interface is required")
    clean = {k: validate_interface(k, v) for k, v in data.items()}
    _write_yaml(paths.interfaces_file(), clean)
    return clean


# ── profiles ────────────────────────────────────────────────────────────

def list_profiles() -> list[dict]:
    out = []
    for p in sorted(paths.profiles_dir().glob("*.yaml")):
        try:
            d = _read_yaml(p)
            out.append({"id": p.stem, "name": d.get("name", p.stem), "description": d.get("description", ""),
                        "archetype": d.get("archetype", ""), "stages": len(d.get("stages") or []),
                        "duration": sum(int(s.get("duration", 0)) for s in d.get("stages") or [])})
        except Exception as e:  # a broken file should not hide the others
            out.append({"id": p.stem, "name": p.stem, "description": f"Could not read: {e}", "error": True,
                        "archetype": "", "stages": 0, "duration": 0})
    return out


def profile_path(pid: str) -> Path:
    if not PROFILE_NAME_RE.match(pid or ""):
        raise ConfigError("profile id may only use lowercase letters, digits, _ and -")
    return paths.profiles_dir() / f"{pid}.yaml"


def load_profile(pid: str) -> dict:
    p = profile_path(pid)
    if not p.exists():
        raise ConfigError(f"profile '{pid}' not found")
    return _read_yaml(p)


def parse_users(v: Any, where: str) -> tuple[str, float]:
    """Returns ('pct', value) or ('abs', value)."""
    if isinstance(v, bool):
        raise ConfigError(f"{where} must be a number or a percentage like 60%")
    if isinstance(v, (int, float)):
        if v < 0:
            raise ConfigError(f"{where} cannot be negative")
        return ("abs", float(v))
    s = str(v).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", s)
    if m:
        pct = float(m.group(1))
        if pct > 500:
            raise ConfigError(f"{where} above 500% is not allowed")
        return ("pct", pct)
    if re.fullmatch(r"\d+", s):
        return ("abs", float(s))
    raise ConfigError(f"{where} must be a number (e.g. 120) or a percentage of capacity (e.g. 60%)")


def validate_profile(data: dict, interfaces: dict | None = None) -> dict:
    interfaces = interfaces if interfaces is not None else load_interfaces()
    if not isinstance(data, dict):
        raise ConfigError("profile must be a section")
    out = {"name": str(data.get("name") or "Untitled"), "description": str(data.get("description") or ""),
           "archetype": str(data.get("archetype") or "custom")}
    mix = data.get("mix") or {}
    if not isinstance(mix, dict) or not mix:
        raise ConfigError("mix must list at least one interface with a weight")
    clean_mix = {}
    for k, w in mix.items():
        if k not in interfaces:
            raise ConfigError(f"mix uses unknown interface '{k}' (known: {', '.join(interfaces)})")
        if not isinstance(w, (int, float)) or isinstance(w, bool) or w < 0:
            raise ConfigError(f"mix weight for {k} must be a number >= 0")
        if w > 0:
            clean_mix[k] = w
    if not clean_mix:
        raise ConfigError("at least one interface needs a weight above 0")
    out["mix"] = clean_mix
    stages = data.get("stages") or []
    if not stages:
        raise ConfigError("a profile needs at least one stage")
    clean_stages = []
    for i, s in enumerate(stages):
        where = f"stage {i + 1}"
        if not isinstance(s, dict):
            raise ConfigError(f"{where} must be a section")
        phase = s.get("phase", "steady")
        if phase not in PHASES:
            raise ConfigError(f"{where}: phase must be one of {', '.join(PHASES)}")
        try:
            dur = int(s.get("duration", 0))
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: duration must be whole seconds")
        if dur <= 0:
            raise ConfigError(f"{where}: duration must be above 0 seconds")
        users = s.get("users", 0)
        if isinstance(users, dict):
            if not users:
                raise ConfigError(f"{where}: users per interface needs at least one interface")
            for k, v in users.items():
                if k not in clean_mix:
                    raise ConfigError(f"{where}: users lists '{k}' which is not in this profile's mix")
                parse_users(v, f"{where}: users for {k}")
        else:
            parse_users(users, f"{where}: users")
        sr = s.get("spawn_rate", "auto")
        if sr != "auto" and (isinstance(sr, bool) or not isinstance(sr, (int, float)) or sr <= 0):
            raise ConfigError(f"{where}: spawn_rate must be auto or a number above 0")
        stage = {"phase": phase, "duration": dur, "users": users, "spawn_rate": sr}
        smix = s.get("mix")
        if smix:
            if not isinstance(smix, dict):
                raise ConfigError(f"{where}: mix must list interface: weight")
            for k, w in smix.items():
                if k not in clean_mix:
                    raise ConfigError(f"{where}: mix lists '{k}' which is not in this profile's mix")
                if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
                    raise ConfigError(f"{where}: mix weight for {k} must be a number >= 0")
            if not any(w > 0 for w in smix.values()):
                raise ConfigError(f"{where}: mix needs a weight above 0")
            stage["mix"] = dict(smix)
        if s.get("label"):
            stage["label"] = str(s["label"])
        only = s.get("only")
        if only:
            if isinstance(only, str):
                only = [x.strip() for x in only.split(",") if x.strip()]
            bad = [x for x in only if x not in clean_mix]
            if bad:
                raise ConfigError(f"{where}: 'only' lists {', '.join(bad)} which is not in this profile's mix")
            stage["only"] = list(only)
        clean_stages.append(stage)
    out["stages"] = clean_stages
    seed = data.get("seed")
    if seed not in (None, ""):
        try:
            out["seed"] = int(seed)
        except (TypeError, ValueError):
            raise ConfigError("seed must be a whole number (or left empty for a new random seed each run)")
    return out


def save_profile(pid: str, data: dict) -> dict:
    clean = validate_profile(data)
    _write_yaml(profile_path(pid), clean)
    return clean


def delete_profile(pid: str) -> None:
    p = profile_path(pid)
    if p.exists():
        p.unlink()


def apportion(total: int, weights: dict) -> dict:
    """Split ``total`` whole users across keys in proportion to weights (largest remainder)."""
    total = max(0, int(total))
    w = {k: float(v) for k, v in weights.items() if v and float(v) > 0}
    if not w or total == 0:
        return {k: 0 for k in weights}
    s = sum(w.values())
    raw = {k: total * v / s for k, v in w.items()}
    out = {k: int(math.floor(x)) for k, x in raw.items()}
    left = total - sum(out.values())
    for k in sorted(raw, key=lambda k: (raw[k] - out[k], w[k], k), reverse=True)[:left]:
        out[k] += 1
    return {k: out.get(k, 0) for k in weights}


def _abs_users(v, capacity: int, where: str) -> int:
    kind, x = parse_users(v, where)
    return int(round(capacity * x / 100.0)) if kind == "pct" else int(x)


def resolve_profile(profile: dict, capacity: int) -> list[dict]:
    """Turn a profile's stages into absolute users per interface for this machine.

    A stage gives either a total (number or % of capacity), split by the
    stage's ``mix`` (or the profile mix) over the allowed interfaces, or a
    separate number per interface (``users: {web: 40%, api: 20}``).
    """
    out, prev, t = [], 0, 0
    for i, s in enumerate(profile["stages"]):
        where = f"stage {i + 1}"
        if isinstance(s["users"], dict):
            targets = {k: _abs_users(v, capacity, f"{where}: {k}") for k, v in s["users"].items()}
            weights = {k: v for k, v in targets.items() if v} or dict(profile["mix"])
            users = sum(targets.values())
        else:
            users = _abs_users(s["users"], capacity, where)
            weights = dict(s.get("mix") or profile["mix"])
            if s.get("only"):
                weights = {k: w for k, w in weights.items() if k in s["only"]}
            targets = apportion(users, weights)
        targets = {k: targets.get(k, 0) for k in profile["mix"]}
        if s["spawn_rate"] == "auto":
            rate = math.ceil(max(1.0, abs(users - prev) / max(1, s["duration"])) * 10) / 10
        else:
            rate = float(s["spawn_rate"])
        out.append({**s, "index": i, "users": users, "targets": targets, "weights": weights, "spawn_rate": rate,
                    "auto_rate": s["spawn_rate"] == "auto", "start": t, "end": t + s["duration"],
                    "label": s.get("label") or s["phase"].replace("_", " ")})
        prev, t = users, t + s["duration"]
    return out


def config_help() -> dict:
    """Map 'section.key' to the comment written next to it in the default settings file."""
    out, stack, pending = {}, [], []
    for raw in (paths.DEFAULTS_DIR / "config.yaml").read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            pending = []
            continue
        if s.startswith("#"):
            pending.append(s.lstrip("# ").strip())
            continue
        indent = (len(raw) - len(raw.lstrip())) // 2
        key = s.split(":", 1)[0].strip()
        stack = stack[:indent] + [key]
        comment = ""
        if " #" in s:
            comment = s.split(" #", 1)[1].strip()
        text = " ".join(pending + ([comment] if comment else []))
        if text:
            out[".".join(stack)] = text
        pending = []
    return out
