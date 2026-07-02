import asyncio
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.models.schemas import (
    ProfileResponse,
    ProfileUpdateRequest,
    ProfileUpdateResponse
)
from app.db.fake_db import get_user, session_count
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


# ── GET /me ───────────────────────────────────────────────────────────────────
@router.get("/me", response_model=ProfileResponse)
async def get_profile(username: str = Depends(get_current_user)):
    """
    Returns the current user's profile.
    Light read — used by WebUser (light) and MobileUser (light + heavy polling).
    """
    user = get_user(username)

    logger.info("Profile read", extra={
        "endpoint": "/me",
        "user": username,
        "user_type": user["role"]
    })

    return ProfileResponse(
        username=username,
        role=user["role"],
        active_sessions=session_count()
    )


# ── POST /profile/update ──────────────────────────────────────────────────────
@router.post("/profile/update", response_model=ProfileUpdateResponse)
async def update_profile(
    body: ProfileUpdateRequest,
    username: str = Depends(get_current_user)
):
    """
    Updates the current user's profile.
    Write-heavy endpoint — simulates DB contention.
    Used by WebUser (heavy) and MobileUser (heavy) in their loops.
    The 50ms sleep simulates a real DB write under load.
    """
    # simulate write delay — represents DB write contention under load
    await asyncio.sleep(0.05)

    updated_fields = [k for k, v in body.model_dump().items() if v is not None]

    logger.info("Profile update", extra={
        "endpoint": "/profile/update",
        "user": username,
        "updated_fields": updated_fields
    })

    return ProfileUpdateResponse(
        username=username,
        message="Profile updated",
        updated_fields=updated_fields
    )