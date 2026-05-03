"""
app/db/database.py  –  Supabase client (mirrors your existing database.py pattern)
All DB operations go through get_supabase_admin_client() for backend use.
"""
import os
import jwt
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "Missing Supabase credentials.\n"
        "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in your .env file."
    )


def get_supabase_client(jwt_token: str | None = None) -> Client:
    """Get a Supabase client with optional JWT auth (respects RLS)."""
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    if jwt_token:
        client.postgrest.auth(jwt_token)
    return client


def get_supabase_admin_client() -> Client:
    """Get a Supabase admin client — bypasses RLS. Used for all pipeline operations."""
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_jwt(jwt_token: str) -> dict | None:
    """Verify JWT token and return decoded payload (no signature check — Supabase handles that)."""
    try:
        return jwt.decode(jwt_token, options={"verify_signature": False})
    except Exception:
        return None
