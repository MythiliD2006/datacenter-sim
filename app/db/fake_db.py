from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# pre-seeded users for simulation
# Locust will log in as these users
USERS = {
    "webuser":    {"username": "webuser",    "hashed_password": pwd_context.hash("pass"), "role": "web"},
    "mobileuser": {"username": "mobileuser", "hashed_password": pwd_context.hash("pass"), "role": "mobile"},
    "apiuser":    {"username": "apiuser",    "hashed_password": pwd_context.hash("pass"), "role": "api"},
}

# in-memory session store
# key = token, value = username
SESSIONS: dict[str, str] = {}

def get_user(username: str):
    return USERS.get(username)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_session(token: str, username: str):
    SESSIONS[token] = username

def get_session(token: str):
    return SESSIONS.get(token)

def delete_session(token: str):
    SESSIONS.pop(token, None)

def session_count() -> int:
    return len(SESSIONS)