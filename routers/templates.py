"""
routers/templates.py  –  Response template management

GET    /api/templates            → list all templates
PUT    /api/templates/{id}       → update subject, body, and send_delay_seconds
POST   /api/templates            → create a new template
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models import (
    get_all_templates,
    update_template,
    update_template_timer,
    insert_template,
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
