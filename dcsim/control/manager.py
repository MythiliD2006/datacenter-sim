"""Runs everything behind the control panel: the server under test, the load
generator, system monitoring, the crash watchdog and run recording."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx
import psutil
from prometheus_client import CollectorRegistry, Gauge, generate_latest

from .. import config, paths, system
from . import analysis, charts, report

log = logging.getLogger("dcsim.control")


def _now_id(profile_id: str) -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + profile_id


def _tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block, data = 8192, b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    return [l.decode("utf-8", "replace") for l in data.splitlines()[-lines:]]


def compact(sample: dict) -> dict:
    """Small version of a sample for the live view (no histograms)."""
    def side(d, dt):
        out = {}
        for k, v in (d or {}).items():
            h = analysis.hist_merge([v.get("hist")])
            out[k] = {"rps": round(v["count"] / dt, 2), "err": v["fails"], "count": v["count"],
                      "p95": analysis.pct(h, 0.95)}
        return out
    dt = max(sample.get("dt") or 1.0, 1e-6)
    keep = ("t", "run_time", "ts", "stage", "phase", "label", "users", "target_users", "users_by_iface", "targets_by_iface",
            "users_by_worker",
            "count", "fails",
            "rps", "fail_rps", "p50", "p95", "p99", "sys", "target", "load", "override", "final")
    out = {k: sample.get(k) for k in keep}
    out["iface"] = side(sample.get("iface"), dt)
    out["endpoint"] = side(sample.get("endpoint"), dt)
    return out


class RunState:
    def __init__(self, run_id: str, run_dir: Path, meta: dict, kind: str):
        self.id = run_id
        self.dir = run_dir
        self.meta = meta
        self.kind = kind
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.samples_file = open(run_dir / "samples.jsonl", "a", encoding="utf-8")
        self.events_file = open(run_dir / "events.jsonl", "a", encoding="utf-8")
        self.last_tick_t = 0.0
        self.last_stage = None
        self.stopping = False
        self.finished = False
        self.processed = False          # True once summary, charts and report are written
        self.latest: dict | None = None
        self.n_samples = 0


class Manager:
    def __init__(self):
        self.cfg = config.load_config()
        self.info = system.probe()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set = set()
        self.live: deque = deque(maxlen=900)
        self.events: deque = deque(maxlen=300)
        self.target_proc: subprocess.Popen | None = None
        self.target_port: int | None = None
        self.target_accounts = 0
        self.target_state = "stopped"
        self.target_started_at = None
        self.health_fails = 0
        self.run: RunState | None = None
        self.sys_latest: dict = {}
        self.target_latest: dict = {}
        self.load_latest: dict = {}
        self._pcache: dict[int, psutil.Process] = {}
        self._io_prev = None
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=2.5)
        self._tasks: list = []
        self.control_port = None
        self.panel_host = "127.0.0.1"
        self.target_grpc_port: int | None = None
        self.agents: dict[str, dict] = {}       # other computers running "dcsim worker"
        self._init_metrics()

    # ── Prometheus metrics for Grafana ─────────────────────────────────
    def _init_metrics(self):
        r = self.metrics_registry = CollectorRegistry()
        self.g = {
            "users": Gauge("dcsim_users", "Simulated users running", registry=r),
            "target_users": Gauge("dcsim_target_users", "Users the plan asks for now", registry=r),
            "rps": Gauge("dcsim_requests_per_second", "Requests per second", registry=r),
            "fail_rps": Gauge("dcsim_failures_per_second", "Failed requests per second", registry=r),
            "p50": Gauge("dcsim_latency_p50_ms", "p50 latency", registry=r),
            "p95": Gauge("dcsim_latency_p95_ms", "p95 latency", registry=r),
            "p99": Gauge("dcsim_latency_p99_ms", "p99 latency", registry=r),
            "stage": Gauge("dcsim_stage_index", "Current stage number", registry=r),
            "phase": Gauge("dcsim_phase", "1 for the current phase", ["phase", "label"], registry=r),
            "iface_users": Gauge("dcsim_interface_users", "Users per interface", ["interface"], registry=r),
            "iface_rps": Gauge("dcsim_interface_rps", "Requests per second per interface", ["interface"], registry=r),
            "iface_p95": Gauge("dcsim_interface_p95_ms", "p95 per interface", ["interface"], registry=r),
            "running": Gauge("dcsim_test_running", "1 while a test runs", registry=r),
            "host_cpu": Gauge("dcsim_host_cpu_percent", "Load-generating computer CPU", registry=r),
            "host_mem": Gauge("dcsim_host_memory_percent", "Load-generating computer memory", registry=r),
            "server_cpu": Gauge("dcsim_server_cpu_percent", "Built-in server process CPU", registry=r),
            "server_rss": Gauge("dcsim_server_memory_mb", "Built-in server process memory", registry=r),
            "sessions": Gauge("dcsim_server_sessions", "Open sessions on the server", registry=r),
            "capacity": Gauge("dcsim_capacity_users", "Users all connected computers can drive", registry=r),
            "agents": Gauge("dcsim_worker_machines", "Remote worker computers connected", registry=r),
        }
        self._last_phase = None

    def update_metrics(self, s: dict):
        g = self.g
        sysd, tg = s.get("sys") or {}, s.get("target") or {}
        for k, v in (("host_cpu", sysd.get("cpu_pct")), ("host_mem", sysd.get("mem_pct")), ("server_cpu", tg.get("cpu_pct")),
                     ("server_rss", tg.get("rss_mb")), ("sessions", tg.get("sessions"))):
            if v is not None:
                g[k].set(v)
        g["agents"].set(len(self.live_agents()))
        g["running"].set(0 if s.get("idle") else 1)
        if s.get("idle"):
            for k in ("users", "rps", "fail_rps", "target_users"):
                g[k].set(0)
            return
        for k in ("users", "rps", "fail_rps", "p50", "p95", "p99"):
            if s.get(k) is not None:
                g[k].set(s[k])
        if s.get("target_users") is not None:
            g["target_users"].set(s["target_users"])
        if s.get("stage") is not None:
            g["stage"].set(s["stage"])
        ph = (s.get("phase") or "", s.get("label") or "")
        if ph != self._last_phase:
            if self._last_phase:
                g["phase"].labels(*self._last_phase).set(0)
            g["phase"].labels(*ph).set(1)
            self._last_phase = ph
        for k, n in (s.get("users_by_iface") or {}).items():
            g["iface_users"].labels(k).set(n)
        for k, v in (s.get("iface") or {}).items():
            g["iface_rps"].labels(k).set(v.get("rps") or 0)
            if v.get("p95") is not None:
                g["iface_p95"].labels(k).set(v["p95"])

    def metrics_text(self) -> bytes:
        self.g["capacity"].set(self.total_capacity()["users"])
        self.g["agents"].set(len(self.live_agents()))
        return generate_latest(self.metrics_registry)

    # ── remote worker computers ────────────────────────────────────────
    def live_agents(self) -> list[dict]:
        now = time.time()
        return [a for a in self.agents.values() if now - a["last_seen"] < 10]

    def register_agent(self, info: dict, remote_host: str) -> dict:
        aid = str(info.get("id") or "")[:64]
        if not aid:
            raise ValueError("agent id missing")
        a = self.agents.get(aid, {})
        a.update({"id": aid, "hostname": str(info.get("hostname", "?"))[:80], "address": remote_host,
                  "capacity": max(1, int(info.get("capacity") or 1)), "workers": max(1, min(int(info.get("workers") or 1), 64)),
                  "system": info.get("system") or {}, "last_seen": time.time()})
        new = aid not in self.agents
        self.agents[aid] = a
        if new:
            self.event("info", f"Worker computer {a['hostname']} ({a['address']}) joined: +{a['capacity']} users, {a['workers']} worker(s)")
            self.broadcast({"type": "state", "state": self.state()})
        return {"ok": True, "allow": bool(self.cfg["load"]["allow_remote_workers"])}

    def poll_agent(self, aid: str) -> dict:
        a = self.agents.get(aid)
        if not a:
            return {"registered": False}
        a["last_seen"] = time.time()
        run = self.run
        if run and not run.finished and aid in (run.meta.get("agent_assignments") or {}):
            asg = run.meta["agent_assignments"][aid]
            if not asg.get("taken"):
                asg["taken"] = True
                plan = json.loads((run.dir / "plan.json").read_text(encoding="utf-8"))
                return {"registered": True, "assignment": {**{k: v for k, v in asg.items() if k != "taken"}, "plan": plan}}
        return {"registered": True, "assignment": None, "running": bool(run and not run.finished)}

    def total_capacity(self) -> dict:
        local = system.estimate_capacity(self.cfg, self.info)
        agents = self.live_agents() if self.cfg["load"]["allow_remote_workers"] else []
        remote = sum(a["capacity"] for a in agents)
        return {**local, "local_users": local["users"], "remote_users": remote, "users": local["users"] + remote,
                "machines": 1 + len(agents),
                "source": local["source"] + (f", plus {remote} from {len(agents)} worker computer(s)" if agents else "")}

    def _bind_host(self) -> str:
        """Open the built-in server to the network only when other computers need to reach it."""
        h = self.cfg["target"]["host"]
        if self.cfg["load"]["allow_remote_workers"] and self.panel_host in ("0.0.0.0", "", "::"):
            return "0.0.0.0"
        return h

    @property
    def grpc_target(self) -> str | None:
        t = self.cfg["target"]
        if t["mode"] == "external":
            return t.get("external_grpc") or None
        if self.target_grpc_port:
            return f"127.0.0.1:{self.target_grpc_port}"
        return None

    # ── helpers ────────────────────────────────────────────────────────
    def reload_config(self):
        self.cfg = config.load_config()

    def capacity(self) -> dict:
        self.info = system.probe()
        return self.total_capacity()

    @property
    def target_url(self) -> str | None:
        t = self.cfg["target"]
        if t["mode"] == "external":
            return t["external_url"].rstrip("/") or None
        if self.target_port:
            host = t["host"] if t["host"] not in ("0.0.0.0", "") else "127.0.0.1"
            return f"http://{host}:{self.target_port}"
        return None

    def event(self, kind: str, msg: str, **extra):
        e = {"ts": time.time(), "kind": kind, "msg": msg, **extra}
        if self.run and not self.run.finished:
            e["t"] = round(time.time() - self.run.started, 1)
            e["phase"] = (self.run.latest or {}).get("label")
            self.run.events_file.write(json.dumps(e) + "\n")
            self.run.events_file.flush()
        self.events.append(e)
        log.info("%s: %s", kind, msg)
        self.broadcast({"type": "event", "event": e})

    def broadcast(self, msg: dict):
        if not self.loop:
            return
        data = json.dumps(msg, default=str)

        async def send_all():
            dead = []
            for ws in list(self.clients):
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(send_all())
        else:
            asyncio.run_coroutine_threadsafe(send_all(), self.loop)

    def state(self) -> dict:
        cap = self.total_capacity()
        run = None
        if self.run:
            r = self.run
            run = {"id": r.id, "kind": r.kind, "profile_id": r.meta["profile_id"], "profile_name": r.meta["profile"]["name"],
                   "started": r.started, "elapsed": round(time.time() - r.started, 1), "total": r.meta["total_duration"],
                   "stages": r.meta["stages"], "workers": r.meta["workers"], "stopping": r.stopping,
                   "current_stage": (r.latest or {}).get("stage"), "finished": r.finished, "seed": r.meta.get("seed"),
                   "remote_workers": len(r.meta.get("agent_assignments") or {})}
        return {"target": {"mode": self.cfg["target"]["mode"], "state": self.target_state, "url": self.target_url,
                           "pid": self.target_proc.pid if self.target_proc and self.target_proc.poll() is None else None,
                           "accounts": self.target_accounts, "started_at": self.target_started_at, "grpc": self.grpc_target},
                "agents": [{k: a[k] for k in ("id", "hostname", "address", "capacity", "workers")} for a in self.live_agents()],
                "run": run, "capacity": cap, "system": self.info, "control_port": self.control_port,
                "home": str(paths.home())}

    # ── lifecycle ──────────────────────────────────────────────────────
    async def startup(self):
        self.loop = asyncio.get_running_loop()
        self._recover_unfinished_runs()
        self._tasks.append(asyncio.create_task(self._monitor_loop()))
        if self.cfg["target"]["mode"] == "external":
            self.target_state = "external"
        elif self.cfg["target"]["auto_start"]:
            try:
                await self.start_target()
            except Exception as e:
                self.event("warning", f"Could not start the built-in server: {e}")

    async def shutdown(self):
        if self.run and not self.run.finished:
            await self.stop_run(wait=True)
        await self.stop_target()
        for t in self._tasks:
            t.cancel()
        await self._http.aclose()

    def _recover_unfinished_runs(self):
        """A run whose panel was closed mid-test still gets a report."""
        for d in paths.runs_dir().glob("*"):
            if (d / "meta.json").exists() and not (d / "summary.json").exists():
                try:
                    self._finalize_dir(d, interrupted=True)
                except Exception as e:
                    log.warning("could not finalize %s: %s", d, e)

    # ── server under test ─────────────────────────────────────────────
    def default_accounts(self) -> int:
        cap = self.total_capacity()["users"]
        return max(10, math.ceil(cap * float(self.cfg["auth"]["accounts_per_user"])))

    async def start_target(self, accounts: int | None = None):
        async with self._lock:
            t = self.cfg["target"]
            if t["mode"] == "external":
                self.target_state = "external"
                ok = await self._health()
                self.event("info", f"Using external server {self.target_url} ({'reachable' if ok else 'NOT reachable'})")
                return
            if self.target_proc and self.target_proc.poll() is None:
                return
            accounts = accounts or self.default_accounts()
            bind = self._bind_host()
            self.target_port = system.free_port(bind, int(t["port"] or 0))
            grpc_on = bool(self.cfg["server"].get("grpc_enabled", True))
            self.target_grpc_port = system.free_port(bind, int(t.get("grpc_port") or 0)) if grpc_on else None
            self.target_accounts = accounts
            self.target_state = "starting"
            self.broadcast({"type": "state", "state": self.state()})
            errlog = open(paths.logs_dir() / "target.stderr.log", "a", encoding="utf-8")
            self.target_proc = subprocess.Popen(
                [sys.executable, "-m", "dcsim.target", "--port", str(self.target_port), "--accounts", str(accounts),
                 "--host", bind, "--home", str(paths.home()), "--parent-pid", str(os.getpid())]
                + (["--grpc-port", str(self.target_grpc_port)] if self.target_grpc_port else []),
                stdout=errlog, stderr=errlog, cwd=str(paths.home()), **_popen_flags())
            for _ in range(120):
                await asyncio.sleep(0.25)
                if self.target_proc.poll() is not None:
                    self.target_state = "crashed"
                    raise RuntimeError("server exited during start-up; see Logs > server errors")
                if await self._health():
                    break
            else:
                self.target_state = "crashed"
                raise RuntimeError("server did not answer its health check within 30 s")
            self.target_state = "running"
            self.target_started_at = time.time()
            self.health_fails = 0
            self.event("info", f"Built-in server started on port {self.target_port}"
                       + (f" (gRPC {self.target_grpc_port})" if self.target_grpc_port else "") + f" with {accounts} accounts")
            self.broadcast({"type": "state", "state": self.state()})

    async def stop_target(self):
        if self.cfg["target"]["mode"] == "external":
            return
        p = self.target_proc
        if p and p.poll() is None:
            p.terminate()
            for _ in range(40):
                if p.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if p.poll() is None:
                p.kill()
            self.event("info", "Built-in server stopped")
        self.target_proc = None
        self.target_state = "stopped"
        self.target_latest = {}
        self.broadcast({"type": "state", "state": self.state()})

    async def restart_target(self, accounts: int | None = None):
        await self.stop_target()
        await self.start_target(accounts)

    async def _health(self) -> bool:
        url = self.target_url
        if not url:
            return False
        try:
            r = await self._http.get(url + self.cfg["target"]["health_path"])
            return r.status_code < 500
        except Exception:
            return False

    # ── monitoring ─────────────────────────────────────────────────────
    def _proc(self, pid: int):
        p = self._pcache.get(pid)
        if p is None:
            try:
                p = psutil.Process(pid)
                p.cpu_percent(None)
                self._pcache[pid] = p
            except psutil.Error:
                return None
        return p

    def _sample_system(self, dt: float) -> dict:
        vm = psutil.virtual_memory()
        out = {"cpu_pct": psutil.cpu_percent(None), "mem_pct": vm.percent, "mem_used_mb": round((vm.total - vm.available) / 2**20)}
        try:
            disk = psutil.disk_io_counters()
        except Exception:
            disk = None
        net = psutil.net_io_counters()
        cur = (disk.read_bytes if disk else 0, disk.write_bytes if disk else 0, net.bytes_recv, net.bytes_sent)
        if self._io_prev and dt > 0:
            d = [max(0, a - b) / 2**20 / dt for a, b in zip(cur, self._io_prev)]
            out.update(disk_read_mbs=round(d[0], 2), disk_write_mbs=round(d[1], 2), net_rx_mbs=round(d[2], 2), net_tx_mbs=round(d[3], 2))
        self._io_prev = cur
        return out

    def _sample_procs(self) -> tuple[dict, dict]:
        target = {}
        if self.target_proc and self.target_proc.poll() is None:
            p = self._proc(self.target_proc.pid)
            if p:
                try:
                    with p.oneshot():
                        target = {"cpu_pct": round(p.cpu_percent(None), 1), "rss_mb": round(p.memory_info().rss / 2**20, 1),
                                  "threads": p.num_threads()}
                        try:
                            target["open_files"] = p.num_fds() if hasattr(p, "num_fds") else p.num_handles()
                        except Exception:
                            pass
                except psutil.Error:
                    pass
        load = {}
        if self.run and self.run.proc and self.run.proc.poll() is None:
            root = self._proc(self.run.proc.pid)
            procs = [root] if root else []
            try:
                procs += [self._proc(c.pid) for c in root.children(recursive=True)] if root else []
            except psutil.Error:
                pass
            cpus, rss = [], 0.0
            for p in procs:
                if not p:
                    continue
                try:
                    cpus.append(p.cpu_percent(None))
                    rss += p.memory_info().rss / 2**20
                except psutil.Error:
                    pass
            if cpus:
                load = {"processes": len(cpus), "cpu_pct": round(sum(cpus), 1), "cpu_pct_per_worker": round(max(cpus), 1),
                        "rss_mb": round(rss, 1)}
        return target, load

    async def _monitor_loop(self):
        prev = time.time()
        psutil.cpu_percent(None)
        while True:
            interval = float(self.cfg["control_panel"]["sample_interval_sec"])
            await asyncio.sleep(interval)
            now = time.time()
            dt, prev = now - prev, now
            try:
                self.sys_latest = self._sample_system(dt)
                target, load = self._sample_procs()
                healthy = None
                if self.target_url and self.target_state in ("running", "external", "crashed"):
                    if self.cfg["target"]["mode"] == "builtin":
                        try:
                            r = await self._http.get(self.target_url + "/stats")
                            healthy = r.status_code < 500
                            if healthy:
                                st = r.json()
                                target["sessions"] = st.get("active_sessions")
                                target["sessions_expired"] = st.get("sessions_expired")
                        except Exception:
                            healthy = False
                    else:
                        healthy = await self._health()
                    target["up"] = healthy
                self.target_latest, self.load_latest = target, load
                await self._watchdog(healthy)
                if not (self.run and not self.run.finished):
                    s = {"t": None, "ts": now, "users": 0, "rps": 0, "sys": self.sys_latest, "target": target, "load": load,
                         "idle": True}
                    self.live.append(s)
                    self.update_metrics(s)
                    self.broadcast({"type": "sample", "sample": s})
            except Exception as e:
                log.exception("monitor error: %s", e)

    async def _watchdog(self, healthy):
        mode = self.cfg["target"]["mode"]
        if mode == "builtin" and self.target_state == "running" and self.target_proc and self.target_proc.poll() is not None:
            self._crash(f"server process exited with code {self.target_proc.returncode}")
            return
        if healthy is None:
            return
        if healthy:
            if self.health_fails >= self.cfg["target"]["health_fail_threshold"]:
                self.event("info", "Server is responding again")
                if self.target_state == "crashed":
                    self.target_state = "running" if mode == "builtin" else "external"
            self.health_fails = 0
            return
        self.health_fails += 1
        if self.health_fails == self.cfg["target"]["health_fail_threshold"] and self.target_state != "crashed":
            self._crash(f"{self.health_fails} health checks in a row failed")

    def _crash(self, reason: str):
        self.target_state = "crashed"
        snap_dir = self.run.dir if self.run and not self.run.finished else paths.logs_dir()
        snap = snap_dir / f"crash_{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        lines = [f"Crash detected: {reason}", f"Time: {datetime.now().isoformat()}", f"Server: {self.target_url}",
                 f"Last system sample: {json.dumps(self.sys_latest)}", f"Last server sample: {json.dumps(self.target_latest)}",
                 "", "---- last server log lines ----"]
        lines += _tail(paths.logs_dir() / "target.log", 200) + ["", "---- server errors ----"]
        lines += _tail(paths.logs_dir() / "target.stderr.log", 100)
        snap.write_text("\n".join(lines), encoding="utf-8")
        self.event("crash", f"Server crash: {reason}. Snapshot saved to {snap.name}", snapshot=str(snap))
        self.broadcast({"type": "state", "state": self.state()})
        if self.cfg["target"]["auto_restart_on_crash"] and self.cfg["target"]["mode"] == "builtin":
            asyncio.ensure_future(self.restart_target(self.target_accounts or None))

    # ── runs ───────────────────────────────────────────────────────────
    async def start_run(self, profile_id: str, kind: str = "test", profile: dict | None = None, seed: int | None = None) -> dict:
        if self.run and not self.run.finished:
            raise RuntimeError("a test is already running - stop it first")
        self.reload_config()
        interfaces = config.load_interfaces()
        profile = config.validate_profile(profile or config.load_profile(profile_id), interfaces)
        cap = self.capacity()
        stages = config.resolve_profile(profile, cap["users"])
        peak = max(s["users"] for s in stages)
        if peak <= 0:
            raise RuntimeError("every stage has 0 users")
        uses_grpc = any(t.get("protocol") == "grpc" for k in profile["mix"] for t in interfaces[k]["tasks"])
        if self.cfg["target"]["mode"] == "builtin":
            need = max(10, math.ceil(peak * float(self.cfg["auth"]["accounts_per_user"])))
            if not (self.target_proc and self.target_proc.poll() is None):
                await self.start_target(max(need, self.default_accounts()))
            elif self.target_accounts < need:
                self.event("info", f"Restarting the server with {need} accounts for {peak} users")
                await self.restart_target(need)
        elif not await self._health():
            raise RuntimeError(f"external server {self.target_url} is not reachable")
        if uses_grpc and not self.grpc_target:
            raise RuntimeError("this profile uses gRPC interfaces but no gRPC server is set "
                               "(turn on server.grpc_enabled, or set target.external_grpc)")

        # reproducibility: profile seed > settings seed > fresh random seed (always recorded)
        if seed is None:
            seed = profile.get("seed")
        if seed is None and self.cfg["load"].get("seed") not in (None, "", "null"):
            seed = int(self.cfg["load"]["seed"])
        if seed is None:
            seed = int.from_bytes(os.urandom(3), "big")
        if self.cfg["target"]["mode"] == "builtin":
            try:
                await self._http.post(self.target_url + "/admin/seed", json={"seed": seed})
            except Exception:
                pass

        # split users between this computer and any worker computers
        agents = self.live_agents() if self.cfg["load"]["allow_remote_workers"] else []
        local_cap = cap["local_users"]
        local_peak = math.ceil(peak * local_cap / max(1, cap["users"]))
        workers = system.worker_count(self.cfg, local_peak, self.info)
        remote, assignments, next_index = [], {}, workers
        for a in agents:
            remote.append({"id": a["id"], "workers": a["workers"], "capacity": a["capacity"]})
            assignments[a["id"]] = {"hostname": a["hostname"], "worker_index": next_index, "workers": a["workers"],
                                    "capacity_per_worker": a["capacity"] / a["workers"]}
            next_index += a["workers"]
        total_workers = next_index
        for asg in assignments.values():
            asg["total_workers"] = total_workers
        distributed = workers > 1 or bool(remote)
        master_port = 0
        if distributed:
            master_port = system.free_port("0.0.0.0" if remote else "127.0.0.1", int(self.cfg["load"]["master_port"]) if remote else 0)
            for asg in assignments.values():
                asg["master_port"] = master_port

        run_id = _now_id(profile_id)
        run_dir = paths.runs_dir() / run_id
        run_dir.mkdir(parents=True)
        total = stages[-1]["end"]
        meta = {"id": run_id, "kind": kind, "profile_id": profile_id, "profile": profile, "stages": stages,
                "total_duration": total, "capacity": cap, "system": self.info, "workers": total_workers,
                "local_workers": workers, "agent_assignments": assignments, "seed": seed,
                "target_url": self.target_url, "grpc_target": self.grpc_target, "target_mode": self.cfg["target"]["mode"],
                "accounts": self.target_accounts, "interfaces": {k: interfaces[k] for k in profile["mix"]},
                "started": time.time(), "config": self.cfg}
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        accounts = self.target_accounts if self.cfg["target"]["mode"] == "builtin" else max(1, math.ceil(peak * float(self.cfg["auth"]["accounts_per_user"])))
        plan = {"target_url": self.target_url, "grpc_target": self.grpc_target, "auth": self.cfg["auth"], "accounts": accounts,
                "stages": stages, "mix": profile["mix"], "interfaces": {k: interfaces[k] for k in profile["mix"]},
                "sample_interval": self.cfg["control_panel"]["sample_interval_sec"], "workers": workers,
                "remote_workers": remote, "local_capacity": local_cap, "master_port": master_port,
                "master_bind": "0.0.0.0" if remote else "127.0.0.1", "seed": seed,
                "worker_wait_sec": self.cfg["load"]["worker_wait_sec"], "stop_timeout": self.cfg["load"]["stop_timeout_sec"]}
        (run_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
        self.run = RunState(run_id, run_dir, meta, kind)
        self.live.clear()
        err = open(run_dir / "runner.stderr.log", "w", encoding="utf-8")
        self.run.proc = subprocess.Popen([sys.executable, "-m", "dcsim.load.runner", "--plan", str(run_dir / "plan.json"),
                                          "--parent-pid", str(os.getpid())],
                                         stdout=subprocess.PIPE, stderr=err, text=True, bufsize=1, cwd=str(run_dir),
                                         **_popen_flags())
        threading.Thread(target=self._read_runner, args=(self.run,), daemon=True).start()
        where = f"{workers} local worker(s)" + (f" + {total_workers - workers} on {len(remote)} other computer(s)" if remote else "")
        self.event("info", f"Started '{profile['name']}' with up to {peak} users (capacity {cap['users']}, {cap['source']}); "
                           f"{where}; seed {seed}")
        self.broadcast({"type": "state", "state": self.state()})
        return self.state()

    def _read_runner(self, run: RunState):
        for line in run.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            asyncio.run_coroutine_threadsafe(self._on_runner_msg(run, msg), self.loop)
        run.proc.wait()
        asyncio.run_coroutine_threadsafe(self._finish_run(run), self.loop)

    async def _on_runner_msg(self, run: RunState, msg: dict):
        kind = msg.get("type")
        if kind == "event":
            if not str(msg.get("msg", "")).startswith("command applied"):
                self.event("info", msg.get("msg", ""))
        elif kind == "tick":
            dt = max(0.05, (msg["t"] - run.last_tick_t)) if msg.get("t") is not None else 1.0
            run.last_tick_t = msg.get("t") or run.last_tick_t
            hist = analysis.hist_merge([msg.get("hist")])
            s = dict(msg)
            s.pop("type", None)
            s.update({"ts": time.time(), "dt": round(dt, 3), "rps": round(msg["count"] / dt, 2),
                      "fail_rps": round(msg["fails"] / dt, 2), "p50": analysis.pct(hist, 0.5),
                      "p95": analysis.pct(hist, 0.95), "p99": analysis.pct(hist, 0.99),
                      "sys": self.sys_latest, "target": self.target_latest, "load": self.load_latest})
            run.samples_file.write(json.dumps(s, separators=(",", ":")) + "\n")
            run.n_samples += 1
            if run.n_samples % 5 == 0:
                run.samples_file.flush()
            run.latest = s
            if s.get("stage") != run.last_stage and not s.get("final"):
                st = run.meta["stages"][s["stage"]] if s.get("stage") is not None and s["stage"] < len(run.meta["stages"]) else {}
                ph = st.get("phase", "?").replace("_", " ")
                lab = st.get("label", "")
                name = ph.capitalize() if lab in ("", ph) else f"{lab} ({ph})"
                self.event("phase", f"{name}: {st.get('users')} users")
                run.last_stage = s.get("stage")
            c = compact(s)
            self.update_metrics(c)
            self.live.append(c)
            self.broadcast({"type": "sample", "sample": c})
        elif kind == "done":
            run.meta["stopped_early"] = bool(msg.get("stopped_early"))

    async def _finish_run(self, run: RunState):
        if run.finished:
            return
        run.finished = True
        code = run.proc.returncode if run.proc else None
        if code not in (0, None) and not run.stopping:
            tail = "\n".join(_tail(run.dir / "runner.stderr.log", 15))
            self.event("warning", f"Load generator exited with code {code}. {tail[-500:]}")
        run.samples_file.close()
        run.events_file.close()
        meta = run.meta
        meta["ended"] = time.time()
        meta["stopped_early"] = meta.get("stopped_early") or run.stopping
        (run.dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        try:
            summary = await asyncio.get_running_loop().run_in_executor(None, self._finalize_dir, run.dir, False)
            msg = f"Run finished: {summary['verdict']} - {len(summary['findings'])} finding(s)"
            if run.kind == "calibration":
                cal = summary.get("calibration") or {}
                if cal.get("measured_users"):
                    config.set_value("capacity.measured_users", int(cal["measured_users"]))
                    config.set_value("capacity.mode", "measured")
                    self.reload_config()
                    msg = f"Calibration finished: this computer handles about {cal['measured_users']} users within the SLA"
                else:
                    msg = "Calibration finished but no step met the SLA - capacity left unchanged"
            self.event("info", msg)
        except Exception as e:
            log.exception("analysis failed")
            self.event("warning", f"Could not analyse the run: {e}")
        run.processed = True
        self.broadcast({"type": "run_finished", "run_id": run.id})
        self.broadcast({"type": "state", "state": self.state()})

    def _finalize_dir(self, run_dir: Path, interrupted: bool) -> dict:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        if interrupted:
            meta["stopped_early"] = True
            meta.setdefault("ended", (run_dir / "samples.jsonl").stat().st_mtime if (run_dir / "samples.jsonl").exists() else meta["started"])
        samples = load_samples(run_dir)
        events = load_events(run_dir)
        cfg = meta.get("config") or self.cfg
        summary = analysis.analyse(samples, meta, cfg, events)
        if meta.get("kind") == "calibration":
            summary["calibration"] = analysis.calibration_result(summary, cfg)
        summary["interrupted"] = interrupted
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        try:
            summary["charts"] = charts.render(run_dir, meta, summary, samples)
        except Exception as e:  # charts are a bonus; never lose the report over them
            log.warning("could not draw charts: %s", e)
            summary["charts"] = []
        (run_dir / "report.html").write_text(report.render(meta, summary, samples, run_dir), encoding="utf-8")
        # summary.json last: its presence means the run is fully processed
        tmp = run_dir / "summary.json.tmp"
        tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        tmp.replace(run_dir / "summary.json")
        return summary

    async def stop_run(self, wait: bool = False):
        run = self.run
        if not run or run.finished:
            return
        run.stopping = True
        self.command({"cmd": "stop"})
        self.event("info", "Stopping test - users are logging out")
        limit = time.time() + float(self.cfg["load"]["stop_timeout_sec"]) + 30

        async def enforce():
            while time.time() < limit and run.proc and run.proc.poll() is None:
                await asyncio.sleep(0.5)
            if run.proc and run.proc.poll() is None:
                self.event("warning", "Load generator did not stop in time - forcing it")
                for c in psutil.Process(run.proc.pid).children(recursive=True):
                    c.kill()
                run.proc.kill()
        if wait:
            await enforce()
            while not run.finished:
                await asyncio.sleep(0.2)
        else:
            asyncio.ensure_future(enforce())

    def command(self, cmd: dict):
        if not self.run or self.run.finished:
            raise RuntimeError("no test is running")
        with open(self.run.dir / "commands.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(cmd) + "\n")
        if cmd.get("cmd") != "stop":
            if cmd["cmd"] == "set_users":
                text = "Following the profile again" if cmd.get("users") is None else f"Holding at {cmd['users']} users"
            else:
                text = "Skipped to the next phase"
            self.event("command", text)

    async def calibrate(self) -> dict:
        cap = self.capacity()
        c = self.cfg["calibration"]
        est = cap["auto_estimate"]
        steps = max(2, int(c["steps"]))
        lo, hi = float(c["start_fraction"]), float(c["end_fraction"])
        stages = []
        for i in range(steps):
            users = max(1, int(round(est * (lo + (hi - lo) * i / (steps - 1)))))
            stages.append({"phase": "steady", "duration": int(c["step_sec"]), "users": users, "spawn_rate": "auto",
                           "label": f"Step {i + 1}: {users} users"})
        stages.append({"phase": "cooldown", "duration": 10, "users": 0, "spawn_rate": "auto", "label": "Cooldown"})
        profile = {"name": "Capacity calibration", "description": "Steps users up until the SLA breaks.", "archetype": "calibration",
                   "mix": {"web": 50, "mobile": 35, "api": 15}, "stages": stages}
        ifs = config.load_interfaces()
        profile["mix"] = {k: v for k, v in profile["mix"].items() if k in ifs} or {next(iter(ifs)): 1}
        return await self.start_run("calibration", kind="calibration", profile=profile)

    # ── run history ────────────────────────────────────────────────────
    def list_runs(self) -> list[dict]:
        out = []
        for d in sorted(paths.runs_dir().glob("*"), reverse=True):
            mf = d / "meta.json"
            if not mf.exists():
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except ValueError:
                continue
            row = {"id": d.name, "kind": meta.get("kind"), "profile_id": meta.get("profile_id"),
                   "profile_name": meta["profile"]["name"], "started": meta.get("started"), "ended": meta.get("ended"),
                   "peak_users": max((s["users"] for s in meta.get("stages", [])), default=0),
                   "host": meta.get("system", {}).get("hostname"), "stopped_early": meta.get("stopped_early"),
                   "seed": meta.get("seed")}
            sf = d / "summary.json"
            if sf.exists():
                try:
                    s = json.loads(sf.read_text(encoding="utf-8"))
                    row.update(verdict=s["verdict"], p95_ms=s["overall"]["p95_ms"], error_rate_pct=s["overall"]["error_rate_pct"],
                               rps=s["overall"]["rps"], findings=len(s["findings"]))
                except Exception:
                    pass
            elif self.run and self.run.id == d.name and not self.run.finished:
                row["verdict"] = "RUNNING"
            out.append(row)
        return out

    def run_detail(self, run_id: str) -> dict:
        d = self._run_dir(run_id)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta.pop("config", None)
        summary = json.loads((d / "summary.json").read_text(encoding="utf-8")) if (d / "summary.json").exists() else None
        series = [compact(s) for s in load_samples(d)]
        return {"meta": meta, "summary": summary, "series": series}

    def _run_dir(self, run_id: str) -> Path:
        d = (paths.runs_dir() / run_id).resolve()
        if d.parent != paths.runs_dir().resolve() or not (d / "meta.json").exists():
            raise FileNotFoundError(run_id)
        return d

    def delete_run(self, run_id: str):
        if self.run and self.run.id == run_id and not self.run.finished:
            raise RuntimeError("cannot delete the run in progress")
        shutil.rmtree(self._run_dir(run_id))

    def samples_csv(self, run_id: str) -> str:
        rows = load_samples(self._run_dir(run_id))
        buf = io.StringIO()
        cols = ["t", "phase", "label", "users", "target_users", "rps", "fail_rps", "p50", "p95", "p99",
                "sys.cpu_pct", "sys.mem_pct", "sys.disk_read_mbs", "sys.disk_write_mbs", "sys.net_rx_mbs", "sys.net_tx_mbs",
                "target.cpu_pct", "target.rss_mb", "target.sessions", "target.up", "load.cpu_pct"]
        w = csv.writer(buf)
        w.writerow(cols)
        for s in rows:
            out = []
            for c in cols:
                v = s
                for k in c.split("."):
                    v = (v or {}).get(k) if isinstance(v, dict) else None
                out.append("" if v is None else v)
            w.writerow(out)
        return buf.getvalue()

    def logs(self, source: str, lines: int) -> list[str]:
        files = {"server": paths.logs_dir() / "target.log", "server_errors": paths.logs_dir() / "target.stderr.log",
                 "control": paths.logs_dir() / "control.log"}
        if source == "load" and self.run:
            return _tail(self.run.dir / "runner.log", lines) + _tail(self.run.dir / "runner.stderr.log", lines)
        if source == "events":
            return [json.dumps(e) for e in list(self.events)[-lines:]]
        f = files.get(source)
        return _tail(f, lines) if f else []


def load_samples(run_dir: Path) -> list[dict]:
    p = run_dir / "samples.jsonl"
    out = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def load_events(run_dir: Path) -> list[dict]:
    p = run_dir / "events.jsonl"
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _popen_flags() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
