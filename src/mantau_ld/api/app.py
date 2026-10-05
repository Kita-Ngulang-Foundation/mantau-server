"""The FastAPI app factory: wire mantau_core + this repo's own store/ingest
into `app.state`, once, in `lifespan`.

Alerts go out as FCM push to every device registered by a member of the
event's household -- there is no other channel.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from mantau_core.notify import ChannelBinding, Fanout, PushBinding
from mantau_core.notify.protocol import Notifier
from mantau_core.notify.channels.push import FCMPushChannel, ServiceAccountCredentials
from mantau_core.notify.delivery import AckService, DeliveryTracker

from ..alerts.dispatcher import AlertDispatcher
from ..config import FIREBASE_JWKS_URL, Settings
from ..frames import FrameStore
from ..inference.service import DetectorFactory, FrameDecoder, InferenceService
from ..oidc_auth import OidcAuthenticator
from ..store.agents_repo import AgentsRepo
from ..store.cameras_repo import CamerasRepo
from ..store.control_repo import ControlRepo
from ..store.db import Database
from ..store.detection_settings_repo import DetectionSettingsRepo, migrate_floor_default
from ..store.recordings_repo import RecordingsRepo
from ..store.events_repo import EventsRepo
from ..store.identity_repo import IdentityRepo
from ..store.inference_repo import InferenceRepo
from ..store.recipient_resolver import SqliteRecipientResolver
from ..store.sync_db import SyncDatabase
from ..store.token_store import SqliteTokenStore
log = logging.getLogger("mantau_ld")
# aiosqlite logs every statement with its parameters at DEBUG, which would put
# agent secrets, enrollment-key hashes, and FCM tokens into logs.
logging.getLogger("aiosqlite").setLevel(logging.INFO)

from .routes import (  # noqa: E402
    agents, cameras, contacts, control, detection, devices, events, frames, health, households,
    inference, ingest, recordings,
)


def _build_channels(
    settings: Settings, resolver: SqliteRecipientResolver, token_store: SqliteTokenStore,
    push_notifier: Notifier | None,
) -> list[ChannelBinding]:
    if push_notifier is None:
        if not settings.push_configured:
            return []
        credentials = ServiceAccountCredentials(settings.fcm_service_account_path)
        push_notifier = FCMPushChannel(settings.fcm_project_id, credentials, token_store)
    return [PushBinding(notifier=push_notifier, resolver=resolver)]


def create_app(
    settings: Settings | None = None,
    *,
    oidc_authenticator: OidcAuthenticator | None = None,
    inference_factory: DetectorFactory | None = None,
    inference_decoder: FrameDecoder | None = None,
    push_notifier: Notifier | None = None,
) -> FastAPI:
    """`inference_factory`/`inference_decoder` replace the MediaPipe detector
    and JPEG decoder (tests); by default the real ones are used when the
    `detection` extra is installed, and inference reports unavailable if not.
    `push_notifier` replaces FCM delivery to registered devices (tests)."""
    settings = settings or Settings()
    oidc_authenticator = oidc_authenticator or OidcAuthenticator(
        issuer=settings.auth_issuer,
        audience=settings.auth_project_id,
        jwks_url=FIREBASE_JWKS_URL,
        algorithms=["RS256"],
        leeway_s=settings.auth_leeway_s,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        problems = settings.configuration_problems()
        if problems:
            # Serving continues so /ready can report it; user routes already
            # fail closed (503) without a Firebase project.
            log.error("mantau-server misconfigured; missing: %s", ", ".join(problems))
        db = Database(settings.db_path)
        await db.connect()
        sync_db = SyncDatabase(settings.db_path)

        agents_repo = AgentsRepo(db)
        identity_repo = IdentityRepo(db)
        control_repo = ControlRepo(db)
        cameras_repo = CamerasRepo(db)
        events_repo = EventsRepo(db)
        token_store = SqliteTokenStore(sync_db)
        resolver = SqliteRecipientResolver(sync_db, token_store)

        fanout = Fanout(channels=_build_channels(settings, resolver, token_store, push_notifier), tracker=DeliveryTracker())
        dispatcher = AlertDispatcher(events_repo, cameras_repo, fanout)

        app.state.settings = settings
        app.state.db = db
        app.state.sync_db = sync_db
        app.state.agents_repo = agents_repo
        app.state.identity_repo = identity_repo
        app.state.control_repo = control_repo
        app.state.cameras_repo = cameras_repo
        app.state.events_repo = events_repo
        app.state.token_store = token_store
        app.state.resolver = resolver
        app.state.dispatcher = dispatcher
        app.state.ack_service = AckService()
        app.state.frames = FrameStore()
        app.state.detection_settings_repo = DetectionSettingsRepo(db)
        try:
            floor_migration = await migrate_floor_default(
                db, control_repo, ttl_s=settings.command_ttl_s)
        except Exception:
            # Rolled back as a whole; the next startup tries again.
            log.exception("floor-default detection-settings migration failed; nothing changed")
        else:
            if floor_migration is not None:
                log.info("floor-default migration: %d camera(s) moved floor_minutes 2.0 -> 0.5, "
                         "%d apply_detection_settings command(s) queued", *floor_migration)
        app.state.recordings_repo = RecordingsRepo(db, settings.recordings_dir)
        await app.state.recordings_repo.prune(settings.recording_retention_days)
        app.state.inference_repo = InferenceRepo(db)
        await app.state.inference_repo.prune(settings.inference_result_retention_days)
        app.state.inference = InferenceService(
            settings, factory=inference_factory, decoder=inference_decoder)
        await app.state.inference.start()

        try:
            yield
        finally:
            await app.state.inference.close()
            await db.close()
            sync_db.close()

    docs = settings.docs_enabled
    app = FastAPI(
        title="mantau-server", version="0.1.0", lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.oidc_authenticator = oidc_authenticator
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # FastAPI's default errors echo rejected input, including passwords.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": error["loc"], "type": error["type"], "msg": "Invalid value"}
            for error in exc.errors()
        ]})
    if settings.cors_origin_list():
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list(),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key",
                           "X-Mantau-Household-ID"],
        )
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(agents.router)
    app.include_router(control.router)
    app.include_router(households.router)
    app.include_router(cameras.router)
    app.include_router(events.router)
    app.include_router(devices.router)
    app.include_router(contacts.router)
    app.include_router(frames.router)
    app.include_router(detection.router)
    app.include_router(recordings.router)
    app.include_router(inference.router)
    return app
