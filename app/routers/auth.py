from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.models.schemas import LoginRequest, LoginResponse
from app.db.fake_db import get_user, verify_password, session_count
from app.core.security import create_user_session, invalidate_token, validate_token
from app.core.metrics import ACTIVE_SESSIONS, LOGIN_SUCCESS, LOGIN_FAILURE
from app.core.logger import get_logger

router = APIRouter()
logger = get_logger()
bearer = HTTPBearer()


# ── POST /login ───────────────────────────────────────────────────────────────
@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """
    Authenticate a user and return a session token.
    Used by all three user types: WebUser, MobileUser, APIUser.
    """
    user = get_user(request.username)

    if not user or not verify_password(request.password, user["hashed_password"]):
        LOGIN_FAILURE.inc()
        logger.warning("Login failed", extra={
            "endpoint": "/login",
            "user": request.username
        })
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_user_session(request.username)
    ACTIVE_SESSIONS.set(session_count())
    LOGIN_SUCCESS.inc()

    logger.info("Login success", extra={
        "endpoint": "/login",
        "user": request.username,
        "user_type": user["role"]
    })

    return LoginResponse(
        token=token,
        username=request.username,
        message="Login successful"
    )


# ── POST /logout ──────────────────────────────────────────────────────────────
@router.post("/logout")
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
):
    """
    End a session and invalidate the token.
    Used primarily by WebUser after completing their session lifecycle.
    """
    token = credentials.credentials  # HTTPBearer strips "Bearer " automatically

    username = validate_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    invalidate_token(token)
    ACTIVE_SESSIONS.set(session_count())

    logger.info("Logout", extra={
        "endpoint": "/logout",
        "user": username
    })

    return {"message": "Logged out successfully"}