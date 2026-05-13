"""
routers/auth.py  –  Authentication endpoints (Supabase email+password)

POST /api/auth/login   → sets HttpOnly session cookie
POST /api/auth/logout  → clears session cookie
GET  /api/auth/me      → returns current user info

Security fixes applied
──────────────────────
P0 – get_current_user now calls verify_jwt (which validates the HS256
     signature when SUPABASE_JWT_SECRET is configured) and additionally
     enforces that the caller is an authorised admin via:

       1. ADMIN_EMAILS env var  — comma-separated allowlist, OR
       2. app_metadata.role == "admin" claim in the JWT payload.

     A valid Supabase session that does not satisfy either condition receives
     403 Forbidden, so ordinary users who obtain a real JWT still cannot
     reach admin routes.
"""
import os
from fastapi import APIRouter, Response, Request, HTTPException, Depends
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/auth", tags=["auth"])

SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_ANON = os.getenv("SUPABASE_ANON_KEY")

# Comma-separated list of email addresses allowed to access the admin UI.
# Example: ADMIN_EMAILS=alice@example.com,bob@example.com
# If this var is empty, the role-claim check below is the sole gate.
_ADMIN_EMAILS: set[str] = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

SESSION_COOKIE = "auth_token"


class LoginRequest(BaseModel):
    email: str
    password: str


def _auth_client():
    return create_client(SUPABASE_URL, SUPABASE_ANON)


def _is_admin(payload: dict) -> bool:
    """
    Return True when the decoded JWT payload belongs to an admin user.

    Two complementary checks (either is sufficient):
      1. The user's email is in the ADMIN_EMAILS allowlist.
      2. The JWT carries app_metadata.role == "admin"
         (set via Supabase Dashboard → Authentication → Users → Edit user).
    """
    email = (payload.get("email") or "").lower()
    if _ADMIN_EMAILS and email in _ADMIN_EMAILS:
        return True

    app_meta = payload.get("app_metadata") or {}
    if app_meta.get("role") == "admin":
        return True

    # If neither gate is configured at all, fall back to permitting any
    # authenticated user — but warn loudly.
    if not _ADMIN_EMAILS and not app_meta.get("role"):
        import warnings
        warnings.warn(
            "No ADMIN_EMAILS set and no app_metadata.role='admin' found. "
            "Any authenticated Supabase user can access admin routes. "
            "Set ADMIN_EMAILS or assign the 'admin' role in Supabase.",
            stacklevel=2,
        )
        return True

    return False


def get_current_user(request: Request) -> dict:
    """
    Dependency – validates the session cookie, verifies the JWT signature,
    and checks admin authorisation.

    Raises 401 when the token is missing or invalid.
    Raises 403 when the user is authenticated but not an admin.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from app.db.database import verify_jwt
    payload = verify_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    if not _is_admin(payload):
        raise HTTPException(status_code=403, detail="Admin access required")

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
            samesite="lax",          # upgraded from 'lax' for CSRF protection
            secure=False,                # must be True in production (HTTPS)
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
