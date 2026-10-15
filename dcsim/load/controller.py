"""Plan logic with no Locust import, so it can be tested and reused anywhere.

Controller moves running users toward each stage's per-interface targets at the
stage's spawn rate; user_rng gives every simulated user its own seeded random
generator so runs with the same seed repeat.
"""
from __future__ import annotations

import random
import time

from ..config import apportion


def user_rng(seed: int | None, account: int, iface: str) -> random.Random:
    """Each simulated user's own random generator: same seed + account + interface = same choices."""
    if seed is None:
        return random.Random()
    return random.Random(int(seed) * 1_000_003 + account * 7919 + sum(map(ord, iface)))



class Controller:
    def __init__(self, plan: dict, apply_fn, counts_fn):
        self.plan = plan
        self.stages = plan["stages"]
        self.total = self.stages[-1]["end"] if self.stages else 0
        self.apply = apply_fn            # dict iface->count  (the whole cluster)
        self.counts = counts_fn          # actual running users per iface
        self.ifaces = list(plan["mix"])
        self.intended = {i: 0 for i in self.ifaces}
        self.started = None
        self.offset = 0.0
        self.override = None
        self.stop = False
        self.stage_idx = 0
        self._rate_key = None
        self._rate = 1.0
        self._budget = 0.0

    def run_time(self) -> float:
        return (time.time() - self.started) + self.offset if self.started else 0.0

    def current_stage(self):
        t = self.run_time()
        for s in self.stages:
            if t < s["end"]:
                return s
        return None

    def desired(self, s) -> dict:
        if self.override is not None:
            weights = s.get("weights") or self.plan["mix"]
            return {i: 0 for i in self.ifaces} | apportion(self.override, weights)
        return {i: 0 for i in self.ifaces} | dict(s["targets"])

    def tick(self, dt: float) -> bool:
        """Move users toward the targets. Returns False when the plan is over."""
        if self.stop:
            return False
        s = self.current_stage()
        if s is None:
            return False
        self.stage_idx = s["index"]
        want = self.desired(s)
        key = (s["index"], self.override)
        if key != self._rate_key:
            delta = sum(abs(want[i] - self.intended.get(i, 0)) for i in want)
            if s.get("auto_rate", True) or self.override is not None:
                remaining = max(1.0, s["end"] - self.run_time())
                self._rate = max(1.0, delta / remaining) if delta else 1.0
                if self.override is not None:
                    self._rate = max(self._rate, 5.0)
            else:
                self._rate = float(s["spawn_rate"])
            self._rate_key, self._budget = key, 0.0
        self._budget += self._rate * dt
        deltas = {i: want[i] - self.intended.get(i, 0) for i in want}
        need = sum(abs(d) for d in deltas.values())
        if need == 0:
            self._budget = 0.0
            return True
        moves = int(self._budget)
        if moves <= 0:
            return True
        moves = min(moves, need)
        self._budget -= moves
        # share the moves between interfaces in proportion to how far each is from its target
        share = apportion(moves, {i: abs(d) for i, d in deltas.items() if d})
        changed = False
        for i, n in share.items():
            if n:
                step = min(n, abs(deltas[i]))
                self.intended[i] += step if deltas[i] > 0 else -step
                changed = True
        if changed:
            self.apply(dict(self.intended))
        return True

    def drain(self):
        self.intended = {i: 0 for i in self.ifaces}
        self.apply(dict(self.intended))

    def command(self, c: dict):
        kind = c.get("cmd")
        if kind == "stop":
            self.stop = True
        elif kind == "set_users":
            self.override = None if c.get("users") in (None, "") else max(0, int(c["users"]))
        elif kind == "next_stage":
            s = self.current_stage()
            if s:
                self.offset += s["end"] - self.run_time() + 0.01
                self.override = None


