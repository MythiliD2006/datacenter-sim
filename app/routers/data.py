import asyncio
import random
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.models.schemas import APIDataResponse
from app.core.security import validate_token
from app.core.logger import get_logger

router = APIRouter()
logger = get_logger()
bearer = HTTPBearer()


# ── shared dependency: validates token and returns username ───────────────────
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
) -> str:
    """
    Extracts and validates the Bearer token from the Authorization header.
    HTTPBearer automatically strips the 'Bearer ' prefix.
    Raises 401 if the token is missing, invalid, or expired.
    """
    token = credentials.credentials
    username = validate_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return username


# ── GET /api/data ─────────────────────────────────────────────────────────────
@router.get("/api/data", response_model=APIDataResponse)
async def get_api_data(username: str = Depends(get_current_user)):
    """
    Sustained machine traffic endpoint.
    Heavy APIUser hammers this continuously — primary source of
    cross-interface CPU and session store contention.
    The random sleep (10–100ms) simulates real compute/IO work under load.
    """
    # simulate variable compute or IO work
    # this is what causes CPU contention when APIUser (heavy) hammers it
    await asyncio.sleep(random.uniform(0.01, 0.1))

    data = [
        {"id": i, "value": round(random.random(), 4), "tag": f"metric_{i}"}
        for i in range(50)
    ]

    logger.info("API data request", extra={
        "endpoint": "/api/data",
        "user": username
    })

    return APIDataResponse(
        data=data,
        count=len(data),
        generated_at=datetime.now(timezone.utc).isoformat()
    )