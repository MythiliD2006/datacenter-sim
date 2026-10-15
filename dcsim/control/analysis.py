"""Turns a run's samples into per-phase results, an SLA verdict and a list of
likely defects. All thresholds come from the ``sla`` and ``analysis`` settings."""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import fmean


def hist_merge(hists) -> Counter:
    out: Counter = Counter()
    for h in hists:
        for k, v in (h or {}).items():
            out[int(k)] += v
    return out


def pct(hist: Counter, q: float) -> float | None:
    n = sum(hist.values())
    if not n:
        return None
    target = q * n
    seen = 0
    for k in sorted(hist):
        seen += hist[k]
        if seen >= target:
            return float(k)
    return float(max(hist))


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(fmean(xs), 2) if xs else None


def _max(xs):
    xs = [x for x in xs if x is not None]
    return round(max(xs), 2) if xs else None


def _get(s, *keys):
    for k in keys:
        s = (s or {}).get(k)
    return s


def summarize_block(samples: list[dict]) -> dict:
    hist = hist_merge(s.get("hist") for s in samples)
    count = sum(s.get("count", 0) for s in samples)
    fails = sum(s.get("fails", 0) for s in samples)
    secs = max(1e-9, sum(s.get("dt", 1.0) for s in samples))
    iface: dict = defaultdict(lambda: {"count": 0, "fails": 0, "hists": []})
    endpoint: dict = defaultdict(lambda: {"count": 0, "fails": 0, "hists": []})
    errors: Counter = Counter()
    for s in samples:
        for src, dst in ((s.get("iface") or {}, iface), (s.get("endpoint") or {}, endpoint)):
            for k, v in src.items():
                dst[k]["count"] += v["count"]
                dst[k]["fails"] += v["fails"]
                dst[k]["hists"].append(v.get("hist"))
        errors.update(s.get("errors") or {})

    def side(d):
        out = {}
        for k, v in d.items():
            h = hist_merge(v["hists"])
            out[k] = {"requests": v["count"], "rps": round(v["count"] / secs, 2),
                      "error_rate_pct": round(100 * v["fails"] / v["count"], 2) if v["count"] else 0.0,
                      "p50_ms": pct(h, 0.5), "p95_ms": pct(h, 0.95), "p99_ms": pct(h, 0.99)}
        return out

    return {
        "duration_sec": round(secs, 1),
        "users_avg": _avg(s.get("users") for s in samples), "users_max": _max(s.get("users") for s in samples),
        "requests": count, "failures": fails, "rps": round(count / secs, 2),
        "error_rate_pct": round(100 * fails / count, 2) if count else 0.0,
        "p50_ms": pct(hist, 0.5), "p95_ms": pct(hist, 0.95), "p99_ms": pct(hist, 0.99),
        "cpu_avg": _avg(_get(s, "sys", "cpu_pct") for s in samples), "cpu_max": _max(_get(s, "sys", "cpu_pct") for s in samples),
        "mem_avg": _avg(_get(s, "sys", "mem_pct") for s in samples), "mem_max": _max(_get(s, "sys", "mem_pct") for s in samples),
        "server_cpu_avg": _avg(_get(s, "target", "cpu_pct") for s in samples),
        "server_cpu_max": _max(_get(s, "target", "cpu_pct") for s in samples),
        "server_rss_start": _get(samples[0], "target", "rss_mb") if samples else None,
        "server_rss_end": _get(samples[-1], "target", "rss_mb") if samples else None,
        "sessions_end": _get(samples[-1], "target", "sessions") if samples else None,
        "load_cpu_max": _max(_get(s, "load", "cpu_pct_per_worker") for s in samples),
        "disk_write_mbs_avg": _avg(_get(s, "sys", "disk_write_mbs") for s in samples),
        "disk_read_mbs_avg": _avg(_get(s, "sys", "disk_read_mbs") for s in samples),
        "net_mbs_avg": _avg((_get(s, "sys", "net_rx_mbs") or 0) + (_get(s, "sys", "net_tx_mbs") or 0) for s in samples),
        "by_interface": side(iface), "by_endpoint": side(endpoint),
        "top_errors": [{"error": k, "count": v} for k, v in errors.most_common(8)],
    }


