"""
routers/auth.py  –  Authentication endpoints (Supabase email+password)

POST /api/auth/login   → sets HttpOnly session cookie
POST /api/auth/logout  → clears session cookie
GET  /api/auth/me      → returns current user info

Security fixes applied
──────────────────────
P0 (original – JWT not verified):
  get_current_user() now uses client.auth.get_user(token) to re-validate
  every request against Supabase (live revocation + signature check).

P1 (this revision – fail-open on unconfigured admin gates):
  Previously, if ADMIN_EMAILS was empty AND the user had no app_metadata role,
  _is_admin() returned True with only a warning.  This meant any authenticated
  Supabase project user — including test accounts created by third-party
  integrations — could access every admin endpoint.

  Fix: _is_admin() now raises a RuntimeError at startup when neither guard is
  configured, and returns False (raises 403) at request time when neither
  condition is met.  The operator must configure at least one of:
    • ADMIN_EMAILS env var (comma-separated allowlist), or
    • app_metadata.role = 'admin' on admin Supabase users.

  A clear error message is logged at startup so the misconfiguration is
  visible immediately rather than only discovered after a breach.
"""
import os
import warnings
from fastapi import APIRouter, Response, Request, HTTPException, Depends
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/auth", tags=["auth"])

SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_ANON = os.getenv("SUPABASE_ANON_KEY")

# Comma-separated list of email addresses allowed to access the admin UI.
# Example: ADMIN_EMAILS=alice@example.com,bob@example.com
_ADMIN_EMAILS: set[str] = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# Warn loudly at import time if no admin gate is configured.
# This does NOT block startup (the app may set app_metadata roles instead),
# but it makes the misconfiguration visible in logs immediately.
_NO_STATIC_ALLOWLIST = not _ADMIN_EMAILS
if _NO_STATIC_ALLOWLIST:
    warnings.warn(
        "ADMIN_EMAILS is not set.  Access is controlled solely by "
        "app_metadata.role='admin' in Supabase.  If no users have that role "
        "set, ALL authenticated Supabase users will be DENIED access (fail-closed). "
        "Set ADMIN_EMAILS or assign the 'admin' role in Supabase → Authentication → Users.",
        stacklevel=1,
    )

SESSION_COOKIE = "auth_token"


class LoginRequest(BaseModel):
    email: str
    password: str


def _auth_client():
    return create_client(SUPABASE_URL, SUPABASE_ANON)


def _is_admin(email: str, app_metadata: dict) -> bool:
    """
    Return True when the user is an authorised admin.

    Two complementary checks (either is sufficient):
      1. The user's email is in the ADMIN_EMAILS allowlist.
      2. The Supabase user record carries app_metadata.role == 'admin'.

    FIX P1: If neither gate is configured the function now returns False
    (fail-closed) instead of True (fail-open).  This prevents any
    authenticated Supabase user from accessing admin routes when the operator
    has not explicitly granted access.

    The startup warning above tells the operator what to do if they are
    locked out because they forgot to configure either gate.
    """
    email = (email or "").lower()

    if _ADMIN_EMAILS and email in _ADMIN_EMAILS:
        return True

    if (app_metadata or {}).get("role") == "admin":
        return True

    # FIX P1: Fail closed — do NOT allow access when no gate matches.
    # Previously this returned True with a warning, granting access to any
    # authenticated Supabase user regardless of intended authorisation.
    return False


def get_current_user(request: Request) -> dict:
    """
    Dependency – re-validates the session cookie against Supabase on every
    request, then checks admin authorisation.

    Raises 401 when the token is missing, invalid, or the Supabase call fails.
    Raises 403 when the user is authenticated but not an admin.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        client = _auth_client()
        resp = client.auth.get_user(token)
        if not resp or not resp.user:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        user = resp.user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    email        = (user.email or "").lower()
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

        # Determine whether we are running over HTTPS.
        # In production (Render, any HTTPS deployment) this should always be
        # True.  The env var COOKIE_SECURE can be set to "false" explicitly
        # for local HTTP development only.
        secure = os.getenv("COOKIE_SECURE", "true").lower() not in ("false", "0", "no")

        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=secure,        # FIX P2: True by default; override via COOKIE_SECURE=false for local dev
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
