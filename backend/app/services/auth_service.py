import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy.orm import Session

from ..clock import utc_now
from ..config import settings
from ..models import User, UserSession

SESSION_COOKIE = "bm_session"
SESSION_LIFETIME = timedelta(days=7)
SECURE_COOKIES = settings.secure_cookies
MAX_FAILED_LOGINS = 5
FAILED_LOGIN_WINDOW_SECONDS = 15 * 60

_hasher = PasswordHasher()
# Checked for unknown emails so response time doesn't reveal which accounts exist.
_DUMMY_HASH = _hasher.hash("buildmart-dummy-password")
# In-memory, so limits reset on restart and aren't shared across worker processes.
_failed_logins: dict[str, deque[float]] = defaultdict(deque)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(user: User | None, password: str) -> bool:
    if user is None or not user.password_hash:
        try:
            _hasher.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        return False
    try:
        _hasher.verify(user.password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    return True


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = utc_now()
    db.add(UserSession(user_id=user.id, token_hash=_hash_token(token), created_at=now, expires_at=now + SESSION_LIFETIME))
    return token


def get_session_user(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    session = db.query(UserSession).filter(UserSession.token_hash == _hash_token(token)).first()
    if session is None:
        return None
    if session.expires_at <= utc_now():
        db.delete(session)
        db.commit()
        return None
    return db.get(User, session.user_id)


def delete_session(db: Session, token: str | None) -> None:
    if token:
        db.query(UserSession).filter(UserSession.token_hash == _hash_token(token)).delete()


def is_login_rate_limited(email: str) -> bool:
    attempts = _failed_logins[email]
    cutoff = time.monotonic() - FAILED_LOGIN_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= MAX_FAILED_LOGINS


def record_failed_login(email: str) -> None:
    _failed_logins[email].append(time.monotonic())


def clear_failed_logins(email: str) -> None:
    _failed_logins.pop(email, None)
