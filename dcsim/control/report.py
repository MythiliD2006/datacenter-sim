"""Self-contained HTML report for one run (opens offline, prints to PDF)."""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path
from datetime import datetime

from .. import paths


def _f(v, unit="", digits=0):
    if v is None:
        return "–"
    if isinstance(v, float):
        v = round(v, digits) if digits else round(v)
    return f"{v:,}{unit}" if isinstance(v, (int, float)) else f"{v}{unit}"


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def series(samples: list[dict]) -> dict:
    out = {k: [] for k in ("users", "rps", "p50", "p95", "p99", "err", "cpu", "mem", "scpu", "rss", "sess")}
    for s in samples:
        t = s.get("run_time") if s.get("run_time") is not None else s.get("t")
        if t is None or s.get("final"):
            continue
        sysd, tg = s.get("sys") or {}, s.get("target") or {}
        cnt = s.get("count") or 0
        vals = {"users": s.get("users"), "rps": s.get("rps"), "p50": s.get("p50"), "p95": s.get("p95"), "p99": s.get("p99"),
                "err": round(100 * (s.get("fails") or 0) / cnt, 2) if cnt else 0, "cpu": sysd.get("cpu_pct"),
                "mem": sysd.get("mem_pct"), "scpu": tg.get("cpu_pct"), "rss": tg.get("rss_mb"), "sess": tg.get("sessions")}
        for k, v in vals.items():
            out[k].append({"x": t, "y": v})
    return out


def _coverage_html(cov: dict | None) -> str:
    if not cov:
        return ""
    d = cov["defects"]
    kinds = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(d["by_kind"].items())) or "none"

    def row(name, part, extra=""):
        return (f"<tr><td>{_e(name)}</td><td class='num'>{_f(part.get('pct'), '%', 1)}</td>"
                f"<td class='muted'>{extra}</td></tr>")
    return (f"<section class='card'><h2>Coverage and defects</h2><table><tbody>"
            + row("Interfaces exercised", cov["interfaces"], _e(", ".join(cov["interfaces"]["exercised"])))
            + row("Endpoints exercised", cov["endpoints"], f"{len(cov['endpoints']['exercised'])} of {len(cov['endpoints']['planned'])}")
            + row("Time windows (stages) with traffic", cov["time_windows"], f"{cov['time_windows']['exercised']} of {cov['time_windows']['planned']}")
            + row("Phase types exercised", cov["phases"], _e(", ".join(p.replace('_', ' ') for p in cov["phases"]["exercised"])))
            + f"<tr><td>Peak users reached</td><td class='num'>{_f(cov['users']['reached_peak'])}</td><td class='muted'>planned {_f(cov['users']['planned_peak'])}</td></tr>"
            + f"<tr><td>Defects flagged</td><td class='num'>{d['total']}</td><td class='muted'>{d['critical']} critical, {d['warning']} warning · {_e(kinds)}</td></tr>"
            + f"<tr><td>Defects at phase transitions</td><td class='num'>{d['transition']}</td><td class='muted'>latency jumps or error bursts when a phase starts</td></tr>"
            + "</tbody></table></section>")


def _png_gallery(run_dir: Path | None, names: list[str] | None) -> str:
    if not run_dir or not names:
        return ""
    imgs = []
    for n in names:
        f = run_dir / n
        if f.exists():
            b64 = base64.b64encode(f.read_bytes()).decode()
            imgs.append(f"<img alt='{_e(n)}' src='data:image/png;base64,{b64}' style='width:100%;height:auto;border-radius:4px'>")
    if not imgs:
        return ""
    return ("<section class='card'><h2>Static charts (Pandas + Matplotlib)</h2><p class='muted'>Also saved as PNG files in the run folder, "
            "with timeseries.csv and phase_summary.csv.</p><div class='grid2'>" + "".join(f"<div>{i}</div>" for i in imgs) + "</div></section>")


