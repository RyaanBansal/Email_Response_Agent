"""
routers/templates.py  –  Response template management + custom query types

GET    /api/templates                       → list all templates
PUT    /api/templates/{id}                  → update subject, body, and send_delay_seconds
POST   /api/templates                       → create a new template

GET    /api/templates/query-types           → list all query types (built-in + custom)
POST   /api/templates/query-types           → create / update a custom query type
DELETE /api/templates/query-types/{name}    → delete a custom query type
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models import (
    get_all_templates,
    update_template,
    update_template_timer,
    insert_template,
    get_custom_query_types,
    upsert_custom_query_type,
    delete_custom_query_type,
)
from app.db.database import get_supabase_admin_client
from routers.auth import get_current_user

router = APIRouter(prefix="/api/templates", tags=["templates"])


class TemplateUpdate(BaseModel):
    subject: str
    body: str
    send_delay_seconds: Optional[int] = None


class TemplateCreate(BaseModel):
    name: str
    query_type: str
    subject: str
    body: str
    send_delay_seconds: Optional[int] = None


class QueryTypeUpsert(BaseModel):
    name: str
    keywords: str = ""   # comma-separated plain words / phrases


# ── Template endpoints ────────────────────────────────────────────────────────

@router.get("")
def list_templates(_user=Depends(get_current_user)):
    return get_all_templates()


@router.put("/{template_id}")
def update(template_id: int, body: TemplateUpdate, _user=Depends(get_current_user)):
    update_template(template_id, body.subject, body.body)
    update_template_timer(template_id, body.send_delay_seconds)
    return {"detail": "saved"}


@router.post("")
def create(body: TemplateCreate, _user=Depends(get_current_user)):
    insert_template(body.name, body.query_type, body.subject, body.body)
    # fetch the new template id to apply the timer
    res = (
        get_supabase_admin_client()
        .table("templates")
        .select("id")
        .eq("name", body.name)
        .limit(1)
        .execute()
    )
    if res.data and body.send_delay_seconds is not None:
        update_template_timer(res.data[0]["id"], body.send_delay_seconds)
    return {"detail": "created"}


# ── Query-type endpoints ──────────────────────────────────────────────────────

@router.get("/query-types")
def list_query_types(_user=Depends(get_current_user)):
    """
    Return all query types available in the system:
    built-in types + any custom types stored in the DB.
    Each entry has: name, is_builtin, keywords (custom only).
    """
    from app.ai.generator import _BUILTIN_QUERY_TYPES
    builtin_set = set(_BUILTIN_QUERY_TYPES)
    custom_rows = get_custom_query_types()
    custom_names = {r["name"] for r in custom_rows}

    result = [{"name": n, "is_builtin": True, "keywords": ""} for n in _BUILTIN_QUERY_TYPES]
    for row in custom_rows:
        if row["name"] not in builtin_set:
            result.append({
                "name":       row["name"],
                "is_builtin": False,
                "keywords":   row.get("keywords") or "",
            })
        # If a custom row shadows a built-in name, surface the keywords but keep is_builtin=True
        else:
            for entry in result:
                if entry["name"] == row["name"]:
                    entry["keywords"] = row.get("keywords") or ""
                    break

    return result


@router.post("/query-types")
def create_or_update_query_type(body: QueryTypeUpsert, _user=Depends(get_current_user)):
    """
    Create or update a custom query type.
    Built-in type names are allowed here to add/update their keyword list only.
    """
    name = body.name.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Query type name cannot be empty.")
    row = upsert_custom_query_type(name, body.keywords)
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to save query type.")
    return {"detail": "saved", "name": name}


@router.delete("/query-types/{name}")
def remove_query_type(name: str, _user=Depends(get_current_user)):
    """
    Delete a custom query type.
    Built-in types cannot be deleted (they are hard-coded), but calling this
    on a built-in name only removes any keyword overrides stored in the DB.
    """
    ok = delete_custom_query_type(name)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete query type.")
    return {"detail": "deleted", "name": name}
