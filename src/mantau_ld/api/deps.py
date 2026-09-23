"""FastAPI dependency accessors -- everything routes need lives on `app.state`,
wired once in `app.py`'s lifespan.
"""

from __future__ import annotations

from fastapi import Request
from mantau_core.notify.delivery import AckService

from ..alerts.dispatcher import AlertDispatcher
from ..config import Settings
from ..frames import FrameStore
from ..heartbeats import HeartbeatTracker
from ..store.agents_repo import AgentsRepo
from ..store.cameras_repo import CamerasRepo
from ..store.control_repo import ControlRepo
from ..store.db import Database
from ..store.events_repo import EventsRepo
from ..store.identity_repo import IdentityRepo
from ..store.recipient_resolver import SqliteRecipientResolver
from ..store.token_store import SqliteTokenStore


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_agents_repo(request: Request) -> AgentsRepo:
    return request.app.state.agents_repo


def get_cameras_repo(request: Request) -> CamerasRepo:
    return request.app.state.cameras_repo


def get_control_repo(request: Request) -> ControlRepo:
    return request.app.state.control_repo


def get_events_repo(request: Request) -> EventsRepo:
    return request.app.state.events_repo


def get_identity_repo(request: Request) -> IdentityRepo:
    return request.app.state.identity_repo


def get_token_store(request: Request) -> SqliteTokenStore:
    return request.app.state.token_store


def get_resolver(request: Request) -> SqliteRecipientResolver:
    return request.app.state.resolver


def get_dispatcher(request: Request) -> AlertDispatcher:
    return request.app.state.dispatcher


def get_ack_service(request: Request) -> AckService:
    return request.app.state.ack_service


def get_heartbeats(request: Request) -> HeartbeatTracker:
    return request.app.state.heartbeats


def get_frames(request: Request) -> FrameStore:
    return request.app.state.frames
