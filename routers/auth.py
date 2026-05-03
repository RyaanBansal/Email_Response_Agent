"""
routers/auth.py  –  Authentication endpoints (Supabase email+password)

POST /api/auth/login   → sets HttpOnly session cookie
POST /api/auth/logout  → clears session cookie
GET  /api/auth/me      → returns current user info
"""
import os
from fastapi import APIRouter, Response, Request, HTTPException, Depends
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/auth", tags=["auth"])

SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_ANON = os.getenv("SUPABASE_ANON_KEY")

SESSION_COOKIE = "auth_token"


class LoginRequest(BaseModel):
    email: str
    password: str


def _auth_client():
    return create_client(SUPABASE_URL, SUPABASE_ANON)


def get_current_user(request: Request) -> dict:
    """Dependency – validates the session cookie and returns the user payload."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.db.database import verify_jwt
    payload = verify_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return payload


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if not body.email or not body.password:
        raise HTTPException(status_code=400, detail="Email and password required")
    try:
        client = _auth_client()
        resp = client.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
        token = resp.session.access_token
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,   # set True in prod behind HTTPS
            max_age=60 * 60 * 8,
        )
        return {"user_email": resp.user.email, "user_id": str(resp.user.id)}
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Login failed: {exc}")


@router.post("/logout")
def logout(response: Response, _user=Depends(get_current_user)):
    try:
        _auth_client().auth.sign_out()
    except Exception:
        pass
    response.delete_cookie(SESSION_COOKIE)
    return {"detail": "Signed out"}


@router.get("/me")
def me(user=Depends(get_current_user)):
    return user