def segments(samples: list[dict]) -> list[list[dict]]:
    out, cur, key = [], [], None
    for s in samples:
        k = s.get("stage")
        if s.get("final"):
            if cur:
                cur.append(s)
            continue
        if k != key and cur:
            out.append(cur)
            cur = []
        key = k
        cur.append(s)
    if cur:
        out.append(cur)
    return out


def analyse(samples: list[dict], meta: dict, cfg: dict, events: list[dict] | None = None) -> dict:
    sla, rules = cfg["sla"], cfg["analysis"]
    events = events or []
    stages = {s["index"]: s for s in meta.get("stages", [])}
    phases = []
    for seg in segments(samples):
        st = stages.get(seg[0].get("stage"), {})
        block = summarize_block(seg)
        win = rules["transition_window_sec"]
        t0 = seg[0].get("t", 0)
        head = [s for s in seg if s.get("t", 0) - t0 < win]
        tail = [s for s in seg if s.get("t", 0) - t0 >= win]
        block["transition_p95_ms"] = pct(hist_merge(s.get("hist") for s in head), 0.95) if head else None
        block["settled_p95_ms"] = pct(hist_merge(s.get("hist") for s in tail), 0.95) if tail else None
        block.update({"stage": seg[0].get("stage"), "phase": st.get("phase", seg[0].get("phase")),
                      "label": st.get("label", seg[0].get("label")), "target_users": st.get("users"),
                      "start_sec": round(t0, 1), "end_sec": round(seg[-1].get("t", 0), 1)})
        phases.append(block)

    active = [s for s in samples if (s.get("users") or 0) > 0 or s.get("count")]
    overall = summarize_block(samples) if samples else summarize_block([])
    cpu_series = sorted(x for x in (_get(s, "sys", "cpu_pct") for s in active) if x is not None)
    cpu_p95 = cpu_series[int(0.95 * (len(cpu_series) - 1))] if cpu_series else None

    checks = []

    def check(name, value, limit, unit, ok_when_le=True):
        if value is None:
            checks.append({"name": name, "value": None, "limit": limit, "unit": unit, "pass": None})
            return
        checks.append({"name": name, "value": value, "limit": limit, "unit": unit,
                       "pass": value <= limit if ok_when_le else value >= limit})

    check("p95 latency", overall["p95_ms"], sla["p95_ms"], "ms")
    check("Error rate", overall["error_rate_pct"], sla["error_rate_pct"], "%")
    check("CPU (95th percentile)", cpu_p95, sla["cpu_pct"], "%")
    check("Memory (max)", overall["mem_max"], sla["memory_pct"], "%")
    crashes = [e for e in events if e.get("kind") == "crash"]
    verdict = "FAIL" if crashes or any(c["pass"] is False for c in checks) else "PASS"

    findings = []

    def find(sev, title, detail, phase=None, kind="other"):
        findings.append({"severity": sev, "title": title, "detail": detail, "phase": phase, "kind": kind})

    for e in crashes:
        find("critical", "Server stopped responding", f"Health checks failed at {e.get('t', '?')} s into the run: {e.get('msg', '')}", e.get("phase"), "crash")

    for p in phases:
        tp, sp = p["transition_p95_ms"], p["settled_p95_ms"]
        if tp and sp and tp > 50 and tp > rules["transition_p95_ratio"] * sp:
            find("warning", f"Latency jump entering '{p['label']}'",
                 f"p95 was {tp:.0f} ms in the first {rules['transition_window_sec']} s of this {p['phase'].replace('_', ' ')} phase "
                 f"vs {sp:.0f} ms once settled ({tp / sp:.1f}x). Phase transitions are where session storms and lock contention show up.",
                 p["label"], "transition")
        if p["requests"] >= 20 and p["error_rate_pct"] > sla["error_rate_pct"]:
            top = "; ".join(f"{e['error']} ({e['count']})" for e in p["top_errors"][:3])
            find("critical" if p["error_rate_pct"] > 5 * sla["error_rate_pct"] else "warning",
                 f"Errors during '{p['label']}' ({p['error_rate_pct']}%)", f"Most common: {top}", p["label"],
                 "transition_errors" if p["phase"] in ("ramp_up", "spike", "cooldown") else "errors")
        ifs = {k: v for k, v in p["by_interface"].items() if v["requests"] >= 20 and v["p95_ms"]}
        if p["phase"] in ("peak", "spike") and len(ifs) >= 2:
            slow = max(ifs, key=lambda k: ifs[k]["p95_ms"])
            fast = min(ifs, key=lambda k: ifs[k]["p95_ms"])
            ratio = ifs[slow]["p95_ms"] / max(ifs[fast]["p95_ms"], 1)
            if ratio >= rules["interface_starvation_ratio"] and ifs[slow]["p95_ms"] > 100:
                find("warning", f"'{slow}' clients slowed most at peak",
                     f"At '{p['label']}' their p95 was {ifs[slow]['p95_ms']:.0f} ms vs {ifs[fast]['p95_ms']:.0f} ms for '{fast}' "
                     f"({ratio:.1f}x) - possible cross-interface contention or starvation.", p["label"], "contention")
        if p["cpu_max"] and p["cpu_max"] >= rules["cpu_saturation_pct"] and p["p95_ms"] and p["p95_ms"] > sla["p95_ms"]:
            find("warning", f"CPU saturated during '{p['label']}'",
                 f"CPU reached {p['cpu_max']:.0f}% while p95 was {p['p95_ms']:.0f} ms. The machine ran out of CPU.", p["label"], "cpu")

    login_path = cfg["auth"]["login_path"]
    lg = overall["by_endpoint"].get(f"POST {login_path}")
    if lg and lg["requests"] >= 10 and lg["error_rate_pct"] > sla["error_rate_pct"]:
        find("critical", f"Logins failing ({lg['error_rate_pct']}%)",
             "Simulated users could not log in, so they generated far less traffic than planned. Check the rate limit (server.rate_limit) "
             "and that the server has enough accounts.", None, "login")

    rss = [(_get(s, "target", "rss_mb"), s) for s in samples if _get(s, "target", "rss_mb")]
    if len(rss) > 10:
        start = fmean(x for x, _ in rss[:5])
        end = fmean(x for x, _ in rss[-5:])
        if start and (end - start) / start * 100 >= rules["memory_growth_pct"]:
            find("warning", "Server memory grew and did not come back",
                 f"Server memory went from {start:.0f} MB to {end:.0f} MB (+{(end - start) / start * 100:.0f}%) by the end of the run. "
                 "If users have left, this points to a leak or a cache that is never cleared.", None, "leak")

    if samples:
        last = [s for s in samples if _get(s, "target", "sessions") is not None]
        if last:
            sess = _get(last[-1], "target", "sessions")
            users_left = last[-1].get("users") or 0
            if sess is not None and sess > rules["session_leftover_ratio"] * users_left + 5:
                find("warning", "Sessions left open after users left",
                     f"{sess} server sessions were still open with {users_left} users connected at the end. Sessions that are not "
                     "cleaned up at cooldown pile up and are removed later in one go (cleanup storm).", None, "cleanup")

    if overall["load_cpu_max"] and overall["load_cpu_max"] >= 90:
        find("info", "Load generator was at its limit",
             f"A load-generator process reached {overall['load_cpu_max']:.0f}% CPU. Results may understate what the server can take; "
             "add workers (load.workers) or lower users.", None, "loadgen")

    planned_peak = max((s.get("users", 0) for s in meta.get("stages", [])), default=0)
    reached = overall["users_max"] or 0
    if planned_peak and reached < 0.8 * planned_peak and not meta.get("stopped_early"):
        find("info", "Planned users were not reached",
             f"The profile asked for up to {planned_peak} users but at most {reached:.0f} were running.", None, "plan")

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return {"verdict": verdict, "checks": checks, "overall": overall, "phases": phases, "findings": findings,
            "coverage": coverage(meta, overall, phases, findings), "seed": meta.get("seed"), "events": events[-200:]}


