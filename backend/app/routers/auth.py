from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..clock import utc_now
from ..database import get_db
from ..dependencies import get_optional_user
from ..models import User
from ..schemas import AuthResponse, LoginRequest, RegisterRequest, UserResponse
from ..services.auth_service import (
    SECURE_COOKIES,
    SESSION_COOKIE,
    SESSION_LIFETIME,
    clear_failed_logins,
    create_session,
    delete_session,
    hash_password,
    is_login_rate_limited,
    record_failed_login,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
        path="/",
    )


def auth_response(user: User | None) -> AuthResponse:
    return AuthResponse(user=UserResponse.model_validate(user, from_attributes=True) if user else None)


@router.post("/register", response_model=AuthResponse, status_code=201)
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    user = User(
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        created_at=utc_now(),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    token = create_session(db, user)
    db.commit()
    set_session_cookie(response, token)
    return auth_response(user)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    if is_login_rate_limited(payload.email):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Please try again in 15 minutes.")
    user = db.query(User).filter(User.email == payload.email).first()
    if not verify_password(user, payload.password):
        record_failed_login(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    clear_failed_logins(payload.email)
    token = create_session(db, user)
    db.commit()
    set_session_cookie(response, token)
    return auth_response(user)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
):
    delete_session(db, session_token)
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=SECURE_COOKIES)


@router.get("/me", response_model=AuthResponse)
def current_user(user: User | None = Depends(get_optional_user)):
    return auth_response(user)
