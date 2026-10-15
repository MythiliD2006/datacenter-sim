"""Seeds, per-interface curves, gRPC, storage I/O, page loads, worker computers, metrics."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from dcsim import config
from dcsim.load.controller import Controller
from dcsim.load.controller import user_rng
from dcsim.target.app import create_app


def test_apportion_is_exact():
    for total in (0, 1, 7, 99, 1000):
        out = config.apportion(total, {"a": 50, "b": 35, "c": 15})
        assert sum(out.values()) == total
    assert config.apportion(5, {"a": 1, "b": 0}) == {"a": 5, "b": 0}


def test_per_interface_users_and_stage_mix(home):
    p = config.validate_profile({"name": "x", "mix": {"web": 1, "api": 1, "batch": 1}, "stages": [
        {"phase": "peak", "duration": 10, "users": {"web": "50%", "api": 10}},
        {"phase": "steady", "duration": 10, "users": 100, "mix": {"web": 3, "api": 1}},
        {"phase": "steady", "duration": 10, "users": 30, "only": ["batch"]}]})
    st = config.resolve_profile(p, 200)
    assert st[0]["targets"] == {"web": 100, "api": 10, "batch": 0} and st[0]["users"] == 110
    assert st[1]["targets"] == {"web": 75, "api": 25, "batch": 0}
    assert st[2]["targets"] == {"web": 0, "api": 0, "batch": 30}
    with pytest.raises(config.ConfigError):
        config.validate_profile({"name": "x", "mix": {"web": 1}, "stages": [{"phase": "peak", "duration": 5, "users": {"api": 3}}]})


def test_controller_moves_each_interface_toward_its_own_target():
    plan = {"mix": {"web": 1, "api": 1}, "stages": [
        {"index": 0, "start": 0, "end": 10, "users": 20, "targets": {"web": 15, "api": 5}, "auto_rate": True, "spawn_rate": 1},
        {"index": 1, "start": 10, "end": 20, "users": 20, "targets": {"web": 2, "api": 18}, "auto_rate": True, "spawn_rate": 1}]}
    applied = []
    c = Controller(plan, lambda t: applied.append(t), lambda: applied[-1] if applied else {})
    c.started = time.time()
    for _ in range(40):
        c.tick(0.25)
    assert applied[-1] == {"web": 15, "api": 5}
    c.offset = 10                       # jump into stage 2
    for _ in range(80):
        c.tick(0.25)
    assert applied[-1] == {"web": 2, "api": 18}
    c.command({"cmd": "set_users", "users": 10})
    for _ in range(40):
        c.tick(0.25)
    assert sum(applied[-1].values()) == 10


def test_same_seed_same_user_behaviour():
    a, b, c = user_rng(42, 7, "web"), user_rng(42, 7, "web"), user_rng(43, 7, "web")
    seq_a = [a.random() for _ in range(20)]
    assert seq_a == [b.random() for _ in range(20)]
    assert seq_a != [c.random() for _ in range(20)]
    assert user_rng(42, 7, "web").random() != user_rng(42, 8, "web").random()


def test_seed_validation(home):
    p = config.validate_profile({"name": "x", "mix": {"web": 1}, "seed": "12", "stages": [{"phase": "peak", "duration": 5, "users": 1}]})
    assert p["seed"] == 12
    with pytest.raises(config.ConfigError):
        config.set_value("load.seed", "abc")
    assert config.set_value("load.seed", "")["load"]["seed"] is None


def test_env_overrides(home, monkeypatch):
    monkeypatch.setenv("DCSIM__TARGET__PORT", "8000")
    monkeypatch.setenv("DCSIM__SLA__P95_MS", "250")
    monkeypatch.setenv("DCSIM__CONTROL_PANEL__OPEN_BROWSER", "false")
    c = config.load_config()
    assert c["target"]["port"] == 8000 and c["sla"]["p95_ms"] == 250 and c["control_panel"]["open_browser"] is False


def test_grpc_and_asset_task_validation(home):
    ifs = config.load_interfaces()
    assert ifs["grpc_telemetry"]["tasks"][0]["protocol"] == "grpc"
    assert config.validate_interface("x", {"tasks": [{"protocol": "grpc", "path": "/a.B/C"}]})["tasks"][0]["protocol"] == "grpc"
    with pytest.raises(config.ConfigError, match="package.Service"):
        config.validate_interface("x", {"tasks": [{"protocol": "grpc", "path": "/nope"}]})
    t = config.validate_interface("x", {"tasks": [{"path": "/web/home", "assets": "/static/a.js, /static/b.css"}]})["tasks"][0]
    assert t["assets"] == ["/static/a.js", "/static/b.css"]


def _target(cfg, home, grpc_port=None):
    cfg["server"]["endpoints"].update(stream_file_mb=1, storage_file_mb=2)
    return TestClient(create_app(cfg, 10, data_dir=home / "scratch" / "t", log_file=home / "logs" / "t.log", grpc_port=grpc_port))


def test_page_load_and_random_storage(cfg, home):
    with _target(cfg, home) as c:
        h = {"Authorization": "Bearer " + c.post("/login", json={"username": "user_1", "password": "pass"}).json()["token"]}
        assert len(c.get("/web/home", headers=h).text) >= cfg["server"]["endpoints"]["page_kb"] * 1024
        a = c.get("/static/app.js")
        assert a.status_code == 200 and a.headers["content-type"].startswith("application/javascript")
        r = c.get("/storage/random", headers=h).json()
        assert r["reads"] > 0 and r["writes"] == 0
        w = c.post("/storage/random", headers=h, params={"write_ratio": 1}).json()
        assert w["writes"] > 0
        st = c.get("/stats").json()
        assert st["random_reads"] >= r["reads"] and st["random_writes"] == w["writes"]
        assert c.post("/admin/seed", json={"seed": 5}).json()["seeded"]


def test_grpc_services(cfg, home):
    import grpc

    from dcsim.system import free_port
    port = free_port("127.0.0.1", 0)
    with _target(cfg, home, grpc_port=port) as c:
        tok = c.post("/login", json={"username": "user_2", "password": "pass"}).json()["token"]
        ch = grpc.insecure_channel(f"127.0.0.1:{port}")
        call = lambda m: ch.unary_unary(m, request_serializer=lambda d: json.dumps(d).encode(), response_deserializer=json.loads)
        md = (("authorization", "Bearer " + tok),)
        assert call("/dcsim.Telemetry/GetData")({}, metadata=md, timeout=10)["count"] > 0
        assert call("/dcsim.Telemetry/Ingest")({"readings": 4}, metadata=md, timeout=10)["accepted"] == 4
        assert call("/dcsim.Payments/Transfer")({"amount": 3}, metadata=md, timeout=10)["status"] == "accepted"
        with pytest.raises(grpc.RpcError) as e:
            call("/dcsim.Telemetry/GetData")({}, timeout=10)
        assert e.value.code() == grpc.StatusCode.UNAUTHENTICATED
        ch.close()
        assert b"grpc_requests_total" in c.get("/metrics").content


def test_worker_computers_and_metrics(home):
    from dcsim.control.server import create_app as panel
    c = config.load_config()
    c["target"]["auto_start"] = False
    config.save_config(c)
    with TestClient(panel()) as cl:
        base = cl.get("/api/state").json()["capacity"]["users"]
        r = cl.post("/api/agents/register", json={"id": "box-1", "hostname": "box", "capacity": 300, "workers": 3})
        assert r.json()["ok"]
        st = cl.get("/api/state").json()
        assert st["capacity"]["users"] == base + 300 and st["agents"][0]["hostname"] == "box"
        assert cl.get("/api/agents/poll", params={"id": "box-1"}).json() == {"registered": True, "assignment": None, "running": False}
        assert cl.get("/api/agents/poll", params={"id": "nobody"}).json() == {"registered": False}
        m = cl.get("/metrics").text
        assert "dcsim_capacity_users" in m and "dcsim_worker_machines 1.0" in m
