import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from app.routers import auth, profile, data
from app.core.metrics import REQUEST_COUNT, REQUEST_LATENCY
from app.core.logger import get_logger
from app.models.schemas import HealthResponse

logger = get_logger()
START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("datacenter-sim FastAPI server starting up")
    yield
    logger.info("datacenter-sim FastAPI server shutting down")


app = FastAPI(
    title="datacenter-sim",
    description="Login load simulation target server",
    version="1.0.0",
    lifespan=lifespan
)


# ── middleware: tracks latency + request count for every endpoint ──────────────
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    latency_ms = (time.time() - start) * 1000

    REQUEST_COUNT.labels(
        endpoint=request.url.path,
        method=request.method,
        status_code=str(response.status_code)
    ).inc()

    REQUEST_LATENCY.labels(
        endpoint=request.url.path
    ).observe(latency_ms)

    logger.info("request", extra={
        "endpoint": request.url.path,
        "method": request.method,
        "status_code": response.status_code,
        "latency_ms": round(latency_ms, 2)
    })

    return response


# ── routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(data.router)


# ── health check ──────────────────────────────────────────────────────────────
@app.get("/health", response_model=HealthResponse)
async def health():
    from app.db.fake_db import session_count
    return HealthResponse(
        status="ok",
        active_sessions=session_count(),
        uptime_seconds=round(time.time() - START_TIME, 2)
    )


# ── prometheus metrics ────────────────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )


# ── custom openapi: adds BearerAuth so Swagger shows the Authorize button ─────
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title="datacenter-sim",
        version="1.0.0",
        description="Login load simulation target server",
        routes=app.routes,
    )

    # register the Bearer security scheme
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer"
        }
    }

    # apply it to every endpoint automatically
    for path in schema["paths"].values():
        for method in path.values():
            method["security"] = [{"BearerAuth": []}]

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi