"""Households of the signed-in user.

The only user route that needs no household selection: a user with several
memberships calls it to learn which `X-Mantau-Household-ID` values are valid.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ...control_auth import authenticated_identity

router = APIRouter(prefix="/households", tags=["households"])


class HouseholdOut(BaseModel):
    household_id: str
    name: str
    role: str


@router.get("", response_model=list[HouseholdOut])
async def list_households(
    request: Request, identity: tuple[str, str] = Depends(authenticated_identity)
) -> list[dict]:
    issuer, subject = identity
    return await request.app.state.identity_repo.households(issuer, subject)
