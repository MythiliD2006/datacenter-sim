"""HTTP + WebSocket API behind the control panel, and the panel's static files."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, config, paths, system
from . import analysis
from .manager import Manager


def create_app(manager: Manager | None = None) -> FastAPI:
    mgr = manager or Manager()

    @asynccontextmanager
    async def lifespan(app):
        await mgr.startup()
        yield
        await mgr.shutdown()

    app = FastAPI(title="datacenter-sim control panel", version=__version__, lifespan=lifespan)
    app.state.manager = mgr

    def bad(e: Exception, code: int = 400):
        raise HTTPException(code, str(e))

    # ── state & system ────────────────────────────────────────────────
    @app.get("/api/state")
    async def state():
        s = mgr.state()
        s["live"] = list(mgr.live)[-600:]
        s["events"] = list(mgr.events)[-100:]
        s["version"] = __version__
        return s

    @app.get("/api/system")
    async def system_info():
        return {"system": system.probe(), "capacity": mgr.capacity()}

    # ── server under test ─────────────────────────────────────────────
    @app.post("/api/target/{action}")
    async def target(action: str):
        try:
            if action == "start":
                await mgr.start_target()
            elif action == "stop":
                if mgr.run and not mgr.run.finished:
                    raise RuntimeError("stop the running test first")
                await mgr.stop_target()
            elif action == "restart":
                if mgr.run and not mgr.run.finished:
                    raise RuntimeError("stop the running test first")
                await mgr.restart_target()
            else:
                raise HTTPException(404, "unknown action")
        except RuntimeError as e:
            bad(e, 409)
        return mgr.state()

    # ── settings ──────────────────────────────────────────────────────
    @app.get("/api/config")
    async def get_config():
        return {"config": config.load_config(), "defaults": config.default_config(), "help": config.config_help(),
                "file": str(paths.config_file())}

    @app.put("/api/config")
    async def put_config(body: dict = Body(...)):
        old = mgr.cfg
        try:
            new = config.save_config(body)
        except config.ConfigError as e:
            bad(e)
        mgr.reload_config()
        restart = []
        if new["control_panel"]["port"] != old["control_panel"]["port"] or new["control_panel"]["host"] != old["control_panel"]["host"]:
            restart.append("control panel address (restart dcsim to apply)")
        target_changed = new["target"]["mode"] != old["target"]["mode"] or new["target"]["external_url"] != old["target"]["external_url"]
        if target_changed:
            if mgr.run and not mgr.run.finished:
                restart.append("server change applies after the current test")
            else:
                if old["target"]["mode"] == "builtin" and mgr.target_proc:
                    mgr.cfg = old
                    await mgr.stop_target()
                    mgr.reload_config()
                if new["target"]["mode"] == "external":
                    mgr.target_state = "external"
                    await mgr.start_target()
                elif new["target"]["auto_start"]:
                    try:
                        await mgr.start_target()
                    except Exception as e:
                        restart.append(f"server start failed: {e}")
        elif new["server"] != old["server"] or new["auth"] != old["auth"]:
            restart.append("server settings apply the next time the built-in server starts (Restart server)")
        mgr.event("info", "Settings saved")
        return {"config": new, "notes": restart, "state": mgr.state()}

    @app.post("/api/config/reset")
    async def reset_config():
        cfg = config.save_config(config.default_config())
        mgr.reload_config()
        return {"config": cfg}

    # ── interfaces ────────────────────────────────────────────────────
    @app.get("/api/interfaces")
    async def get_interfaces():
        return config.load_interfaces()

    @app.put("/api/interfaces")
    async def put_interfaces(body: dict = Body(...)):
        try:
            return config.save_interfaces(body)
        except config.ConfigError as e:
            bad(e)

    # ── profiles ──────────────────────────────────────────────────────
    @app.get("/api/profiles")
    async def profiles():
        return config.list_profiles()

    @app.get("/api/profiles/{pid}")
    async def get_profile(pid: str):
        try:
            return config.load_profile(pid)
        except config.ConfigError as e:
            bad(e, 404)

    @app.put("/api/profiles/{pid}")
    async def put_profile(pid: str, body: dict = Body(...)):
        try:
            return config.save_profile(pid, body)
        except config.ConfigError as e:
            bad(e)

    @app.delete("/api/profiles/{pid}")
    async def del_profile(pid: str):
        try:
            config.delete_profile(pid)
        except config.ConfigError as e:
            bad(e)
        return {"deleted": pid}

    @app.post("/api/profiles/restore-defaults")
    async def restore_profiles():
        return {"restored": paths.restore_default_profiles()}

    @app.post("/api/preview")
    async def preview(body: dict = Body(...)):
        try:
            p = config.validate_profile(body)
        except config.ConfigError as e:
            bad(e)
        cap = mgr.capacity()
        stages = config.resolve_profile(p, cap["users"])
        return {"stages": stages, "capacity": cap, "peak": max(s["users"] for s in stages),
                "workers": system.worker_count(mgr.cfg, max(s["users"] for s in stages), mgr.info),
                "duration": stages[-1]["end"]}

    # ── runs ──────────────────────────────────────────────────────────
    @app.post("/api/runs")
    async def start_run(body: dict = Body(...)):
        try:
            seed = body.get("seed")
            seed = int(seed) if seed not in (None, "") else None
            return await mgr.start_run(body["profile_id"], seed=seed)
        except (RuntimeError, config.ConfigError, KeyError, ValueError) as e:
            bad(e, 409)

    @app.post("/api/calibrate")
    async def calibrate():
        try:
            return await mgr.calibrate()
        except (RuntimeError, config.ConfigError) as e:
            bad(e, 409)

    @app.post("/api/runs/stop")
    async def stop_run():
        await mgr.stop_run()
        return mgr.state()

    @app.post("/api/runs/command")
    async def command(body: dict = Body(...)):
        if body.get("cmd") not in ("set_users", "next_stage"):
            bad(ValueError("cmd must be set_users or next_stage"))
        try:
            mgr.command(body)
        except RuntimeError as e:
            bad(e, 409)
        return {"ok": True}

    @app.get("/api/runs")
    async def runs():
        return mgr.list_runs()

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        try:
            return mgr.run_detail(run_id)
        except FileNotFoundError:
            bad(FileNotFoundError("run not found"), 404)

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str):
        try:
            mgr.delete_run(run_id)
        except FileNotFoundError:
            bad(FileNotFoundError("run not found"), 404)
        except RuntimeError as e:
            bad(e, 409)
        return {"deleted": run_id}

    @app.get("/api/runs/{run_id}/report")
    async def run_report(run_id: str, download: bool = False):
        try:
            d = mgr._run_dir(run_id)
        except FileNotFoundError:
            bad(FileNotFoundError("run not found"), 404)
        f = d / "report.html"
        if not f.exists():
            bad(FileNotFoundError("report not ready yet"), 404)
        if download:
            return FileResponse(f, media_type="text/html", filename=f"dcsim-report-{run_id}.html")
        return HTMLResponse(f.read_text(encoding="utf-8"))

    @app.get("/api/runs/{run_id}/samples.csv")
    async def run_csv(run_id: str):
        try:
            text = mgr.samples_csv(run_id)
        except FileNotFoundError:
            bad(FileNotFoundError("run not found"), 404)
        return PlainTextResponse(text, media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="dcsim-{run_id}.csv"'})

    @app.get("/api/compare")
    async def compare(a: str, b: str):
        try:
            ra, rb = mgr.run_detail(a), mgr.run_detail(b)
        except FileNotFoundError:
            bad(FileNotFoundError("run not found"), 404)
        if not ra["summary"] or not rb["summary"]:
            bad(ValueError("both runs need to be finished"))
        return {"a": ra["meta"]["id"], "b": rb["meta"]["id"], **analysis.compare(ra["summary"], rb["summary"]),
                "names": {"a": ra["meta"]["profile"]["name"], "b": rb["meta"]["profile"]["name"]}}

    @app.get("/api/runs/{run_id}/files/{name}")
    async def run_file(run_id: str, name: str):
        allowed = name.startswith("chart_") and name.endswith(".png") or name in ("timeseries.csv", "phase_summary.csv")
        if not allowed or "/" in name or "\\" in name:
            bad(FileNotFoundError("not available"), 404)
        try:
            f = mgr._run_dir(run_id) / name
        except FileNotFoundError:
            bad(FileNotFoundError("run not found"), 404)
        if not f.exists():
            bad(FileNotFoundError("not available"), 404)
        return FileResponse(f, filename=name if name.endswith(".csv") else None)

    # ── worker computers ("dcsim worker --panel http://this-computer:port") ──
    @app.post("/api/agents/register")
    async def agent_register(request: Request, body: dict = Body(...)):
        try:
            return mgr.register_agent(body, request.client.host if request.client else "?")
        except ValueError as e:
            bad(e)

    @app.get("/api/agents/poll")
    async def agent_poll(id: str):
        return mgr.poll_agent(id)

    @app.get("/metrics")
    async def metrics():
        return Response(mgr.metrics_text(), media_type="text/plain; version=0.0.4")

    @app.get("/api/logs")
    async def logs(source: str = "server", lines: int = 200):
        return {"lines": mgr.logs(source, max(1, min(lines, 2000)))}

    # ── live updates ──────────────────────────────────────────────────
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        mgr.clients.add(socket)
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            mgr.clients.discard(socket)

    # ── panel ─────────────────────────────────────────────────────────
    @app.get("/")
    async def index():
        return FileResponse(paths.UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/ui", StaticFiles(directory=paths.UI_DIR), name="ui")
    logging.getLogger("dcsim.control").debug("app created")
    return app
