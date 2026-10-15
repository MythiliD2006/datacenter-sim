"""Worker agent: lends this computer's capacity to a control panel elsewhere.

    dcsim worker --panel http://192.168.1.20:8765

The agent registers with the panel (reporting how many users this computer can
drive), waits for a test to start, then launches load-generator worker
processes that connect to the panel computer's load master. It keeps running
between tests; stop it with Ctrl+C.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config, paths, system


def _agent_id() -> str:
    f = paths.home() / "agent-id"
    if f.exists():
        return f.read_text().strip()
    aid = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    f.write_text(aid)
    return aid


def run(panel: str, workers: int | None = None):
    panel = panel.rstrip("/")
    host = urlparse(panel).hostname or "127.0.0.1"
    cfg = config.load_config()
    cfg["target"]["mode"] = "external"        # nothing else runs on this computer, so use every core
    info = system.probe()
    cap = system.estimate_capacity(cfg, info)
    n_workers = workers or system.worker_count(cfg, cap["users"], info)
    aid = _agent_id()
    body = {"id": aid, "hostname": info["hostname"], "capacity": cap["users"], "workers": n_workers, "system": info}
    print(f"[dcsim worker] {info['hostname']}: {cap['users']} users with {n_workers} worker process(es)")
    print(f"[dcsim worker] joining {panel} - press Ctrl+C to stop")
    client = httpx.Client(timeout=5)
    registered = False
    procs: list[subprocess.Popen] = []
    try:
        while True:
            try:
                if not registered:
                    r = client.post(panel + "/api/agents/register", json=body)
                    r.raise_for_status()
                    if not r.json().get("allow", True):
                        print("[dcsim worker] the panel has remote workers turned off (load.allow_remote_workers)")
                    registered = True
                    print("[dcsim worker] connected; waiting for a test")
                r = client.get(panel + "/api/agents/poll", params={"id": aid})
                d = r.json()
                if not d.get("registered"):
                    registered = False
                    continue
                asg = d.get("assignment")
                if asg and not any(p.poll() is None for p in procs):
                    procs = launch(asg, host)
                    print(f"[dcsim worker] test started - {len(procs)} worker(s) connecting to {host}:{asg['master_port']}")
            except httpx.HTTPError as e:
                if registered:
                    print(f"[dcsim worker] lost the panel ({e}); retrying")
                registered = False
            if procs and all(p.poll() is not None for p in procs):
                print("[dcsim worker] test finished; waiting for the next one")
                procs = []
            time.sleep(2)
    except KeyboardInterrupt:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        print("\n[dcsim worker] stopped")


def _rehost(url: str | None, host: str) -> str | None:
    """The plan names servers as the panel computer sees them (127.0.0.1); swap in its real address."""
    if not url:
        return url
    for loop in ("127.0.0.1", "localhost", "0.0.0.0"):
        url = url.replace(loop, host)
    return url


def launch(asg: dict, host: str) -> list[subprocess.Popen]:
    plan = asg["plan"]
    plan["target_url_override"] = _rehost(plan.get("target_url"), host)
    plan["grpc_target"] = _rehost(plan.get("grpc_target"), host)
    d = paths.scratch_dir() / "agent-runs" / str(plan.get("seed", "run"))
    d.mkdir(parents=True, exist_ok=True)
    pf = d / "plan.json"
    pf.write_text(json.dumps(plan), encoding="utf-8")
    procs = []
    for k in range(int(asg["workers"])):
        idx = int(asg["worker_index"]) + k
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "dcsim.load.runner", "--plan", str(pf), "--mode", "worker", "--master-host", host,
             "--master-port", str(asg["master_port"]), "--worker-index", str(idx), "--workers", str(asg["total_workers"]),
             "--capacity", str(asg["capacity_per_worker"]), "--parent-pid", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=open(d / f"worker{idx}.log", "w"), cwd=str(d)))
    return procs
