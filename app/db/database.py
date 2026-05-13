import os
import jwt
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import timedelta

load_dotenv()

SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_PUBLIC_KEY = os.getenv("SUPABASE_JWT_PUBLIC_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "Missing Supabase credentials.\n"
        "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in your .env file."
    )

if not SUPABASE_JWT_PUBLIC_KEY:
    import warnings
    warnings.warn(
        "SUPABASE_JWT_PUBLIC_KEY is not set. JWT signatures will NOT be "
        "verified — set this variable before deploying to production.",
        stacklevel=1,
    )


def get_supabase_client(jwt_token: str | None = None) -> Client:
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    if jwt_token:
        client.postgrest.auth(jwt_token)
    return client


def get_supabase_admin_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_jwt(jwt_token: str) -> dict | None:
    if not jwt_token:
        return None
    try:
        if SUPABASE_JWT_PUBLIC_KEY:
            return jwt.decode(
                jwt_token,
                SUPABASE_JWT_PUBLIC_KEY,
                algorithms=["ES256"],
                options={"require": ["exp", "sub"]},
                leeway=timedelta(seconds=30),
                audience="authenticated"
            )
        else:
            import warnings
            warnings.warn(
                "JWT decoded WITHOUT signature verification. "
                "Set SUPABASE_JWT_PUBLIC_KEY before deploying.",
                stacklevel=2,
            )
            return jwt.decode(
                jwt_token,
                options={"verify_signature": False, "require": ["exp"]},
                algorithms=["ES256"],
            )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError as e:
        print(f"JWT error: {e}")
        return None
    except Exception:
        return None
