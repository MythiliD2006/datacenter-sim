"""Pandas + Matplotlib analysis saved with every run.

Writes into the run folder:
    timeseries.csv       one row per second (Pandas DataFrame)
    phase_summary.csv    per-phase aggregates computed with Pandas groupby
    chart_*.png          static charts for slides and written reports
"""
from __future__ import annotations

from pathlib import Path

PHASE_COLORS = {"idle": "#9aa6b3", "ramp_up": "#0f6d78", "spike": "#b3261e", "peak": "#c98a12",
                "steady": "#3f9466", "cooldown": "#5c4eaa"}


def frame(samples: list[dict]):
    import pandas as pd

    rows = []
    for s in samples:
        if s.get("final"):
            continue
        t = s.get("run_time") if s.get("run_time") is not None else s.get("t")
        if t is None:
            continue
        sysd, tg = s.get("sys") or {}, s.get("target") or {}
        row = {"t": t, "stage": s.get("stage"), "phase": s.get("phase"), "label": s.get("label"),
               "users": s.get("users"), "requests": s.get("count") or 0, "failures": s.get("fails") or 0,
               "rps": s.get("rps"), "p50_ms": s.get("p50"), "p95_ms": s.get("p95"), "p99_ms": s.get("p99"),
               "cpu_pct": sysd.get("cpu_pct"), "mem_pct": sysd.get("mem_pct"),
               "disk_read_mbs": sysd.get("disk_read_mbs"), "disk_write_mbs": sysd.get("disk_write_mbs"),
               "net_rx_mbs": sysd.get("net_rx_mbs"), "net_tx_mbs": sysd.get("net_tx_mbs"),
               "server_cpu_pct": tg.get("cpu_pct"), "server_rss_mb": tg.get("rss_mb"), "sessions": tg.get("sessions")}
        for k, n in (s.get("users_by_iface") or {}).items():
            row[f"users_{k}"] = n
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["error_pct"] = (100 * df["failures"] / df["requests"].where(df["requests"] > 0)).fillna(0.0)
    return df


def phase_table(df):
    if df.empty:
        return df
    g = df.groupby(["stage", "phase", "label"], sort=True)
    out = g.agg(start_s=("t", "min"), end_s=("t", "max"), users_avg=("users", "mean"), users_max=("users", "max"),
                requests=("requests", "sum"), failures=("failures", "sum"), rps_avg=("rps", "mean"),
                p95_ms_median=("p95_ms", "median"), p95_ms_worst=("p95_ms", "max"), cpu_max=("cpu_pct", "max"),
                mem_max=("mem_pct", "max"), server_cpu_max=("server_cpu_pct", "max"), server_rss_end=("server_rss_mb", "last"),
                disk_write_mbs_avg=("disk_write_mbs", "mean"), disk_read_mbs_avg=("disk_read_mbs", "mean"))
    out["error_pct"] = (100 * out["failures"] / out["requests"].where(out["requests"] > 0)).fillna(0.0)
    return out.round(2).reset_index()


