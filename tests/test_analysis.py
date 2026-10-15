from dcsim import config
from dcsim.control import analysis


def sample(t, stage, users, count, fails=0, ms=50, rss=100, sessions=None, iface="web"):
    return {"t": t, "dt": 1.0, "stage": stage, "users": users, "count": count, "fails": fails,
            "hist": {str(ms): count}, "iface": {iface: {"count": count, "fails": fails, "hist": {str(ms): count}}},
            "endpoint": {"GET /me": {"count": count, "fails": fails, "hist": {str(ms): count}}},
            "errors": {"GET /me: HTTP 500": fails} if fails else {},
            "sys": {"cpu_pct": 30, "mem_pct": 40}, "target": {"cpu_pct": 20, "rss_mb": rss, "sessions": sessions if sessions is not None else users}}


META = {"stages": [{"index": 0, "phase": "ramp_up", "label": "Ramp", "users": 10, "start": 0, "end": 30},
                   {"index": 1, "phase": "peak", "label": "Peak", "users": 10, "start": 30, "end": 60},
                   {"index": 2, "phase": "cooldown", "label": "Cool", "users": 0, "start": 60, "end": 90}]}


def test_percentiles():
    from collections import Counter
    h = Counter({10: 90, 1000: 10})
    assert analysis.pct(h, 0.5) == 10 and analysis.pct(h, 0.95) == 1000


def test_clean_run_passes(home):
    cfg = config.load_config()
    s = [sample(t, t // 30, 10, 20) for t in range(90)]
    r = analysis.analyse(s, META, cfg)
    assert r["verdict"] == "PASS" and len(r["phases"]) == 3 and not r["findings"]


def test_flags_transition_errors_leak_and_sessions(home):
    cfg = config.load_config()
    s = []
    for t in range(90):
        st = t // 30
        slow = st == 1 and t < 40          # slow at the start of peak
        s.append(sample(t, st, 0 if st == 2 else 10, 20, fails=5 if st == 1 else 0, ms=900 if slow else 40,
                        rss=100 + t * 2, sessions=50 if st == 2 else None))
    r = analysis.analyse(s, META, cfg)
    titles = " | ".join(f["title"] for f in r["findings"])
    assert r["verdict"] == "FAIL"
    assert "Latency jump entering 'Peak'" in titles
    assert "Errors during 'Peak'" in titles
    assert "memory grew" in titles
    assert "Sessions left open" in titles


def test_crash_event_fails_run(home):
    cfg = config.load_config()
    s = [sample(t, t // 30, 10, 20) for t in range(90)]
    r = analysis.analyse(s, META, cfg, [{"kind": "crash", "msg": "x", "t": 50}])
    assert r["verdict"] == "FAIL" and r["findings"][0]["severity"] == "critical"


def test_calibration_picks_last_good_step(home):
    cfg = config.load_config()
    meta = {"stages": [{"index": i, "phase": "steady", "label": f"s{i}", "users": u, "start": i * 10, "end": i * 10 + 10}
                       for i, u in enumerate([20, 40, 60])]}
    s = [sample(t, t // 10, [20, 40, 60][t // 10], 10, ms=[50, 200, 2000][t // 10]) for t in range(30)]
    r = analysis.analyse(s, meta, cfg)
    assert analysis.calibration_result(r, cfg)["measured_users"] == 40


def test_compare(home):
    cfg = config.load_config()
    a = analysis.analyse([sample(t, t // 30, 10, 20, ms=50) for t in range(90)], META, cfg)
    b = analysis.analyse([sample(t, t // 30, 10, 20, ms=100) for t in range(90)], META, cfg)
    c = analysis.compare(a, b)
    assert c["overall"]["p95_ms"]["delta"] == 50
