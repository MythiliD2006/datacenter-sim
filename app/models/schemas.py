from pydantic import BaseModel
from typing import Optional

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    token: str
    username: str
    message: str

class ProfileResponse(BaseModel):
    username: str
    role: str
    active_sessions: int

class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    preferences: Optional[dict] = None

class ProfileUpdateResponse(BaseModel):
    username: str
    message: str
    updated_fields: list[str]

class APIDataResponse(BaseModel):
    data: list[dict]
    count: int
    generated_at: str

class HealthResponse(BaseModel):
    status: str
    active_sessions: int
    uptime_seconds: float