"""Command line entry point.

    dcsim                 open the control panel (default)
    dcsim info            show this computer's resources and user capacity
    dcsim run <profile>   run one profile without the panel and print the verdict
    dcsim profiles        list profiles
    dcsim worker --panel URL   lend this computer's capacity to a panel on another computer
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
import webbrowser

from . import __version__, config, paths, system


def _setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(paths.logs_dir() / "control.log", encoding="utf-8")], force=True)


def cmd_panel(a):
    import uvicorn

    from .control.server import create_app

    cfg = config.load_config()
    host = a.host or cfg["control_panel"]["host"]
    port = system.free_port(host, a.port or int(cfg["control_panel"]["port"]))
    app = create_app()
    app.state.manager.control_port = port
    app.state.manager.panel_host = host
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}"
    cap = system.estimate_capacity(cfg)
    print(f"\n  datacenter-sim {__version__}")
    print(f"  Control panel : {url}")
    print(f"  Data folder   : {paths.home()}")
    print(f"  Capacity      : {cap['users']} simulated users, {cap['source']}")
    print("  Press Ctrl+C to stop.\n")
    if host in ("0.0.0.0", ""):
        print(f"  Other computers can join with:  dcsim worker --panel http://{_lan_ip()}:{port}")
    if cfg["control_panel"]["open_browser"] and not a.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "THIS-COMPUTER-IP"
    finally:
        s.close()


def cmd_worker(a):
    from . import agent
    agent.run(a.panel, a.workers)


def cmd_info(a):
    cfg = config.load_config()
    info = system.probe()
    cap = system.estimate_capacity(cfg, info)
    if a.json:
        print(json.dumps({"system": info, "capacity": cap}, indent=2))
        return
    print(f"Computer : {info['hostname']} - {info['os']}, Python {info['python']}")
    print(f"CPU      : {info['logical_cores']} logical cores ({info['physical_cores']} physical)")
    print(f"Memory   : {info['memory_available_mb']} MB free of {info['memory_total_mb']} MB")
    print(f"Capacity : {cap['users']} users - {cap['source']}")
    for k, v in cap["limits"].items():
        print(f"           limit by {k.replace('_', ' ')}: {v}")
    print(f"Workers  : {system.worker_count(cfg, cap['users'], info)} load-generator process(es) at full capacity")


def cmd_profiles(a):
    for p in config.list_profiles():
        print(f"{p['id']:<24} {p['duration']:>5}s  {p['name']}")


def cmd_run(a):
    from .control.manager import Manager

    async def go():
        mgr = Manager()
        await mgr.startup()
        try:
            st = await (mgr.calibrate() if a.profile == "calibrate" else mgr.start_run(a.profile, seed=a.seed))
            total = st["run"]["total"]
            print(f"Running '{st['run']['profile_name']}' for about {total}s against {mgr.target_url} ...")
            last = 0
            while not mgr.run.processed:
                await asyncio.sleep(1)
                s = mgr.run.latest or {}
                if time.time() - last >= 10 and s:
                    last = time.time()
                    print(f"  t={s.get('t', 0):>6.0f}s  {s.get('label', ''):<22} users={s.get('users', 0):<5} "
                          f"rps={s.get('rps', 0):<7} p95={s.get('p95')}ms")
            summary = json.loads((mgr.run.dir / "summary.json").read_text(encoding="utf-8"))
            print(f"\nVerdict: {summary['verdict']}   (seed {summary.get('seed')} - rerun with --seed {summary.get('seed')})")
            for c in summary["checks"]:
                print(f"  {c['name']:<24} {c['value']} {c['unit']} (limit {c['limit']}) {'ok' if c['pass'] else 'FAIL' if c['pass'] is False else ''}")
            for f in summary["findings"]:
                print(f"  [{f['severity']}] {f['title']}")
            print(f"\nReport: {mgr.run.dir / 'report.html'}")
            return 0 if summary["verdict"] == "PASS" else 2
        finally:
            await mgr.shutdown()
    try:
        sys.exit(asyncio.run(go()))
    except KeyboardInterrupt:
        print("stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dcsim", description="Real-world multi-user data centre workload simulation")
    ap.add_argument("--home", help="data folder (default ~/.dcsim or $DCSIM_HOME)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("panel", help="open the control panel (default)")
    for target in (ap, p):
        target.add_argument("--host", help="address to serve the panel on")
        target.add_argument("--port", type=int, help="panel port")
        target.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
        target.add_argument("--reset", action="store_true", help="restore default settings, interfaces and profiles")
    i = sub.add_parser("info", help="show this computer's capacity")
    i.add_argument("--json", action="store_true")
    sub.add_parser("profiles", help="list workload profiles")
    r = sub.add_parser("run", help="run a profile without the panel (use 'calibrate' to measure capacity)")
    r.add_argument("profile")
    r.add_argument("--seed", type=int, help="repeat a previous run exactly by giving its seed")
    w = sub.add_parser("worker", help="lend this computer's capacity to a control panel on another computer")
    w.add_argument("--panel", required=True, help="the panel's address, e.g. http://192.168.1.20:8765")
    w.add_argument("--workers", type=int, help="worker processes to run here (default: based on this computer)")
    a = ap.parse_args(argv)
    paths.set_home(a.home)
    paths.ensure_home(reset=getattr(a, "reset", False))
    _setup_logging()
    {"info": cmd_info, "profiles": cmd_profiles, "run": cmd_run, "worker": cmd_worker}.get(a.cmd, cmd_panel)(a)


if __name__ == "__main__":
    main()
