"""Load-generator process.

Started by the control panel as ``python -m dcsim.load.runner --plan plan.json``.

Each stage of a plan says how many users every interface should have
(``targets``). A controller moves the running users toward those targets at
the stage's spawn rate, per interface, so web, mobile, API, batch and gRPC
clients can each follow their own curve.

Output: one JSON line per sample interval on stdout (users, phase, latency
histograms per interface and endpoint, errors). Input: live commands (hold at
N users, skip stage, stop) appended to ``commands.jsonl`` in the run folder.

With more than one worker this process is a Locust master. Workers are local
processes it starts itself, plus worker agents on other machines
(``dcsim worker``) that the control panel tells to join. Users are split
between workers in proportion to each worker's capacity.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict

try:  # gRPC needs to cooperate with gevent; must run before any channel exists
    import grpc.experimental.gevent as _grpc_gevent
    _grpc_gevent.init_gevent()
except Exception:  # grpc not installed - gRPC tasks will report an error
    pass

import gevent
from locust import events
from locust.env import Environment
from locust.user.task import LOCUST_STATE_STOPPING

from ..config import apportion
from .controller import Controller
from .users import build_user_classes

log = logging.getLogger("dcsim.runner")


def bucket_ms(ms: float) -> int:
    """Round latencies the same way in every process so histograms merge."""
    if ms < 100:
        return int(round(ms))
    if ms < 1000:
        return int(round(ms, -1))
    return int(round(ms, -2))


class Collector:
    def __init__(self):
        self.reset()

    def reset(self):
        self.count = 0
        self.fails = 0
        self.bytes = 0
        self.hist: Counter = Counter()
        self.iface: dict = defaultdict(lambda: {"count": 0, "fails": 0, "hist": Counter()})
        self.endpoint: dict = defaultdict(lambda: {"count": 0, "fails": 0, "hist": Counter()})
        self.errors: Counter = Counter()

    def add(self, iface, name, ms, length, failed, err):
        b = bucket_ms(ms)
        self.count += 1
        self.bytes += int(length or 0)
        self.hist[b] += 1
        for d, key in ((self.iface, iface), (self.endpoint, name)):
            e = d[key]
            e["count"] += 1
            e["hist"][b] += 1
            if failed:
                e["fails"] += 1
        if failed:
            self.fails += 1
            self.errors[f"{name}: {str(err)[:120]}"] += 1

    def dump(self) -> dict:
        def h(c):
            return {str(k): v for k, v in c.items()}
        return {"count": self.count, "fails": self.fails, "bytes": self.bytes, "hist": h(self.hist),
                "iface": {k: {"count": v["count"], "fails": v["fails"], "hist": h(v["hist"])} for k, v in self.iface.items()},
                "endpoint": {k: {"count": v["count"], "fails": v["fails"], "hist": h(v["hist"])} for k, v in self.endpoint.items()},
                "errors": dict(self.errors)}

    def merge(self, d: dict):
        self.count += d["count"]
        self.fails += d["fails"]
        self.bytes += d.get("bytes", 0)
        for k, v in d["hist"].items():
            self.hist[int(k)] += v
        for src, dst in ((d["iface"], self.iface), (d["endpoint"], self.endpoint)):
            for key, v in src.items():
                e = dst[key]
                e["count"] += v["count"]
                e["fails"] += v["fails"]
                for k, n in v["hist"].items():
                    e["hist"][int(k)] += n
        for k, v in d["errors"].items():
            self.errors[k] += v


# ── local user management (used by the single process and by each worker) ──

class LocalUsers:
    """Starts and stops users of each interface inside this process."""

    def __init__(self, runner, classes: dict):
        self.runner = runner
        self.classes = classes            # iface -> class
        self.names = {c.__name__: i for i, c in classes.items()}

    def counts(self) -> dict:
        out = {i: 0 for i in self.classes}
        for g in list(self.runner.user_greenlets):
            try:
                u = g.args[0]
            except (IndexError, AttributeError):
                continue
            if u._state == LOCUST_STATE_STOPPING:
                continue
            i = self.names.get(type(u).__name__)
            if i:
                out[i] += 1
        return out

    def set_targets(self, targets: dict):
        cur = self.counts()
        spawn = {self.classes[i].__name__: n - cur.get(i, 0) for i, n in targets.items() if n > cur.get(i, 0)}
        if spawn:
            self.runner.spawn_users(spawn)
        for i, n in targets.items():
            extra = cur.get(i, 0) - n
            if extra <= 0:
                continue
            cls = self.classes[i]
            victims = []
            for g in reversed(list(self.runner.user_greenlets)):   # newest first
                try:
                    u = g.args[0]
                except (IndexError, AttributeError):
                    continue
                if type(u) is cls and u._state != LOCUST_STATE_STOPPING:
                    victims.append(u)
                    if len(victims) == extra:
                        break
            for u in victims:
                gevent.spawn(_safe_stop, u)

    def total(self) -> int:
        return sum(self.counts().values())


def _safe_stop(user):
    try:
        user.stop(force=False)
    except Exception:
        pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_commands(path: str, pos: int) -> tuple[list, int]:
    if not os.path.exists(path):
        return [], pos
    with open(path, encoding="utf-8") as f:
        f.seek(pos)
        lines = f.readlines()
        pos = f.tell()
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out, pos


def _seed(plan: dict, worker_index: int):
    seed = plan.get("seed")
    if seed is not None:
        random.seed(int(seed) * 1000 + worker_index)


def run_master_or_local(plan: dict):
    local_workers = int(plan.get("workers", 1))
    remote = list(plan.get("remote_workers") or [])
    distributed = local_workers > 1 or bool(remote)
    collector = Collector()
    _seed(plan, 0)
    classes = build_user_classes(plan, 0, 1)
    env = Environment(user_classes=list(classes.values()), events=events, stop_timeout=float(plan.get("stop_timeout", 15)))
    procs = []
    reported: dict[str, dict] = {}         # worker node id -> {iface: count}
    capacities: dict[str, float] = {}       # worker node id -> capacity weight
    last_sent: dict[str, dict] = {}

    if distributed:
        bind = plan.get("master_bind", "127.0.0.1")
        runner = env.create_master_runner(bind, int(plan["master_port"]))

        def on_bucket(msg, **_):
            data = msg.data
            collector.merge(data["stats"])
            reported[msg.node_id] = data.get("counts", {})

        def on_hello(msg, **_):
            capacities[msg.node_id] = float(msg.data.get("capacity") or 1)

        runner.register_message("dcsim_bucket", on_bucket)
        runner.register_message("dcsim_hello", on_hello)
        here = os.path.dirname(plan["plan_file"])
        total_workers = local_workers + sum(int(r.get("workers", 1)) for r in remote)
        per_local = float(plan.get("local_capacity", 1)) / max(1, local_workers)
        for i in range(local_workers):
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "dcsim.load.runner", "--plan", plan["plan_file"], "--mode", "worker",
                 "--master-host", "127.0.0.1", "--master-port", str(plan["master_port"]),
                 "--worker-index", str(i), "--workers", str(total_workers), "--capacity", str(per_local),
                 "--parent-pid", str(os.getpid())],
                stdout=subprocess.DEVNULL, stderr=open(os.path.join(here, f"worker{i}.log"), "w"), cwd=here))
        deadline = time.time() + float(plan.get("worker_wait_sec", 60))
        while (len(runner.clients.ready) < total_workers or len(capacities) < len(runner.clients.ready)) and time.time() < deadline:
            gevent.sleep(0.5)
        emit({"type": "event", "msg": f"{len(runner.clients.ready)} of {total_workers} load workers connected"
              + (f" ({len(remote)} remote machine(s))" if remote else "")})

        def apply(targets: dict):
            nodes = sorted(c.id for c in runner.clients.ready)
            if not nodes:
                return
            weights = {n: capacities.get(n, 1.0) for n in nodes}
            per_node = {n: {} for n in nodes}
            for iface, count in targets.items():
                for n, k in apportion(count, weights).items():
                    per_node[n][iface] = k
            for n, t in per_node.items():
                if last_sent.get(n) != t:
                    runner.send_message("dcsim_target", t, client_id=n)
                    last_sent[n] = t

        def by_worker():
            names = {}
            for n, c in sorted(reported.items()):
                host, _, rest = n.partition("_")
                names[f"{host}-{rest[:6]}" if rest else n] = sum(c.values())
            return names

        def counts():
            out = defaultdict(int)
            for c in reported.values():
                for k, v in c.items():
                    out[k] += v
            return dict(out)
    else:
        runner = env.create_local_runner()
        local = LocalUsers(runner, classes)

        @events.request.add_listener
        def on_request(request_type, name, response_time, response_length, exception=None, context=None, **_):
            collector.add((context or {}).get("iface", "?"), name, response_time or 0, response_length, exception is not None, exception)

        apply, counts = local.set_targets, local.counts

        def by_worker():
            return {}

    ctl = Controller(plan, apply, counts)
    cmd_file = os.path.join(os.path.dirname(plan["plan_file"]), "commands.jsonl")
    cmd_pos = 0
    interval = float(plan.get("sample_interval", 1.0))
    step = 0.25
    emit({"type": "started", "workers": len(runner.clients.ready) if distributed else 1, "total_duration": ctl.total,
          "seed": plan.get("seed")})
    ctl.started = time.time()
    last_emit = last_tick = time.time()
    while True:
        gevent.sleep(step)
        now = time.time()
        alive = ctl.tick(now - last_tick)
        last_tick = now
        cmds, cmd_pos = read_commands(cmd_file, cmd_pos)
        for c in cmds:
            ctl.command(c)
        if now - last_emit >= interval or not alive:
            dt, last_emit = now - last_emit, now
            data = collector.dump()
            collector.reset()
            by_iface = counts()
            s = ctl.stages[ctl.stage_idx] if ctl.stages else {}
            emit({"type": "tick", "t": round(now - ctl.started, 2), "run_time": round(min(ctl.run_time(), ctl.total), 2),
                  "users": sum(by_iface.values()), "users_by_iface": by_iface,
                  "target_users": ctl.override if ctl.override is not None else s.get("users"),
                  "targets_by_iface": ctl.desired(s) if s else {}, "stage": ctl.stage_idx, "phase": s.get("phase"),
                  "label": s.get("label"), "override": ctl.override, "users_by_worker": by_worker(), **data})
        if not alive:
            break
    # everyone logs out, then flush the last numbers
    ctl.drain()
    deadline = time.time() + float(plan.get("stop_timeout", 15)) + 5
    while time.time() < deadline and sum(counts().values()) > 0:
        gevent.sleep(0.5)
    gevent.sleep(1.5 if distributed else 0.5)
    data = collector.dump()
    emit({"type": "tick", "t": round(time.time() - ctl.started, 2), "run_time": None, "users": 0, "users_by_iface": {},
          "target_users": 0, "stage": ctl.stage_idx, "phase": "cooldown", "label": "stopped", "final": True, **data})
    try:
        runner.quit()
    except Exception:
        pass
    for p in procs:
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
    emit({"type": "done", "stopped_early": ctl.stop})


def run_worker(plan: dict, master_host: str, master_port: int, worker_index: int, workers: int, capacity: float):
    collector = Collector()
    _seed(plan, worker_index)
    classes = build_user_classes(plan, worker_index, workers)
    env = Environment(user_classes=list(classes.values()), events=events, stop_timeout=float(plan.get("stop_timeout", 15)))
    runner = env.create_worker_runner(master_host, master_port)
    local = LocalUsers(runner, classes)

    @events.request.add_listener
    def on_request(request_type, name, response_time, response_length, exception=None, context=None, **_):
        collector.add((context or {}).get("iface", "?"), name, response_time or 0, response_length, exception is not None, exception)

    def on_target(msg, **_):
        local.set_targets({k: int(v) for k, v in msg.data.items()})
    runner.register_message("dcsim_target", on_target)

    def flusher():
        gevent.sleep(0.3)
        runner.send_message("dcsim_hello", {"index": worker_index, "capacity": capacity})
        while True:
            gevent.sleep(1.0)
            data = collector.dump()
            collector.reset()
            try:
                runner.send_message("dcsim_bucket", {"stats": data, "counts": local.counts()})
            except Exception:
                pass
    gevent.spawn(flusher)
    runner.greenlet.join()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--mode", default="auto", choices=["auto", "worker"])
    ap.add_argument("--master-host", default="127.0.0.1")
    ap.add_argument("--master-port", type=int, default=0)
    ap.add_argument("--worker-index", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--capacity", type=float, default=1.0)
    ap.add_argument("--parent-pid", type=int, default=0)
    a = ap.parse_args()
    if a.parent_pid:
        from ..lifeline import watch_parent
        watch_parent(a.parent_pid)
    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    plan["plan_file"] = os.path.abspath(a.plan)
    if plan.get("target_url_override"):
        plan["target_url"] = plan["target_url_override"]
    log_name = "runner.log" if a.mode != "worker" else f"worker{a.worker_index}-locust.log"
    logging.basicConfig(filename=os.path.join(os.path.dirname(plan["plan_file"]), log_name), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    if a.mode == "worker":
        run_worker(plan, a.master_host, a.master_port, a.worker_index, a.workers, a.capacity)
    else:
        run_master_or_local(plan)


if __name__ == "__main__":
    main()
