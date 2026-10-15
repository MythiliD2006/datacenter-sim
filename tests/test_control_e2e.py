"""Starts the real control panel API, the built-in server and a short load test."""
import time

from fastapi.testclient import TestClient

from dcsim import config
from dcsim.control.server import create_app


def test_full_run_through_api(home):
    c = config.load_config()
    c["server"]["password_hash_rounds"] = 4
    c["server"]["endpoints"]["stream_file_mb"] = 1
    c["capacity"].update(mode="fixed", fixed_users=20)
    config.save_config(c)
    profile = {"name": "Tiny", "mix": {"web": 2, "api": 1, "batch": 1},
               "stages": [{"phase": "ramp_up", "duration": 4, "users": "50%"}, {"phase": "peak", "duration": 4, "users": "100%"},
                          {"phase": "steady", "duration": 3, "users": 4, "only": ["batch"]}, {"phase": "cooldown", "duration": 2, "users": 0}]}
    with TestClient(create_app()) as cl:
        assert cl.put("/api/profiles/tiny", json=profile).status_code == 200
        pv = cl.post("/api/preview", json=profile).json()
        assert pv["peak"] == 20 and pv["duration"] == 13
        st = cl.get("/api/state").json()
        assert st["target"]["state"] == "running" and st["target"]["accounts"] >= 20
        r = cl.post("/api/runs", json={"profile_id": "tiny"})
        assert r.status_code == 200, r.text
        assert cl.post("/api/runs", json={"profile_id": "tiny"}).status_code == 409   # one at a time
        time.sleep(5)
        assert cl.post("/api/runs/command", json={"cmd": "set_users", "users": 6}).json()["ok"]
        deadline = time.time() + 90
        runs = []
        while time.time() < deadline:
            runs = cl.get("/api/runs").json()
            if runs and runs[0].get("verdict") not in (None, "RUNNING"):
                break
            time.sleep(1)
        assert runs and runs[0]["verdict"] in ("PASS", "FAIL")
        d = cl.get(f"/api/runs/{runs[0]['id']}").json()
        assert d["summary"]["overall"]["requests"] > 20
        assert {"web", "api"} <= set(d["summary"]["overall"]["by_interface"])
        assert any(e["kind"] == "command" for e in d["summary"]["events"])
        assert "Users and throughput" in cl.get(f"/api/runs/{runs[0]['id']}/report").text
        assert cl.get(f"/api/runs/{runs[0]['id']}/samples.csv").text.startswith("t,phase")
        assert cl.put("/api/config", json={"sla": {"p95_ms": "x"}}).status_code == 400
        assert cl.get("/").status_code == 200 and cl.get("/ui/app.js").status_code == 200