def planned_endpoints(meta: dict) -> set[str]:
    cfg = meta.get("config") or {}
    auth = cfg.get("auth") or {}
    out = set()
    for iface in (meta.get("interfaces") or {}).values():
        if iface.get("login", True) and auth:
            out.add(f"POST {auth.get('login_path', '/login')}")
        for t in iface.get("tasks", []):
            out.add(f"GRPC {t['path'].lstrip('/')}" if t.get("protocol") == "grpc" else f"{t['method']} {t['path']}")
            if t.get("assets"):
                out.add("GET /static/*")
    return out


def coverage(meta: dict, overall: dict, phases: list[dict], findings: list[dict]) -> dict:
    """How much of the plan was actually exercised, and how many defects were found where."""
    planned_if = list((meta.get("profile") or {}).get("mix", {}))
    hit_if = [k for k, v in overall["by_interface"].items() if v["requests"] > 0]
    planned_ep = planned_endpoints(meta)
    hit_ep = {k for k, v in overall["by_endpoint"].items() if v["requests"] > 0}
    stages = meta.get("stages", [])
    hit_stages = {p["stage"] for p in phases if p["requests"] > 0}
    planned_phases = sorted({s["phase"] for s in stages})
    hit_phases = sorted({p["phase"] for p in phases if p["requests"] > 0})
    by_kind: dict = {}
    for f in findings:
        by_kind[f.get("kind", "other")] = by_kind.get(f.get("kind", "other"), 0) + 1
    transition = sum(by_kind.get(k, 0) for k in ("transition", "transition_errors"))

    def pct(a, b):
        return round(100 * a / b, 1) if b else None
    return {
        "interfaces": {"planned": planned_if, "exercised": sorted(hit_if), "pct": pct(len(set(hit_if) & set(planned_if)), len(planned_if))},
        "endpoints": {"planned": sorted(planned_ep), "exercised": sorted(hit_ep & planned_ep) if planned_ep else sorted(hit_ep),
                      "pct": pct(len(hit_ep & planned_ep), len(planned_ep)) if planned_ep else None},
        "time_windows": {"planned": len(stages), "exercised": len(hit_stages), "pct": pct(len(hit_stages), len(stages))},
        "phases": {"planned": planned_phases, "exercised": hit_phases, "pct": pct(len(hit_phases), len(planned_phases))},
        "users": {"planned_peak": max((s.get("users", 0) for s in stages), default=0), "reached_peak": overall.get("users_max")},
        "defects": {"total": len(findings), "transition": transition, "by_kind": by_kind,
                    "critical": sum(1 for f in findings if f["severity"] == "critical"),
                    "warning": sum(1 for f in findings if f["severity"] == "warning")},
    }


