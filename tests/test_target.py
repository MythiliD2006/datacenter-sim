from fastapi.testclient import TestClient

from dcsim.target.app import create_app


def client(cfg, home, accounts=20):
    app = create_app(cfg, accounts, data_dir=home / "scratch" / "t", log_file=home / "logs" / "t.log")
    return TestClient(app)


def login(c, n=0, pw="pass"):
    return c.post("/login", json={"username": f"user_{n}", "password": pw})


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_login_logout_and_sessions(cfg, home):
    with client(cfg, home) as c:
        r = login(c)
        assert r.status_code == 200
        tok = r.json()["token"]
        assert c.get("/health").json()["active_sessions"] == 1
        assert c.get("/me", headers=hdr(tok)).json()["username"] == "user_0"
        assert c.post("/logout", headers=hdr(tok)).status_code == 200
        assert c.get("/me", headers=hdr(tok)).status_code == 401


def test_account_count_comes_from_launch_value(cfg, home):
    with client(cfg, home, accounts=5) as c:
        assert login(c, 4).status_code == 200
        assert login(c, 5).status_code == 401
        assert c.get("/stats").json()["accounts"] == 5


def test_bad_password_and_missing_token(cfg, home):
    with client(cfg, home) as c:
        assert login(c, pw="nope").status_code == 401
        assert c.get("/me").status_code == 401


def test_rate_limit_is_per_username_not_per_ip(cfg, home):
    cfg["server"]["rate_limit"]["max_attempts"] = 3
    with client(cfg, home) as c:
        for _ in range(3):
            assert login(c, 1).status_code == 200
        assert login(c, 1).status_code == 429
        # a different user from the same IP is not blocked
        assert login(c, 2).status_code == 200


def test_rate_limit_can_be_disabled(cfg, home):
    cfg["server"]["rate_limit"].update(max_attempts=1, enabled=False)
    with client(cfg, home) as c:
        assert all(login(c, 1).status_code == 200 for _ in range(4))


def test_api_data_count_within_configured_range(cfg, home):
    cfg["server"]["endpoints"]["api_data_records"] = [7, 9]
    with client(cfg, home) as c:
        tok = login(c).json()["token"]
        n = c.get("/api/data", headers=hdr(tok)).json()["count"]
        assert 7 <= n <= 9


def test_workload_endpoints(cfg, home):
    cfg["server"]["endpoints"]["stream_file_mb"] = 1
    with client(cfg, home) as c:
        h = hdr(login(c).json()["token"])
        assert c.get("/catalog", headers=h).status_code == 200
        assert c.post("/cart/checkout", headers=h, json={"items": 2}).json()["items"] == 2
        assert c.post("/inventory/update", headers=h, json={"delta": -1}).status_code == 200
        assert c.post("/transactions", headers=h, json={"amount": 5}).json()["status"] == "accepted"
        assert c.get("/transactions/history", headers=h).json()["count"] == 1
        assert c.post("/batch/upload", headers=h, content=b"x" * 2048).json()["stored_bytes"] == 2048
        assert len(c.get("/media/stream", headers=h, params={"size_kb": 64}).content) == 64 * 1024
        assert c.get("/recommendations", headers=h).status_code == 200
        assert c.post("/posts", headers=h, json={"text": "hi"}).status_code == 200
        assert c.get("/feed", headers=h).json()["count"] == 1
        assert c.post("/iot/ingest", headers=h, json={"readings": 3}).json()["accepted"] == 3
        assert c.get("/analytics/aggregate", headers=h).status_code == 200
        assert b"http_requests_total" in c.get("/metrics").content


def test_upload_limit(cfg, home):
    cfg["server"]["endpoints"]["upload_max_kb"] = 1
    with client(cfg, home) as c:
        h = hdr(login(c).json()["token"])
        assert c.post("/batch/upload", headers=h, content=b"x" * 4096).status_code == 413