def render(meta: dict, summary: dict, samples: list[dict], run_dir: Path | None = None) -> str:
    css = (paths.UI_DIR / "style.css").read_text(encoding="utf-8")
    chartjs = (paths.UI_DIR / "vendor" / "chart.umd.js").read_text(encoding="utf-8")
    helpers = (paths.UI_DIR / "charts.js").read_text(encoding="utf-8")
    o, sysi, cap = summary["overall"], meta.get("system", {}), meta.get("capacity", {})
    started = datetime.fromtimestamp(meta["started"]).strftime("%d %b %Y, %H:%M:%S")
    dur = (meta.get("ended") or meta["started"]) - meta["started"]
    v = summary["verdict"]

    checks = "".join(
        f"<tr><td>{_e(c['name'])}</td><td class='num'>{_f(c['value'], c['unit'], 1)}</td><td class='num'>≤ {_f(c['limit'], c['unit'])}</td>"
        f"<td><span class='pill {'ok' if c['pass'] else ('bad' if c['pass'] is False else 'muted')}'>"
        f"{'Pass' if c['pass'] else ('Fail' if c['pass'] is False else 'n/a')}</span></td></tr>" for c in summary["checks"])

    phases = "".join(
        f"<tr><td><span class='phase-dot {p['phase']}'></span>{_e(p['label'])}</td><td>{_e(p['phase'].replace('_', ' '))}</td>"
        f"<td class='num'>{_f(p['target_users'])}</td><td class='num'>{_f(p['users_avg'])}</td><td class='num'>{_f(p['rps'], '', 1)}</td>"
        f"<td class='num'>{_f(p['p50_ms'], ' ms')}</td><td class='num'>{_f(p['p95_ms'], ' ms')}</td><td class='num'>{_f(p['p99_ms'], ' ms')}</td>"
        f"<td class='num'>{_f(p['error_rate_pct'], '%', 2)}</td><td class='num'>{_f(p['cpu_max'], '%')}</td>"
        f"<td class='num'>{_f(p['server_cpu_max'], '%')}</td></tr>" for p in summary["phases"])

    def side_rows(d):
        return "".join(
            f"<tr><td>{_e(k)}</td><td class='num'>{_f(x['requests'])}</td><td class='num'>{_f(x['rps'], '', 1)}</td>"
            f"<td class='num'>{_f(x['p50_ms'], ' ms')}</td><td class='num'>{_f(x['p95_ms'], ' ms')}</td><td class='num'>{_f(x['p99_ms'], ' ms')}</td>"
            f"<td class='num'>{_f(x['error_rate_pct'], '%', 2)}</td></tr>"
            for k, x in sorted(d.items(), key=lambda kv: -kv[1]["requests"]))

    findings = "".join(
        f"<div class='finding {f['severity']}'><div class='finding-head'><span class='pill {f['severity']}'>{_e(f['severity'])}</span>"
        f"<strong>{_e(f['title'])}</strong></div><p>{_e(f['detail'])}</p></div>" for f in summary["findings"]
    ) or "<p class='muted'>No likely defects were flagged for this run.</p>"

    errors = "".join(f"<tr><td class='mono'>{_e(e['error'])}</td><td class='num'>{_f(e['count'])}</td></tr>" for e in o["top_errors"]) \
        or "<tr><td class='muted' colspan='2'>No failed requests.</td></tr>"

    cal = ""
    if summary.get("calibration"):
        c = summary["calibration"]
        rows = "".join(f"<tr><td class='num'>{_f(r['users'])}</td><td class='num'>{_f(r['p95_ms'], ' ms')}</td>"
                       f"<td class='num'>{_f(r['error_rate_pct'], '%', 2)}</td><td class='num'>{_f(r['cpu_max'], '%')}</td>"
                       f"<td><span class='pill {'ok' if r['ok'] else 'bad'}'>{'within SLA' if r['ok'] else 'over SLA'}</span></td></tr>"
                       for r in c["steps"])
        cal = (f"<section class='card'><h2>Calibration</h2><p>Highest step within the SLA: <strong>{_f(c['measured_users'])} users</strong>. "
               f"This is now used as this computer's capacity.</p><table><thead><tr><th>Users</th><th>p95</th><th>Errors</th><th>CPU max</th>"
               f"<th></th></tr></thead><tbody>{rows}</tbody></table></section>")

    data = json.dumps({"series": series(samples), "stages": meta.get("stages", [])})
    mix = ", ".join(f"{k} {w}" for k, w in meta["profile"]["mix"].items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run report – {_e(meta['profile']['name'])}</title>
<style>{css}
body{{padding:24px 16px}} .report{{max-width:1100px;margin:0 auto;display:grid;gap:16px}}
.chart-box{{height:220px}} @media print{{.chart-box{{height:180px}} body{{padding:0}}}}
</style></head>
<body><div class="report">
<header class="report-head">
  <div><div class="eyebrow">datacenter-sim run report</div><h1>{_e(meta['profile']['name'])}</h1>
  <p class="muted">{_e(started)} · {_f(dur / 60, ' min', 1)} · {_e(sysi.get('hostname'))} ({_e(sysi.get('os'))}) · run id {_e(meta['id'])} · seed {_e(meta.get('seed'))}</p></div>
  <div class="verdict {v.lower()}">{v}</div>
</header>
<section class="kpis">
  <div class="kpi"><div class="kpi-label">Peak users</div><div class="kpi-value">{_f(o['users_max'])}</div></div>
  <div class="kpi"><div class="kpi-label">Requests</div><div class="kpi-value">{_f(o['requests'])}</div></div>
  <div class="kpi"><div class="kpi-label">Throughput</div><div class="kpi-value">{_f(o['rps'], '', 1)}<small> req/s</small></div></div>
  <div class="kpi"><div class="kpi-label">p95 latency</div><div class="kpi-value">{_f(o['p95_ms'])}<small> ms</small></div></div>
  <div class="kpi"><div class="kpi-label">Error rate</div><div class="kpi-value">{_f(o['error_rate_pct'], '', 2)}<small> %</small></div></div>
  <div class="kpi"><div class="kpi-label">Findings</div><div class="kpi-value">{len(summary['findings'])}</div></div>
</section>
<section class="card"><h2>What to look at</h2>{findings}</section>
{_coverage_html(summary.get("coverage"))}
{cal}
<section class="grid2">
  <div class="card"><h3>Users and throughput</h3><div class="chart-box"><canvas id="c1"></canvas></div></div>
  <div class="card"><h3>Latency</h3><div class="chart-box"><canvas id="c2"></canvas></div></div>
  <div class="card"><h3>Errors</h3><div class="chart-box"><canvas id="c3"></canvas></div></div>
  <div class="card"><h3>CPU and memory</h3><div class="chart-box"><canvas id="c4"></canvas></div></div>
  <div class="card"><h3>Server memory and sessions</h3><div class="chart-box"><canvas id="c5"></canvas></div></div>
  <div class="card"><h3>SLA checks</h3><table><thead><tr><th>Check</th><th class="num">Result</th><th class="num">Target</th><th></th></tr></thead><tbody>{checks}</tbody></table></div>
</section>
<section class="card"><h2>By phase</h2><div class="table-wrap"><table><thead><tr><th>Stage</th><th>Phase</th><th class="num">Planned users</th><th class="num">Avg users</th>
<th class="num">Req/s</th><th class="num">p50</th><th class="num">p95</th><th class="num">p99</th><th class="num">Errors</th><th class="num">CPU max</th><th class="num">Server CPU max</th></tr></thead>
<tbody>{phases}</tbody></table></div></section>
<section class="grid2">
<div class="card"><h2>By interface</h2><div class="table-wrap"><table><thead><tr><th>Interface</th><th class="num">Requests</th><th class="num">Req/s</th><th class="num">p50</th><th class="num">p95</th><th class="num">p99</th><th class="num">Errors</th></tr></thead><tbody>{side_rows(o['by_interface'])}</tbody></table></div></div>
<div class="card"><h2>By endpoint</h2><div class="table-wrap"><table><thead><tr><th>Endpoint</th><th class="num">Requests</th><th class="num">Req/s</th><th class="num">p50</th><th class="num">p95</th><th class="num">p99</th><th class="num">Errors</th></tr></thead><tbody>{side_rows(o['by_endpoint'])}</tbody></table></div></div>
</section>
<section class="grid2">
<div class="card"><h2>Top errors</h2><table><tbody>{errors}</tbody></table></div>
<div class="card"><h2>Test setup</h2><table><tbody>
<tr><td>Computer</td><td>{_e(sysi.get('logical_cores'))} cores · {_f(sysi.get('memory_total_mb'), ' MB')} RAM · {_e(sysi.get('os'))}</td></tr>
<tr><td>Capacity used</td><td>{_f(cap.get('users'))} users ({_e(cap.get('source'))})</td></tr>
<tr><td>Server under test</td><td>{_e(meta.get('target_mode'))} · {_e(meta.get('target_url'))} · {_f(meta.get('accounts'))} accounts</td></tr>
<tr><td>Load workers</td><td>{_e(meta.get('workers'))}</td></tr>
<tr><td>Interface mix</td><td>{_e(mix)}</td></tr>
<tr><td>Stopped early</td><td>{'yes' if meta.get('stopped_early') else 'no'}</td></tr>
<tr><td>Random seed</td><td>{_e(meta.get('seed'))} (start a run with this seed to repeat it exactly)</td></tr>
<tr><td>Worker computers</td><td>{len(meta.get('agent_assignments') or {})} besides this one</td></tr>
</tbody></table></div>
</section>
{_png_gallery(run_dir, summary.get("charts"))}
</div>
<script>{chartjs}</script><script>{helpers}</script>
<script>
const D = {data};
const S = D.series, st = D.stages, xmax = st.length ? st[st.length-1].end : undefined;
const L = (id, series, o) => DCCharts.lineChart(document.getElementById(id), Object.assign({{series, stages: st, xmin: 0, xmax}}, o||{{}}));
L("c1", [{{label:"Users", data:S.users, color:"--c-users"}}, {{label:"Requests/s", data:S.rps, color:"--c-rps", axis:"y2"}}], {{yTitle:"users", y2:"req/s"}});
L("c2", [{{label:"p50", data:S.p50, color:"--c-p50"}}, {{label:"p95", data:S.p95, color:"--c-p95"}}, {{label:"p99", data:S.p99, color:"--c-p99", dash:[4,3]}}], {{yTitle:"ms"}});
L("c3", [{{label:"Error rate %", data:S.err, color:"--c-err"}}], {{yTitle:"%"}});
L("c4", [{{label:"Host CPU %", data:S.cpu, color:"--c-cpu"}}, {{label:"Host memory %", data:S.mem, color:"--c-mem"}}, {{label:"Server CPU %", data:S.scpu, color:"--c-scpu", dash:[4,3]}}], {{yTitle:"%"}});
L("c5", [{{label:"Server memory MB", data:S.rss, color:"--c-mem"}}, {{label:"Open sessions", data:S.sess, color:"--c-users", axis:"y2"}}], {{yTitle:"MB", y2:"sessions"}});
</script></body></html>"""
