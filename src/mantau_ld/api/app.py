"""The FastAPI app factory: wire mantau_core + this repo's own store/ingest
into `app.state`, once, in `lifespan`.

Channel selection mirrors mantau-backend-rtsp exactly: push if FCM is
configured, Telegram if a bot token + chat ids are configured (TEMPORARY,
see mantau_core.notify.channels.telegram), and a console fallback if
neither is -- so a fresh checkout with zero secrets still demonstrates the
full ingest -> dispatch -> alert path, just to stdout.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mantau_core.notify import ChannelBinding, Fanout, FixedBinding, PushBinding, TelegramBinding
from mantau_core.notify.channels.console import ConsoleChannel
from mantau_core.notify.channels.push import FCMPushChannel, ServiceAccountCredentials
from mantau_core.notify.channels.telegram import TelegramChannel
from mantau_core.notify.delivery import AckService, DeliveryTracker

from ..alerts.dispatcher import AlertDispatcher
from ..config import Settings
from ..frames import FrameStore
from ..heartbeats import HeartbeatTracker
from ..store.agents_repo import AgentsRepo
from ..store.cameras_repo import CamerasRepo
from ..store.control_repo import ControlRepo
from ..store.db import Database
from ..store.events_repo import EventsRepo
from ..store.recipient_resolver import SqliteRecipientResolver
from ..store.sync_db import SyncDatabase
from ..store.token_store import SqliteTokenStore
from .routes import agents, cameras, contacts, control, devices, events, frames, health, ingest


def _build_channels(
    settings: Settings, resolver: SqliteRecipientResolver, token_store: SqliteTokenStore
) -> list[ChannelBinding]:
    channels: list[ChannelBinding] = []
    if settings.push_configured:
        credentials = ServiceAccountCredentials(settings.fcm_service_account_path)
        fcm = FCMPushChannel(settings.fcm_project_id, credentials, token_store)
        channels.append(PushBinding(notifier=fcm, resolver=resolver))
    if settings.telegram_configured and settings.telegram_chat_id_list():
        telegram = TelegramChannel(settings.telegram_bot_token)
        channels.append(TelegramBinding(notifier=telegram, chat_ids=settings.telegram_chat_id_list()))
    if not channels:
        print("[mantau-ld] no push/Telegram configured -- alerts go to the console only")
        # FixedBinding, not PushBinding: a console fallback must fire
        # regardless of whether any device is registered yet -- PushBinding
        # resolves targets from the (empty, on a fresh checkout) device
        # resolver, which silently never fires. See FixedBinding's docstring.
        channels.append(FixedBinding(notifier=ConsoleChannel(), targets=["console"]))
    return channels


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        await db.connect()
        sync_db = SyncDatabase(settings.db_path)

        agents_repo = AgentsRepo(db)
        control_repo = ControlRepo(db)
        cameras_repo = CamerasRepo(db)
        events_repo = EventsRepo(db)
        token_store = SqliteTokenStore(sync_db)
        resolver = SqliteRecipientResolver(sync_db, token_store)

        fanout = Fanout(channels=_build_channels(settings, resolver, token_store), tracker=DeliveryTracker())
        dispatcher = AlertDispatcher(events_repo, cameras_repo, fanout)

        app.state.settings = settings
        app.state.db = db
        app.state.sync_db = sync_db
        app.state.agents_repo = agents_repo
        app.state.control_repo = control_repo
        app.state.cameras_repo = cameras_repo
        app.state.events_repo = events_repo
        app.state.token_store = token_store
        app.state.resolver = resolver
        app.state.dispatcher = dispatcher
        app.state.ack_service = AckService()
        app.state.heartbeats = HeartbeatTracker()
        app.state.frames = FrameStore()

        yield

        await db.close()
        sync_db.close()

    app = FastAPI(title="mantau-backend-localdevice (server)", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(agents.router)
    app.include_router(control.router)
    app.include_router(cameras.router)
    app.include_router(events.router)
    app.include_router(devices.router)
    app.include_router(contacts.router)
    app.include_router(frames.router)
    return app
