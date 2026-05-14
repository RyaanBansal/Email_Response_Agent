"""
routers/auth.py  –  Authentication endpoints (Supabase email+password)

POST /api/auth/login   → sets HttpOnly session cookie
POST /api/auth/logout  → clears session cookie
GET  /api/auth/me      → returns current user info

Security fixes applied
──────────────────────
P0 – get_current_user previously decoded the JWT locally with
     verify_signature=False when SUPABASE_JWT_PUBLIC_KEY was absent,
     meaning any structurally-valid JWT string was accepted regardless
     of whether Supabase actually issued it or whether the session was
     still active.

     Fix: always re-validate the cookie token against Supabase via
     client.auth.get_user(token).  This is a live network call that:
       • verifies the token was issued by this Supabase project,
       • confirms the session has not been revoked,
       • returns the canonical user object (email, id, app_metadata).
     verify_jwt / SUPABASE_JWT_PUBLIC_KEY is no longer used here.

     Admin check is kept unchanged: ADMIN_EMAILS allowlist OR
     app_metadata.role == "admin" in the Supabase user record.
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


def _is_admin(email: str, app_metadata: dict) -> bool:
    """
    Return True when the user belongs to an admin account.

    Two complementary checks (either is sufficient):
      1. The user's email is in the ADMIN_EMAILS allowlist.
      2. The Supabase user record carries app_metadata.role == "admin"
         (set via Supabase Dashboard → Authentication → Users → Edit user).

    If neither gate is configured at all, any authenticated user is
    permitted with a loud warning — identical to the previous behaviour
    so existing single-admin deployments are not broken.
    """
    email = (email or "").lower()
    if _ADMIN_EMAILS and email in _ADMIN_EMAILS:
        return True

    if (app_metadata or {}).get("role") == "admin":
        return True

    # Fallback: if the operator has configured neither guard, allow any
    # authenticated Supabase user but warn loudly.
    if not _ADMIN_EMAILS and not (app_metadata or {}).get("role"):
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
    Dependency – re-validates the session cookie against Supabase on every
    request, then checks admin authorisation.

    Uses client.auth.get_user(token) instead of local JWT decoding so that:
      • Tampered tokens are rejected (Supabase verifies the signature).
      • Expired / revoked sessions are rejected (live DB check).
      • The canonical user object (email, app_metadata) is always fresh.

    Raises 401 when the token is missing, invalid, or the Supabase call fails.
    Raises 403 when the user is authenticated but not an admin.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Live re-validation against Supabase — replaces local verify_jwt().
    try:
        client = _auth_client()
        resp = client.auth.get_user(token)
        if not resp or not resp.user:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        user = resp.user
    except HTTPException:
        raise
    except Exception:
        # Any network or auth error → treat as invalid session.
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    email       = (user.email or "").lower()
    app_metadata = dict(user.app_metadata or {})

    if not _is_admin(email, app_metadata):
        raise HTTPException(status_code=403, detail="Admin access required")

    return {
        "email":        email,
        "id":           str(user.id),
        "app_metadata": app_metadata,
    }


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
            secure=False,            # set to True in production (HTTPS)
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
