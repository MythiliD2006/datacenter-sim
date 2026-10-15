"""Builds Locust user classes from the interface library and a profile's mix.

Each simulated user has its own random generator seeded from the run seed and
its account number, so with the same seed every user makes the same choices
(which action, how long to think, heavy or light, session length) run after run.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import time
from urllib.parse import urlencode

from locust import FastHttpUser, task

from .controller import user_rng

_grpc_channels: dict = {}


def _grpc_channel(target: str):
    ch = _grpc_channels.get(target)
    if ch is None:
        import grpc
        ch = grpc.insecure_channel(target)
        _grpc_channels[target] = ch
    return ch


def _account_allocator(worker_index: int, workers: int, accounts: int):
    counter = itertools.count()

    def next_account() -> int:
        return (worker_index + workers * next(counter)) % max(1, accounts)

    return next_account


def build_user_classes(plan: dict, worker_index: int = 0, workers: int = 1) -> dict[str, type]:
    auth = plan["auth"]
    accounts = int(plan["accounts"])
    next_account = _account_allocator(worker_index, workers, accounts)
    login_path, logout_path = auth["login_path"], auth["logout_path"]
    token_field, header, prefix = auth["token_field"], auth["token_header"], auth["token_prefix"]
    seed = plan.get("seed")
    grpc_target = plan.get("grpc_target")
    payload_cache: dict[int, bytes] = {}

    def payload(kb: int) -> bytes:
        if kb not in payload_cache:
            payload_cache[kb] = os.urandom(kb * 1024)
        return payload_cache[kb]

    classes = {}
    for iface, weight in plan["mix"].items():
        spec = plan["interfaces"][iface]
        wait_lo, wait_hi = spec["wait_sec"]
        lifetime = spec.get("session_lifetime_sec")
        task_specs = spec["tasks"]
        task_weights = [float(t["weight"]) for t in task_specs]

        def on_start(self, _spec=spec, _iface=iface):
            self.account = next_account()
            self.rng = user_rng(seed, self.account, _iface)
            self.heavy = self.rng.random() < _spec.get("heavy_user_fraction", 0)
            self.wait_mult = _spec.get("heavy_wait_multiplier", 1) if self.heavy else 1
            self.token = None
            self.next_login_try = 0.0
            self.session_ends = None
            if _spec.get("login", True):
                self._login()

        def _login(self, _iface=iface, _lifetime=lifetime):
            username = auth["account_template"].format(n=self.account, interface=_iface)
            ctx = {"iface": _iface}
            with self.client.request("POST", login_path, name=f"POST {login_path}", context=ctx, catch_response=True,
                                     json={auth["username_field"]: username, auth["password_field"]: auth["password"]}) as r:
                tok = None
                if r.status_code == 200:
                    try:
                        tok = r.json().get(token_field)
                    except Exception:
                        tok = None
                if tok:
                    r.success()
                    self.token = tok
                    self.session_ends = time.time() + self.rng.uniform(*_lifetime) if _lifetime else None
                else:
                    r.failure(f"login failed: HTTP {r.status_code}")
                    self.token = None
                    self.next_login_try = time.time() + self.rng.uniform(2, 6)

        def _logout(self, _iface=iface):
            if self.token:
                self.client.request("POST", logout_path, name=f"POST {logout_path}", context={"iface": _iface},
                                    headers={header: prefix + self.token})
                self.token = None

        def on_stop(self):
            self._logout()

        def _http(self, t, _iface=iface):
            name = f"{t['method']} {t['path']}"
            url = t["path"] + ("?" + urlencode(t["params"]) if t.get("params") else "")
            kw = {"name": name, "context": {"iface": _iface}}
            hdrs = {}
            if t.get("auth", True) and self.token:
                hdrs[header] = prefix + self.token
            if hdrs:
                kw["headers"] = hdrs
            if t.get("json") is not None:
                kw["json"] = t["json"]
            if t.get("body_kb"):
                kw["data"] = payload(int(t["body_kb"]))
            with self.client.request(t["method"], url, catch_response=True, **kw) as r:
                if r.status_code == 401 and t.get("auth", True):
                    self.token = None           # the server expired the session
                    r.failure("session rejected (401)")
                elif r.status_code and r.status_code < 400:
                    r.success()
                else:
                    r.failure(f"HTTP {r.status_code}")
            # a browser page load: fetch the page's assets after the page itself
            for asset in t.get("assets") or []:
                with self.client.request("GET", asset, name="GET /static/*", context={"iface": _iface}, catch_response=True,
                                         headers=hdrs or None) as r:
                    if r.status_code and r.status_code < 400:
                        r.success()
                    else:
                        r.failure(f"HTTP {r.status_code}")

        def _grpc(self, t, _iface=iface):
            name = f"GRPC {t['path'].lstrip('/')}"
            start = time.perf_counter()
            exc, length = None, 0
            try:
                if not grpc_target:
                    raise RuntimeError("no gRPC server configured (target.external_grpc or the built-in server)")
                call = _grpc_channel(grpc_target).unary_unary(
                    t["path"], request_serializer=lambda d: json.dumps(d).encode(), response_deserializer=lambda b: b)
                md = (("authorization", prefix + self.token),) if (t.get("auth", True) and self.token) else ()
                resp = call(t.get("json") or {}, metadata=md, timeout=30)
                length = len(resp)
            except Exception as e:  # grpc.RpcError carries the status code
                code = getattr(e, "code", None)
                exc = RuntimeError(f"gRPC {code().name}" if callable(code) else str(e)[:120])
                if callable(code) and code().name == "UNAUTHENTICATED":
                    self.token = None
            self.environment.events.request.fire(
                request_type="GRPC", name=name, response_time=(time.perf_counter() - start) * 1000,
                response_length=length, exception=exc, context={"iface": _iface}, response=None, url=t["path"])

        @task
        def act(self, _spec=spec, _tasks=task_specs, _w=task_weights):
            if _spec.get("login", True):
                now = time.time()
                if self.token and self.session_ends and now >= self.session_ends:
                    self._logout()          # session over: log out, then back in
                    self._login()
                if not self.token:
                    if now >= self.next_login_try:
                        self._login()
                    if not self.token:
                        return
            t = self.rng.choices(_tasks, weights=_w, k=1)[0]
            if t.get("protocol") == "grpc":
                self._grpc(t)
            else:
                self._http(t)

        def wait_time(self, _lo=wait_lo, _hi=wait_hi):
            rng = getattr(self, "rng", random)
            return rng.uniform(_lo, _hi) * getattr(self, "wait_mult", 1)

        cls = type(f"DC_{iface}", (FastHttpUser,), {
            "host": plan["target_url"], "weight": max(1, int(round(weight * 10))), "wait_time": wait_time,
            "tasks": [act], "on_start": on_start, "on_stop": on_stop, "_login": _login, "_logout": _logout,
            "_http": _http, "_grpc": _grpc, "dcsim_interface": iface, "network_timeout": 30.0, "connection_timeout": 15.0,
        })
        classes[iface] = cls
    return classes