def render(run_dir: Path, meta: dict, summary: dict, samples: list[dict]) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = frame(samples)
    if df.empty:
        return []
    df.to_csv(run_dir / "timeseries.csv", index=False)
    phase_table(df).to_csv(run_dir / "phase_summary.csv", index=False)
    stages = meta.get("stages", [])

    def bands(ax):
        for s in stages:
            ax.axvspan(s["start"], s["end"], color=PHASE_COLORS.get(s["phase"], "#999"), alpha=0.09, lw=0)

    def finish(fig, ax, name, ylabel):
        ax.set_xlabel("seconds into the test")
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
        ax.legend(loc="upper left", frameon=False, fontsize=8)
        fig.tight_layout()
        fig.savefig(run_dir / name, dpi=130)
        plt.close(fig)
        names.append(name)

    names: list[str] = []
    title = meta["profile"]["name"]

    fig, ax = plt.subplots(figsize=(9, 3.4))
    bands(ax)
    ucols = [c for c in df.columns if c.startswith("users_")]
    if ucols:
        palette = ["#0f6d78", "#c98a12", "#3f9466", "#5c4eaa", "#b3261e", "#9aa6b3", "#6aa7ae", "#8a6d3b"]
        ax.stackplot(df["t"], *[df[c].fillna(0) for c in ucols], labels=[c[6:] for c in ucols], alpha=0.85,
                     colors=[palette[i % len(palette)] for i in range(len(ucols))])
    else:
        ax.plot(df["t"], df["users"], label="users")
    ax2 = ax.twinx()
    ax2.plot(df["t"], df["rps"], color="#333", lw=1, label="requests/s")
    ax2.set_ylabel("requests/s")
    ax.set_title(f"{title}: users per interface and throughput", fontsize=10, loc="left")
    finish(fig, ax, "chart_users.png", "users")

    fig, ax = plt.subplots(figsize=(9, 3.4))
    bands(ax)
    for col, c, ls in (("p50_ms", "#6aa7ae", "-"), ("p95_ms", "#0f6d78", "-"), ("p99_ms", "#5c4eaa", "--")):
        ax.plot(df["t"], df[col], color=c, lw=1.2, ls=ls, label=col.replace("_ms", ""))
    sla = (meta.get("config") or {}).get("sla", {}).get("p95_ms")
    if sla:
        ax.axhline(sla, color="#b3261e", lw=0.8, ls=":", label=f"SLA p95 {sla} ms")
    ax.set_title("Response time by phase", fontsize=10, loc="left")
    finish(fig, ax, "chart_latency.png", "ms")

    fig, ax = plt.subplots(figsize=(9, 3.4))
    bands(ax)
    ax.plot(df["t"], df["error_pct"], color="#b3261e", lw=1.2, label="failed %")
    ax.set_ylim(0, max(1.0, float(df["error_pct"].max() or 0) * 1.15))
    ax.set_title("Failed requests", fontsize=10, loc="left")
    finish(fig, ax, "chart_errors.png", "%")

    fig, ax = plt.subplots(figsize=(9, 3.4))
    bands(ax)
    ax.plot(df["t"], df["cpu_pct"], color="#c98a12", lw=1.2, label="computer CPU %")
    ax.plot(df["t"], df["mem_pct"], color="#5c4eaa", lw=1.2, label="computer memory %")
    if df["server_cpu_pct"].notna().any():
        ax.plot(df["t"], df["server_cpu_pct"], color="#0f6d78", lw=1, ls="--", label="server CPU %")
    ax.set_ylim(0, max(100, (df["server_cpu_pct"].max() or 0) + 5))
    ax.set_title("CPU and memory", fontsize=10, loc="left")
    finish(fig, ax, "chart_resources.png", "%")

    if df[["disk_read_mbs", "disk_write_mbs", "net_rx_mbs", "net_tx_mbs"]].notna().any().any():
        fig, ax = plt.subplots(figsize=(9, 3.4))
        bands(ax)
        for col, c in (("disk_read_mbs", "#3f9466"), ("disk_write_mbs", "#b3261e"), ("net_rx_mbs", "#0f6d78"), ("net_tx_mbs", "#c98a12")):
            ax.plot(df["t"], df[col], color=c, lw=1, label=col.replace("_mbs", " MB/s").replace("_", " "))
        ax.set_title("Storage and network", fontsize=10, loc="left")
        finish(fig, ax, "chart_io.png", "MB/s")

    pt = phase_table(df)
    if not pt.empty:
        fig, ax = plt.subplots(figsize=(9, 3.4))
        x = range(len(pt))
        ax.bar(x, pt["p95_ms_median"], color=[PHASE_COLORS.get(p, "#999") for p in pt["phase"]])
        ax.set_xticks(list(x), [str(l)[:14] for l in pt["label"]], rotation=30, ha="right", fontsize=8)
        ax.set_title("Median p95 per phase", fontsize=10, loc="left")
        ax.set_ylabel("ms")
        ax.grid(axis="y", alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(run_dir / "chart_phases.png", dpi=130)
        plt.close(fig)
        names.append("chart_phases.png")
    return names
