import asyncio
import random
import math
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.models.schemas import APIDataResponse
from app.core.security import validate_token
from app.core.logger import get_logger

router = APIRouter()
logger = get_logger()
bearer = HTTPBearer()

_METRIC_CATEGORIES = ["cpu", "mem", "disk", "net", "gpu", "temp", "io", "cache"]
_METRIC_UNITS = {"cpu": "%", "mem": "MB", "disk": "MB/s", "net": "Mbps", "gpu": "%", "temp": "C", "io": "IOPS", "cache": "%"}
_BASELINES = {"cpu": 45, "mem": 6200, "disk": 120, "net": 850, "gpu": 30, "temp": 62, "io": 4200, "cache": 78}

def _make_metric(idx, username):
    category = _METRIC_CATEGORIES[idx % len(_METRIC_CATEGORIES)]
    unit = _METRIC_UNITS[category]
    baseline = _BASELINES[category]
    drift = math.sin(datetime.now().timestamp() / 60 + idx) * baseline * 0.15
    jitter = random.gauss(0, baseline * 0.08)
    value = max(0.0, round(baseline + drift + jitter, 2))
    node = f"node-{(idx % 4) + 1:02d}"
    rack = f"rack-{(idx // 4) % 3 + 1}"
    return {"id": idx, "metric": f"{category}.{node}", "value": value, "unit": unit, "node": node, "rack": rack, "status": "warn" if value > baseline * 1.3 else "ok", "ts": datetime.now(timezone.utc).isoformat()}

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    token = credentials.credentials
    username = validate_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return username

@router.get("/api/data", response_model=APIDataResponse)
async def get_api_data(username: str = Depends(get_current_user)):
    await asyncio.sleep(random.uniform(0.01, 0.1))
    count = random.randint(45, 60)
    data = [_make_metric(i, username) for i in range(count)]
    logger.info("API data request", extra={"endpoint": "/api/data", "user": username, "records": count})
    return APIDataResponse(data=data, count=len(data), generated_at=datetime.now(timezone.utc).isoformat())
