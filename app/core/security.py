import secrets
from app.db.fake_db import create_session, get_session, delete_session

def generate_token() -> str:
    return secrets.token_hex(32)

def create_user_session(username: str) -> str:
    token = generate_token()
    create_session(token, username)
    return token

def validate_token(token: str) -> str | None:
    return get_session(token)

def invalidate_token(token: str):
    delete_session(token)