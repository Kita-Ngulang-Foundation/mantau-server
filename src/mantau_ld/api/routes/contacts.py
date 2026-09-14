"""Emergency-contact CRUD -- populates the escalation chain
(`mantau_core.notify.escalation`). Not wired to an actual escalation walk
this pass; see the server README for why.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from mantau_core.notify.recipients import EmergencyContact
from pydantic import BaseModel

from ...store.recipient_resolver import SqliteRecipientResolver
from ..deps import get_resolver

router = APIRouter(prefix="/contacts", tags=["contacts"])


class ContactCreate(BaseModel):
    name: str
    phone: str
    relation: str
    priority: int


@router.post("", response_model=EmergencyContact, status_code=201)
async def add_contact(
    body: ContactCreate, resolver: SqliteRecipientResolver = Depends(get_resolver)
) -> EmergencyContact:
    contact = EmergencyContact(
        contact_id=f"contact-{uuid.uuid4().hex[:8]}", name=body.name, phone=body.phone,
        relation=body.relation, priority=body.priority,
    )
    resolver.add_contact(contact)
    return contact


@router.get("", response_model=list[EmergencyContact])
async def list_contacts(resolver: SqliteRecipientResolver = Depends(get_resolver)) -> list[EmergencyContact]:
    return resolver.list_contacts()


@router.delete("/{contact_id}", status_code=204)
async def delete_contact(contact_id: str, resolver: SqliteRecipientResolver = Depends(get_resolver)) -> None:
    if not resolver.delete_contact(contact_id):
        raise HTTPException(404, f"contact {contact_id!r} not found")