def calibration_result(summary: dict, cfg: dict) -> dict:
    """Highest step that still met the SLA."""
    sla = cfg["sla"]
    best, rows = 0, []
    for p in summary["phases"]:
        ok = (p["requests"] > 0 and (p["p95_ms"] or 0) <= sla["p95_ms"] and p["error_rate_pct"] <= sla["error_rate_pct"]
              and (p["cpu_max"] or 0) <= max(sla["cpu_pct"], 95))
        rows.append({"users": p["target_users"], "p95_ms": p["p95_ms"], "error_rate_pct": p["error_rate_pct"],
                     "cpu_max": p["cpu_max"], "ok": ok})
        if ok and (p["target_users"] or 0) > best:
            best = p["target_users"]
        if not ok:
            break
    return {"measured_users": best, "steps": rows}


def compare(a: dict, b: dict) -> dict:
    """Line up two runs' phases by position and phase name."""
    def key(p):
        return f"{p['phase']}: {p['label']}"
    keys = []
    for p in a["phases"] + b["phases"]:
        if key(p) not in keys:
            keys.append(key(p))
    am = {key(p): p for p in a["phases"]}
    bm = {key(p): p for p in b["phases"]}
    metrics = ["users_avg", "rps", "p95_ms", "error_rate_pct", "cpu_max", "server_cpu_max"]
    rows = []
    for k in keys:
        row = {"phase": k}
        for m in metrics:
            va, vb = (am.get(k) or {}).get(m), (bm.get(k) or {}).get(m)
            row[m] = {"a": va, "b": vb, "delta": round(vb - va, 2) if va is not None and vb is not None else None}
        rows.append(row)
    overall = {}
    for m in metrics + ["p99_ms", "requests"]:
        va, vb = a["overall"].get(m), b["overall"].get(m)
        overall[m] = {"a": va, "b": vb, "delta": round(vb - va, 2) if va is not None and vb is not None else None}
    return {"phases": rows, "overall": overall, "verdicts": {"a": a["verdict"], "b": b["verdict"]}}
