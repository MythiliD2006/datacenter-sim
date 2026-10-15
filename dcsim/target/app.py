"""The bundled server under test.

Every delay, size and limit comes from the ``server`` section of the settings,
and the number of accounts is decided at launch from this computer's capacity.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import secrets
import statistics
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest


def _rand(rng: list | tuple) -> float:
    lo, hi = rng
    return random.uniform(lo, hi)


def _randint(rng: list | tuple) -> int:
    lo, hi = int(rng[0]), int(rng[1])
    return random.randint(min(lo, hi), max(lo, hi))


class JSONFormatter(logging.Formatter):
    def format(self, record):
        d = {"ts": datetime.now(timezone.utc).isoformat(), "level": record.levelname, "msg": record.getMessage()}
        d.update(getattr(record, "fields", {}) or {})
        return json.dumps(d)


def _logger(log_file: Path | None) -> logging.Logger:
    log = logging.getLogger(f"dcsim.target.{id(log_file)}")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        h = logging.FileHandler(log_file, encoding="utf-8") if log_file else logging.StreamHandler()
        h.setFormatter(JSONFormatter())
        log.addHandler(h)
    return log


class Store:
    def __init__(self, cfg: dict, accounts: int):
        auth, srv = cfg["auth"], cfg["server"]
        self.template = auth["account_template"]
        self.accounts = accounts
        self.password_hash = bcrypt.hashpw(str(auth["password"]).encode(), bcrypt.gensalt(rounds=int(srv["password_hash_rounds"])))
        self.profiles: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.attempts: dict[str, deque] = defaultdict(deque)
        e = srv["endpoints"]
        self.catalog = [{"sku": f"SKU-{i:05d}", "price": round(random.uniform(2, 500), 2), "stock": random.randint(5, 200)}
                        for i in range(int(e["catalog_items"]))]
        self.ledger: deque = deque(maxlen=int(e["ledger_kept"]))
        self.posts: deque = deque(maxlen=int(e["posts_kept"]))
        self.iot_buffer: list = []
        self.iot_flushed: deque = deque(maxlen=1000)
        self.ledger_lock = asyncio.Lock()
        self.inventory_lock = asyncio.Lock()

    def username_valid(self, name: str) -> bool:
        # accept exactly the names produced by the template for 0..accounts-1
        pre, _, post = self.template.partition("{n}")
        if not (name.startswith(pre) and name.endswith(post)):
            return False
        mid = name[len(pre):len(name) - len(post) if post else None]
        return mid.isdigit() and int(mid) < self.accounts


def create_app(cfg: dict, accounts: int, data_dir: Path | None = None, log_file: Path | None = None,
               grpc_port: int | None = None) -> FastAPI:
    srv, auth = cfg["server"], cfg["auth"]
    e = srv["endpoints"]
    rl = srv["rate_limit"]
    log = _logger(log_file)
    data_dir = Path(data_dir) if data_dir else Path.cwd() / "dcsim-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    reg = CollectorRegistry()
    m_req = Counter("http_requests_total", "Requests", ["endpoint", "method", "status_code"], registry=reg)
    m_lat = Histogram("http_request_duration_ms", "Latency ms", ["endpoint"], registry=reg,
                      buckets=[5, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000])
    m_sess = Gauge("active_sessions_total", "Active sessions", registry=reg)
    m_login_ok = Counter("login_success_total", "Successful logins", registry=reg)
    m_login_fail = Counter("login_failure_total", "Failed logins", ["reason"], registry=reg)
    m_expired = Counter("sessions_expired_total", "Sessions removed by the expiry sweeper", registry=reg)
    started = time.time()
    store = Store(cfg, accounts)
    stream_file = data_dir / "stream.bin"
    storage_file = data_dir / "random-io.bin"
    upload_dir = data_dir / "uploads"
    upload_dir.mkdir(exist_ok=True)
    counters = {"uploaded_bytes": 0, "streamed_bytes": 0, "expired": 0, "last_sweep_removed": 0}

    def ensure_stream_file():
        size = int(e["stream_file_mb"]) * 2**20
        if not stream_file.exists() or stream_file.stat().st_size != size:
            with open(stream_file, "wb") as f:
                left = size
                while left > 0:
                    n = min(left, 2**20)
                    f.write(os.urandom(n))
                    left -= n

    async def sweeper():
        ttl, every = float(srv["session_ttl_sec"]), float(srv["session_sweep_interval_sec"])
        while True:
            await asyncio.sleep(every)
            now = time.time()
            dead = [t for t, s in list(store.sessions.items()) if now - s["last_seen"] > ttl]
            for t in dead:
                store.sessions.pop(t, None)
            if dead:
                m_expired.inc(len(dead))
                counters["expired"] += len(dead)
                log.info("sessions expired", extra={"fields": {"count": len(dead)}})
            counters["last_sweep_removed"] = len(dead)
            m_sess.set(len(store.sessions))

    def ensure_storage_file():
        size = int(e["storage_file_mb"]) * 2**20
        if not storage_file.exists() or storage_file.stat().st_size != size:
            with open(storage_file, "wb") as f:
                f.truncate(size)

    m_grpc = Counter("grpc_requests_total", "gRPC calls", ["method", "code"], registry=reg)
    m_grpc_lat = Histogram("grpc_request_duration_ms", "gRPC latency ms", ["method"], registry=reg,
                           buckets=[5, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000])

    def grpc_user(context) -> str | None:
        md = dict(context.invocation_metadata() or ())
        tok = str(md.get("authorization", ""))
        pre = auth["token_prefix"]
        tok = tok[len(pre):] if pre and tok.startswith(pre) else tok
        sess = store.sessions.get(tok)
        if sess:
            sess["last_seen"] = time.time()
            return sess["user"]
        return None

    def grpc_method(name, fn):
        async def handler(request: bytes, context):
            import grpc
            t0 = time.perf_counter()
            code = "OK"
            try:
                user = grpc_user(context)
                if not user:
                    code = "UNAUTHENTICATED"
                    await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or expired session")
                body = json.loads(request or b"{}")
                out = await fn(user, body)
                return json.dumps(out).encode()
            except Exception as ex:
                if code == "OK":
                    code = "INTERNAL" if not hasattr(ex, "code") else "ABORTED"
                raise
            finally:
                m_grpc.labels(name, code).inc()
                m_grpc_lat.labels(name).observe((time.perf_counter() - t0) * 1000)
        return handler

    async def g_getdata(user, body):
        await asyncio.sleep(_rand(e["api_data_delay_ms"]) / 1000)
        n = _randint(e["api_data_records"])
        return {"count": n, "values": [round(random.gauss(50, 10), 2) for _ in range(n)]}

    async def g_ingest(user, body):
        n = max(1, min(int(body.get("readings", 1)), 1000))
        store.iot_buffer.extend(random.gauss(40, 8) for _ in range(n))
        if len(store.iot_buffer) >= int(e["iot_buffer_flush_every"]):
            buf, store.iot_buffer = store.iot_buffer, []
            store.iot_flushed.append({"n": len(buf), "mean": statistics.fmean(buf), "max": max(buf), "ts": time.time()})
        return {"accepted": n}

    async def g_transfer(user, body):
        sig = await run_in_threadpool(_work, int(e["transaction_cpu_iterations"]), user + str(time.time()))
        async with store.ledger_lock:
            await asyncio.sleep(_rand(e["transaction_lock_hold_ms"]) / 1000)
            store.ledger.append({"user": user, "amount": float(body.get("amount", 1)), "sig": sig, "ts": time.time()})
        return {"status": "accepted", "signature": sig}

    async def start_grpc():
        import grpc
        server = grpc.aio.server()
        services = {"dcsim.Telemetry": {"GetData": g_getdata, "Ingest": g_ingest}, "dcsim.Payments": {"Transfer": g_transfer}}
        for svc, methods in services.items():
            handlers = {m: grpc.unary_unary_rpc_method_handler(grpc_method(f"{svc}/{m}", fn)) for m, fn in methods.items()}
            server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(svc, handlers),))
        host = cfg["target"]["host"] or "127.0.0.1"
        server.add_insecure_port(f"{'[::]' if host in ('0.0.0.0', '') else host}:{grpc_port}")
        await server.start()
        log.info("grpc started", extra={"fields": {"port": grpc_port}})
        return server

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(ensure_stream_file)
        await run_in_threadpool(ensure_storage_file)
        task = asyncio.create_task(sweeper())
        gserver = await start_grpc() if grpc_port and srv.get("grpc_enabled", True) else None
        log.info("server started", extra={"fields": {"accounts": accounts}})
        yield
        task.cancel()
        if gserver:
            await gserver.stop(1)
        log.info("server stopped")

    app = FastAPI(title="datacenter-sim target", version="2.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def metrics_mw(request: Request, call_next):
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            ms = (time.perf_counter() - t0) * 1000
            path = request.url.path
            m_req.labels(path, request.method, str(status)).inc()
            m_lat.labels(path).observe(ms)
            if srv.get("log_requests") or status >= 500:
                log.info("request", extra={"fields": {"path": path, "method": request.method, "status": status, "ms": round(ms, 2)}})

    def current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        if cred is None:
            raise HTTPException(401, "Missing session token")
        s = store.sessions.get(cred.credentials)
        if not s:
            raise HTTPException(401, "Invalid or expired session")
        s["last_seen"] = time.time()
        return s["user"]

    def rate_limited(key: str) -> bool:
        if not rl["enabled"]:
            return False
        now, q = time.time(), store.attempts[key]
        while q and now - q[0] >= rl["window_sec"]:
            q.popleft()
        if len(q) >= rl["max_attempts"]:
            return True
        q.append(now)
        return False

    # ── auth ────────────────────────────────────────────────────────────
    @app.post(auth["login_path"])
    async def login(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "Body must be JSON")
        user = str(body.get(auth["username_field"], ""))
        pw = str(body.get(auth["password_field"], "")).encode()
        key = user if rl["key"] == "username" else (request.client.host if request.client else "unknown")
        if rate_limited(key):
            m_login_fail.labels("rate_limited").inc()
            raise HTTPException(429, f"Too many login attempts - limit is {rl['max_attempts']} per {rl['window_sec']}s")
        if srv["hash_in_threadpool"]:
            ok = await run_in_threadpool(bcrypt.checkpw, pw, store.password_hash)
        else:
            ok = bcrypt.checkpw(pw, store.password_hash)  # blocks the event loop on purpose
        if not ok or not store.username_valid(user):
            m_login_fail.labels("bad_credentials").inc()
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_hex(32)
        now = time.time()
        store.sessions[token] = {"user": user, "created": now, "last_seen": now}
        m_sess.set(len(store.sessions))
        m_login_ok.inc()
        return {auth["token_field"]: token, "username": user, "message": "Login successful"}

    @app.post(auth["logout_path"])
    async def logout(cred: HTTPAuthorizationCredentials | None = Depends(bearer), user: str = Depends(current_user)):
        store.sessions.pop(cred.credentials, None)
        m_sess.set(len(store.sessions))
        return {"message": "Logged out"}

    # ── generic ─────────────────────────────────────────────────────────
    @app.get("/me")
    async def me(user: str = Depends(current_user)):
        p = store.profiles.get(user, {})
        return {"username": user, "display_name": p.get("display_name", user), "active_sessions": len(store.sessions)}

    @app.post("/profile/update")
    async def profile_update(request: Request, user: str = Depends(current_user)):
        body = await request.json() if (await request.body()) else {}
        await asyncio.sleep(_rand(e["profile_update_delay_ms"]) / 1000)
        store.profiles[user] = {**store.profiles.get(user, {}), **{k: v for k, v in body.items() if isinstance(v, (str, int, float))}}
        return {"message": "Profile updated", "username": user}

    @app.get("/api/data")
    async def api_data(user: str = Depends(current_user)):
        await asyncio.sleep(_rand(e["api_data_delay_ms"]) / 1000)
        n = _randint(e["api_data_records"])
        now = datetime.now(timezone.utc).isoformat()
        cats = ["cpu", "mem", "disk", "net", "io", "cache"]
        data = [{"id": i, "metric": f"{cats[i % len(cats)]}.node-{i % 8:02d}", "value": round(random.gauss(50, 10), 2), "ts": now}
                for i in range(n)]
        return {"data": data, "count": n, "generated_at": now}

    # ── e-commerce ──────────────────────────────────────────────────────
    @app.get("/catalog")
    async def catalog(user: str = Depends(current_user)):
        k = min(_randint(e["catalog_page_size"]), len(store.catalog))
        return {"items": random.sample(store.catalog, k) if k else []}

    @app.post("/cart/checkout")
    async def checkout(request: Request, user: str = Depends(current_user)):
        body = await request.json() if (await request.body()) else {}
        items = max(1, int(body.get("items", 1)))
        async with store.inventory_lock:
            picked = random.sample(store.catalog, min(items, len(store.catalog)))
            for it in picked:
                if it["stock"] <= 0:
                    it["stock"] = random.randint(5, 200)  # restock so the test keeps running
                it["stock"] -= 1
            await asyncio.sleep(_rand(e["transaction_lock_hold_ms"]) / 1000)
        total = round(sum(i["price"] for i in picked), 2)
        return {"order_id": uuid.uuid4().hex[:12], "items": len(picked), "total": total}

    @app.post("/inventory/update")
    async def inventory_update(request: Request, user: str = Depends(current_user)):
        body = await request.json() if (await request.body()) else {}
        async with store.inventory_lock:
            it = random.choice(store.catalog)
            it["stock"] = max(0, it["stock"] + int(body.get("delta", -1)))
        return {"sku": it["sku"], "stock": it["stock"]}

    # ── banking ─────────────────────────────────────────────────────────
    def _work(iterations: int, seed: str) -> str:
        h = seed.encode()
        for _ in range(iterations):
            h = hashlib.sha256(h).digest()
        return h.hex()[:16]

    @app.post("/transactions")
    async def transaction(request: Request, user: str = Depends(current_user)):
        body = await request.json() if (await request.body()) else {}
        sig = await run_in_threadpool(_work, int(e["transaction_cpu_iterations"]), user + str(time.time()))
        async with store.ledger_lock:
            await asyncio.sleep(_rand(e["transaction_lock_hold_ms"]) / 1000)
            store.ledger.append({"user": user, "amount": float(body.get("amount", 1)), "sig": sig, "ts": time.time()})
        return {"status": "accepted", "signature": sig}

    @app.get("/transactions/history")
    async def history(limit: int = 20, user: str = Depends(current_user)):
        limit = max(1, min(limit, len(store.ledger) or 1))
        rows = list(store.ledger)[-limit:]
        return {"count": len(rows), "total": round(sum(r["amount"] for r in rows), 2), "rows": rows[-20:]}

    # ── storage ─────────────────────────────────────────────────────────
    @app.post("/batch/upload")
    async def upload(request: Request, user: str = Depends(current_user)):
        body = await request.body()
        if len(body) > int(e["upload_max_kb"]) * 1024:
            raise HTTPException(413, f"Upload larger than {e['upload_max_kb']} KB")

        def write():
            p = upload_dir / f"{uuid.uuid4().hex}.bin"
            with open(p, "wb") as f:
                f.write(body)
                if e["upload_fsync"]:
                    f.flush()
                    os.fsync(f.fileno())
            p.unlink(missing_ok=True)

        await run_in_threadpool(write)
        counters["uploaded_bytes"] += len(body)
        return {"stored_bytes": len(body)}

    @app.get("/media/stream")
    async def stream(size_kb: int = 1024, user: str = Depends(current_user)):
        total = stream_file.stat().st_size
        want = max(1, min(size_kb * 1024, total))
        chunk = int(e["stream_chunk_kb"]) * 1024
        start = random.randint(0, max(0, total - want))

        def gen():
            sent = 0
            with open(stream_file, "rb") as f:
                f.seek(start)
                while sent < want:
                    b = f.read(min(chunk, want - sent))
                    if not b:
                        break
                    sent += len(b)
                    yield b
            counters["streamed_bytes"] += sent

        return StreamingResponse(gen(), media_type="application/octet-stream", headers={"Content-Length": str(want)})

    # ── streaming / social ──────────────────────────────────────────────
    @app.get("/recommendations")
    async def recommendations(depth: int = 1, user: str = Depends(current_user)):
        sig = await run_in_threadpool(_work, int(e["recommendation_cpu_iterations"]) * max(1, min(depth, 10)), user)
        return {"items": [f"title-{int(sig[i:i + 2], 16)}" for i in range(0, 16, 2)]}

    @app.get("/feed")
    async def feed(user: str = Depends(current_user)):
        n = _randint(e["feed_items"])
        recent = list(store.posts)[-n:]
        return {"items": recent, "count": len(recent)}

    @app.post("/posts")
    async def post(request: Request, user: str = Depends(current_user)):
        body = await request.json() if (await request.body()) else {}
        store.posts.append({"user": user, "text": str(body.get("text", ""))[:280], "ts": time.time()})
        return {"status": "posted"}

    # ── IoT ─────────────────────────────────────────────────────────────
    @app.post("/iot/ingest")
    async def ingest(request: Request, user: str = Depends(current_user)):
        body = await request.json() if (await request.body()) else {}
        n = max(1, min(int(body.get("readings", 1)), 1000))
        store.iot_buffer.extend(random.gauss(40, 8) for _ in range(n))
        flushed = False
        if len(store.iot_buffer) >= int(e["iot_buffer_flush_every"]):
            buf, store.iot_buffer = store.iot_buffer, []
            store.iot_flushed.append({"n": len(buf), "mean": statistics.fmean(buf), "max": max(buf), "ts": time.time()})
            flushed = True
        return {"accepted": n, "flushed": flushed}

    @app.get("/analytics/aggregate")
    async def aggregate(user: str = Depends(current_user)):
        def calc():
            vals = [b["mean"] for b in store.iot_flushed] + store.iot_buffer[-5000:]
            if len(vals) < 2:
                return {"samples": len(vals)}
            s = sorted(vals)
            return {"samples": len(vals), "mean": statistics.fmean(vals), "stdev": statistics.pstdev(vals),
                    "p95": s[int(0.95 * (len(s) - 1))]}
        return await run_in_threadpool(calc)

    # ── browser page loads ──────────────────────────────────────────────
    page_cache: dict = {}

    @app.get("/web/home")
    async def web_home(user: str = Depends(current_user)):
        size = int(e["page_kb"]) * 1024
        if size not in page_cache:
            filler = "".join(random.choice("abcdefghij klmnop") for _ in range(size))
            page_cache[size] = f"<!doctype html><html><head><title>Home</title></head><body><p>{filler}</p></body></html>"
        return Response(page_cache[size], media_type="text/html")

    asset_cache: dict = {}

    @app.get("/static/{name}")
    async def static_asset(name: str):
        if name not in asset_cache:
            if len(asset_cache) > 200:
                asset_cache.clear()
            asset_cache[name] = os.urandom(_randint(e["asset_kb"]) * 1024)
        kind = "text/css" if name.endswith(".css") else "application/javascript" if name.endswith(".js") else "image/png"
        return Response(asset_cache[name], media_type=kind, headers={"Cache-Control": "max-age=60"})

    # ── small random disk I/O (peak-hour OLTP-style storage load) ─────────
    @app.api_route("/storage/random", methods=["GET", "POST"])
    async def storage_random(request: Request, write_ratio: float = 0.3, user: str = Depends(current_user)):
        block = int(e["storage_block_kb"]) * 1024
        ops = _randint(e["storage_ops"])
        size = storage_file.stat().st_size
        blocks = max(1, size // block)
        writes = request.method == "POST"

        def io():
            done_r = done_w = 0
            fd = os.open(storage_file, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                for _ in range(ops):
                    off = random.randrange(blocks) * block
                    os.lseek(fd, off, os.SEEK_SET)
                    if writes and random.random() < max(0.0, min(1.0, write_ratio)):
                        os.write(fd, os.urandom(block))
                        done_w += 1
                    else:
                        os.read(fd, block)
                        done_r += 1
                if done_w and e["storage_fsync"]:
                    os.fsync(fd)
            finally:
                os.close(fd)
            return done_r, done_w

        r, w = await run_in_threadpool(io)
        counters["random_reads"] = counters.get("random_reads", 0) + r
        counters["random_writes"] = counters.get("random_writes", 0) + w
        return {"reads": r, "writes": w, "block_kb": block // 1024}

    # ── test control ────────────────────────────────────────────────────
    @app.post("/admin/seed")
    async def admin_seed(request: Request):
        body = await request.json()
        random.seed(int(body.get("seed", 0)))
        return {"seeded": True}

    # ── observability ───────────────────────────────────────────────────
    @app.get(cfg["target"]["health_path"])
    async def health():
        return {"status": "ok", "active_sessions": len(store.sessions), "uptime_seconds": round(time.time() - started, 1)}

    @app.get("/stats")
    async def stats():
        return {"status": "ok", "accounts": store.accounts, "active_sessions": len(store.sessions),
                "uptime_seconds": round(time.time() - started, 1), "pid": os.getpid(),
                "sessions_expired": counters["expired"], "uploaded_bytes": counters["uploaded_bytes"],
                "streamed_bytes": counters["streamed_bytes"], "ledger_rows": len(store.ledger),
                "posts": len(store.posts), "iot_buffered": len(store.iot_buffer),
                "random_reads": counters.get("random_reads", 0), "random_writes": counters.get("random_writes", 0),
                "grpc_port": grpc_port}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    @app.exception_handler(Exception)
    async def crash_handler(request: Request, exc: Exception):
        log.error("unhandled error", extra={"fields": {"path": request.url.path, "error": repr(exc)}})
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    app.state.store = store
    return app
