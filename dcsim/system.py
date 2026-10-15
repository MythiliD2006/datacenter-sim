"""Looks at the computer the package runs on and works out how many simulated
users it can drive. Nothing here is fixed: every coefficient comes from the
``capacity`` and ``load`` sections of the settings."""
from __future__ import annotations

import math
import os
import platform
import socket

import psutil


def fd_limit() -> int | None:
    try:
        import resource  # not on Windows
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        return None if soft == resource.RLIM_INFINITY else int(soft)
    except Exception:
        return None  # Windows has no practical per-process socket cap at this scale


def probe() -> dict:
    vm = psutil.virtual_memory()
    du = None
    for candidate in (os.path.expanduser("~"), os.getcwd(), os.path.abspath(os.sep)):
        try:
            du = psutil.disk_usage(candidate)
            break
        except OSError:
            continue
    try:
        freq = psutil.cpu_freq()
        mhz = round(freq.max or freq.current) if freq else None
    except Exception:
        mhz = None
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.system(),
        "python": platform.python_version(),
        "cpu_model": platform.processor() or platform.machine(),
        "logical_cores": psutil.cpu_count(logical=True) or 1,
        "physical_cores": psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 1,
        "cpu_mhz": mhz,
        "memory_total_mb": round(vm.total / 2**20),
        "memory_available_mb": round(vm.available / 2**20),
        "disk_free_gb": round(du.free / 2**30, 1) if du else None,
        "fd_limit": fd_limit(),
    }


def estimate_capacity(cfg: dict, info: dict | None = None) -> dict:
    """Return the number of users this machine should drive, with the reasoning."""
    info = info or probe()
    c = cfg["capacity"]
    by_cpu = info["logical_cores"] * c["users_per_core"]
    usable_mb = info["memory_available_mb"] * (1 - c["reserve_memory_fraction"])
    by_mem = usable_mb / max(c["mb_per_user"], 0.01)
    limits = {"cpu": int(by_cpu), "memory": int(by_mem)}
    if info.get("fd_limit"):
        limits["open_files"] = int(info["fd_limit"] * 0.8 / max(c["fds_per_user"], 1))
    limits["configured_max"] = int(c["max_users"])
    auto = max(int(c["min_users"]), min(limits.values()))
    limiting = min(limits, key=limits.get)

    mode = c["mode"]
    if mode == "fixed":
        users, source = int(c["fixed_users"]), "fixed in settings"
    elif mode == "measured" and int(c.get("measured_users") or 0) > 0:
        users, source = int(c["measured_users"]), "measured by calibration"
    else:
        users, source = auto, f"estimated from this computer (limited by {limiting.replace('_', ' ')})"
        mode = "auto"
    return {"users": max(1, users), "auto_estimate": auto, "mode": mode, "source": source,
            "limits": limits, "limiting_factor": limiting}


def worker_count(cfg: dict, peak_users: int, info: dict | None = None) -> int:
    info = info or probe()
    w = cfg["load"]["workers"]
    if w != "auto":
        return max(1, int(w))
    cores = info["logical_cores"]
    # leave a core for the server under test when it runs on this machine
    spare = cores - 1 if cfg["target"]["mode"] == "builtin" and cores > 1 else cores
    need = math.ceil(peak_users / max(cfg["load"]["users_per_worker"], 1))
    return max(1, min(need, spare, cfg["load"]["max_workers"]))


def free_port(host: str = "127.0.0.1", preferred: int = 0) -> int:
    """Return ``preferred`` if it is free, else any free port."""
    for candidate in ([preferred] if preferred else []) + list(range(preferred + 1, preferred + 50) if preferred else []):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host if host != "0.0.0.0" else "", candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host if host != "0.0.0.0" else "", 0))
        return s.getsockname()[1]
