"""Emergency-contact CRUD -- populates the escalation chain
(`mantau_core.notify.escalation`). Not wired to an actual escalation walk
this pass; see the server README for why.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from mantau_core.notify.recipients import EmergencyContact
from pydantic import BaseModel, Field

from ...control_auth import authenticated_user
from ...store.recipient_resolver import SqliteRecipientResolver
from ...store.identity_repo import UserPrincipal
from ..deps import get_resolver

router = APIRouter(prefix="/contacts", tags=["contacts"])


MAX_CONTACTS = 10
_PHONE = r"^\+?[0-9][0-9 ()-]{5,19}$"


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    phone: str = Field(pattern=_PHONE)
    relation: str = Field(min_length=1, max_length=40)
    priority: int = Field(default=100, ge=0, le=1000)


class ContactUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    phone: str = Field(pattern=_PHONE)
    relation: str = Field(min_length=1, max_length=40)


class ContactOrder(BaseModel):
    contact_ids: list[str] = Field(max_length=MAX_CONTACTS)


@router.post("", response_model=EmergencyContact, status_code=201)
async def add_contact(
    body: ContactCreate,
    resolver: SqliteRecipientResolver = Depends(get_resolver),
    principal: UserPrincipal = Depends(authenticated_user),
) -> EmergencyContact:
    if len(resolver.list_contacts(principal.household_id)) >= MAX_CONTACTS:
        raise HTTPException(409, "too_many_contacts")
    contact = EmergencyContact(
        contact_id=f"contact-{uuid.uuid4().hex[:8]}", name=body.name, phone=body.phone,
        relation=body.relation, priority=body.priority,
    )
    resolver.add_contact(principal.household_id, contact)
    return contact


@router.get("", response_model=list[EmergencyContact])
async def list_contacts(
    resolver: SqliteRecipientResolver = Depends(get_resolver),
    principal: UserPrincipal = Depends(authenticated_user),
) -> list[EmergencyContact]:
    return resolver.list_contacts(principal.household_id)


@router.put("/order", response_model=list[EmergencyContact])
async def reorder_contacts(
    body: ContactOrder,
    resolver: SqliteRecipientResolver = Depends(get_resolver),
    principal: UserPrincipal = Depends(authenticated_user),
) -> list[EmergencyContact]:
    """Order decides who is called first. The list must name every contact
    of the household exactly once."""
    current = {c.contact_id: c for c in resolver.list_contacts(principal.household_id)}
    if sorted(body.contact_ids) != sorted(current):
        raise HTTPException(400, "order_must_list_every_contact")
    for priority, contact_id in enumerate(body.contact_ids, start=1):
        resolver.add_contact(principal.household_id,
                             current[contact_id].model_copy(update={"priority": priority}))
    return resolver.list_contacts(principal.household_id)


@router.put("/{contact_id}", response_model=EmergencyContact)
async def update_contact(
    contact_id: str,
    body: ContactUpdate,
    resolver: SqliteRecipientResolver = Depends(get_resolver),
    principal: UserPrincipal = Depends(authenticated_user),
) -> EmergencyContact:
    existing = next((c for c in resolver.list_contacts(principal.household_id)
                     if c.contact_id == contact_id), None)
    if existing is None:
        raise HTTPException(404, "resource_not_found")
    updated = existing.model_copy(update=body.model_dump())
    resolver.add_contact(principal.household_id, updated)
    return updated


@router.delete("/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: str,
    resolver: SqliteRecipientResolver = Depends(get_resolver),
    principal: UserPrincipal = Depends(authenticated_user),
) -> None:
    if not resolver.delete_contact(principal.household_id, contact_id):
        raise HTTPException(404, "resource_not_found")
