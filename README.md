# mantau-ld-server

The server half of **Scenario 2**. Accepts signed envelopes from enrolled
agents over `POST /ingest`; never opens a connection to a customer's
network itself. See `../protocol/PROTOCOL.md` for the wire contract this
implements, and `../README.md` for how agent and server fit together.

```
 agent (outbound only) --POST /ingest--> verify signature --> dedupe (agent_id, seq)
                                                                    |
                                                      new: dispatch --> mantau_core.notify.Fanout
                                                      duplicate: no-op, still 200
```

## Install (local dev)

Requires Python 3.10–3.12, and `mantau-core` checked out one level up
(`../mantau-core`) -- it isn't published anywhere yet.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```powershell
$env:MANTAU_FCM_PROJECT_ID = "mantau-fce89"
$env:MANTAU_FCM_SERVICE_ACCOUNT_PATH = "C:\path\to\service-account.json"
$env:MANTAU_CONTROL_PLANE_ENCRYPTION_KEY = "<Fernet key>"
.venv\Scripts\python.exe -m uvicorn mantau_ld.main:app --port 8100 --reload
```

Every user request carries a Firebase ID token from the Mantau app
(`Authorization: Bearer ...`). There is no development identity and no
anonymous route except `/health`, `/ready`, `/inference/capability`, and the
agent's own enrollment.

Adding an agent:

```powershell
# 1. A household owner/admin creates a single-use key (the app: Beranda > Tambah perangkat)
curl -X POST http://localhost:8100/enrollment-keys -H "Authorization: Bearer <firebase-id-token>"
# -> {"key_id": "ekey-...", "enrollment_key": "MTU-XXXXX-XXXXX-XXXXX-XXXXX", "expires_at": "..."}

# 2. The agent presents it once (mantau-agent setup --key ..., or the Android agent app)
curl -X POST http://localhost:8100/agents/enroll -H "Content-Type: application/json" `
  -d '{"enrollment_key": "MTU-...", "agent_id": "agent-pi-3f9a", "name": "Ruang tamu", "platform": "linux_arm64"}'
# -> {"agent_id": "agent-pi-3f9a", "secret": "..."}   the agent stores this; nobody else sees it
```

The agent is now in the household that created the key. Camera setup then
runs from the app through the command channel.

## Configuration

Env vars, all prefixed `MANTAU_` (see `mantau_core.config.CoreSettings` for
the inherited ones -- push credentials, backoff defaults):

| Var | Default | |
|---|---|---|
| `MANTAU_DB_PATH` | `data/mantau_ld.db` | SQLite file. `:memory:` supported via shared-cache mode (see `store/db.py`). |
| `MANTAU_FIREBASE_PROJECT_ID` | `MANTAU_FCM_PROJECT_ID` | Firebase project whose ID tokens sign users in. Required (through either variable). |
| `MANTAU_AUTH_LEEWAY_S` | `30` | Clock-skew allowance for ID-token time validation. |
| `MANTAU_CONTROL_PLANE_ENCRYPTION_KEY` | empty | Required Fernet key for camera test/config commands. Keep stable across restarts and rotate operationally only after credential commands drain. |
| `MANTAU_COMMAND_TTL_S` | `300` | Command expiry window. |
| `MANTAU_COMMAND_DELIVERY_LEASE_S` | `30` | Re-delivery delay when an agent does not acknowledge a delivered command. |
| `MANTAU_ENROLLMENT_KEY_TTL_S` | `3600` | Lifetime of a single-use enrollment key. Only its SHA-256 is stored. |
| `MANTAU_CORS_ORIGINS` | empty | Comma-separated browser origins. Empty disables CORS (the mobile app and agents do not need it). |
| `MANTAU_RECORDINGS_DIR` | `data/recordings` | Event clips. Put on the same persistent volume as the database. |
| `MANTAU_RECORDING_RETENTION_DAYS` | `30` | Older clips are deleted. |
| `MANTAU_RECORDING_MAX_BYTES` / `MANTAU_FRAME_MAX_BYTES` | 20 MB / 2 MB | Upload limits (413 above them). |
| `MANTAU_HOUSEHOLD_INVITE_TTL_S` | `172800` | Invite code lifetime. |
| `MANTAU_INFERENCE_ENABLED` | `true` | Server inference for agents without a usable on-device detector. Needs mantau-AI (`mantau-core[detection]`); otherwise reported unavailable. |
| `MANTAU_INFERENCE_MAX_FRAME_BYTES` | `524288` | Per-frame upload limit (413 above it). |
| `MANTAU_INFERENCE_MAX_FRAME_AGE_S` / `MANTAU_INFERENCE_MAX_CLOCK_SKEW_S` | `10` / `5` | Frames captured longer ago, or further in the future, are refused (422). |
| `MANTAU_INFERENCE_MAX_FPS` | `15` | Advertised per-stream rate; a stream sending faster than twice that is refused (429). |
| `MANTAU_INFERENCE_MAX_SESSIONS` / `MANTAU_INFERENCE_SESSION_IDLE_S` | `8` / `120` | Concurrent detector sessions (each holds a pose model; 503 when full) and when an idle one is closed. |
| `MANTAU_INFERENCE_WORKERS` | `2` | Frames run through the detector at the same time. |
| `MANTAU_INFERENCE_IDEMPOTENCY_TTL_S` | `300` | How long an answered frame id is replayed instead of re-run. |
| `MANTAU_INFERENCE_RESULT_RETENTION_DAYS` | `30` | HYBRID confirmation results are deleted after this. Frames are never stored. |
| `MANTAU_API_DOCS_ENABLED` | `false` | Serve `/docs`, `/redoc`, `/openapi.json`. |

## API

| Route | What |
|---|---|
| `GET /health`, `GET /ready` | Liveness; readiness answers 503 with the *names* of missing settings (Firebase project, Fernet key, FCM) or an unreachable database. Use `/ready` as the deploy health check. |
| `POST /enrollment-keys`, `GET/DELETE /enrollment-keys/{id}` | Owners/admins create a single-use key for adding one agent; the app polls its status (`pending`/`used`/`expired`/`revoked`, with the agent id once used) and revokes it when setup is abandoned. |
| `POST /agents/enroll` | The agent presents the key with its own id, name, and platform and is created in the key's household. Invalid/expired/used key: 401 `invalid_enrollment_key`; id in use: 409 `agent_id_taken` (key stays unused). Secret shown once. |
| `GET /agents`, `PATCH /agents/{id}`, `DELETE /agents/{id}` | The household's agents with health; rename; remove (owners/admins; every later agent request fails with 401). |
| `POST /ingest` | The one endpoint an agent calls. See `../protocol/PROTOCOL.md`. |
| `POST/GET/DELETE /cameras[/{id}]` | Register a camera's display name (this server holds no RTSP URL or credentials -- the agent owns those). |
| `GET /events`, `GET /events/{id}` | Household event history, newest first; `limit`, `before` (the last `created_at` shown), `kind` filters. Includes camera name, signals, ack/review attribution, and whether a clip exists. |
| `POST /events/{id}/ack` | First acknowledgement is recorded durably for the calling user; closes the latency trace's ACKED stage. |
| `POST /events/{id}/status` | Human triage: `needs_review` / `dismissed` / `confirmed`. |
| `GET /households` | The signed-in user's households (`household_id`, `name`, `role`). The only user route that needs no household selection. |
| `POST /households/join` | Join a household with a single-use invite code (rate-limited). Needs no household selection. |
| `PATCH /households/{id}`, `GET /households/{id}/members`, `POST /households/{id}/invites`, `DELETE /households/{id}/members/{user_id}` | Rename, list members, invite (owner/admin; admin invites only by owners), remove or leave. The last owner cannot leave. Push alerts go to every current member. |
| `GET/PUT /cameras/{id}/detection-settings` | Zones, per-feature thresholds, night window, timezone. Members read; owners/admins write. Each change is a new version delivered to the agent (`apply_detection_settings`); `applied_version` shows what the agent runs. One-time startup migration (schema version 4): stored `stillness.floor_minutes` exactly 2.0 (the old default) becomes 0.5, with a new version and a queued `apply_detection_settings`; other values are kept. |
| `POST /events/{id}/recording` | Agent-signed MP4 clip upload for its own event (`HMAC(secret, "<event_id>." + body)`). Refused for bathroom-duration events. Size-limited. |
| `GET /events/{id}/recording` | Clip download for household members. |
| `POST /cameras/{id}/frame`, `GET /cameras/{id}/snapshot.jpg`, `GET /cameras/{id}/live.mjpeg` | Live view: agent-signed JPEG upload (`HMAC(secret, "<camera_id>." + body)`), latest frame in memory only. Each upload answers `X-Mantau-Live-Viewers`: open MJPEG streams plus a snapshot fetched in the last 5 s; agents send video-rate frames only while it is above 0. |
| `GET /inference/capability` | Whether this server runs the fall detector, with its frame limits. No tenant data; agents read it at startup. |
| `POST /agents/{id}/inference` | Agent-signed JPEG frame (`mantau_core.contracts.inference`: HMAC over a versioned message covering every header and the body). The server runs the same detector as the agents in one session per agent+camera+session id. Falls it detects are stored and pushed like ingested events and returned; frames with `X-Mantau-Event-Ids` are HYBRID confirmations, stored against that agent's own events (`server_confirmed` on `GET /events/{id}`). Retries with the same frame id return the first answer. Frames are never stored. |
| `POST /devices/register`, `DELETE /devices/{device_id}` | Household-owned push token lifecycle; raw tokens never appear in URLs. |
| `POST/GET/PUT/DELETE /contacts[/{id}]`, `PUT /contacts/order` | Emergency contacts (max 10, validated phone numbers); order decides who is called first. |

## Firebase Authentication

The Mantau app signs users in with Firebase Authentication (project
`mantau-fce89`, the same project that delivers push). Firebase ID tokens are
RS256 JWTs; the server checks signature (Google's published keys), issuer
`https://securetoken.google.com/<project>`, audience `<project>`, and expiry.
The project defaults to `MANTAU_FCM_PROJECT_ID`, so a deployment with push
configured needs no other auth setting.

Users are keyed by `(issuer, sub)`, where `sub` is the Firebase UID. A first
sign-in creates the user and a household they own. A user with several
memberships must send `X-Mantau-Household-ID` (one of the ids from
`GET /households`); without it, household-scoped routes answer
`400 household_required`.

## Agents and the command channel

Agents and app users share one database and one household model: an agent
belongs to exactly the household whose enrollment key it used, and users see
or control only their household's agents. Agents authenticate with their
enrolled id/secret (`X-Mantau-Agent-ID`/`-Secret`, or the HMAC signatures on
envelopes, frames, and clips).

App routes are `GET /agents`, `GET /agents/{id}/setup`, discovery command/result,
camera test/configuration, inference-mode update, restart, and reconfigure.
Every command-creating request requires `Idempotency-Key`. Agents poll at
`POST /agent-control/commands/poll` and submit structured state/results at
`POST /agent-control/commands/{id}/results`.

Commands persist as `queued -> delivered -> running -> succeeded|failed`, or
`expired`. A lost delivery is leased and re-queued; an offline agent receives
unexpired work when it returns. Camera username/password fields are encrypted
with Fernet in a separate short-lived blob, never copied into metadata or
results, and the blob is deleted on the first running/final acknowledgement.
Database backups contain agent HMAC secrets, so protect them accordingly.

Upgrading an existing database (v4 migration) drops the old claim-code tables
and revokes agents that were enrolled but never claimed; they join again with
an enrollment key.

## Test

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

All offline: real RS256 Firebase-style ID tokens and HMAC signing/verification,
real SQLite round-trips including the dedupe ledger's uniqueness constraint,
cross-household denial and migration coverage, and a real ASGI `TestClient`
exercising the full ingest -> dispatch -> event path together.

## Known gaps

- **Escalation isn't triggered on a timeout.** Same reasoning as
  mantau-backend-rtsp: `mantau_core.notify.escalation` exists and is tested,
  `/contacts` populates the chain, but there's no real channel to escalate
  *through* yet (push targets devices, not phone
  numbers).
- **No out-of-order reassembly.** `ingest/dedupe.py` accepts an
  out-of-order envelope and flags it; it does not hold it back to restore
  strict per-agent sequence. See that module's docstring for what a reorder
  buffer would need.
- Native agents generate bounded event clips; this server admits, stores and serves them under household authorization.
- **Agent secrets are stored in plain SQLite columns**, same simplification
  as mantau-backend-rtsp's camera passwords.
- **Docker Compose is unverified end-to-end** (`../docker/compose.yaml`) --
  run it against a live Docker engine before a demo depends on it.


## Deploying

`mantau-core.ref` pins the mantau-core commit for both the Docker image and
CI. Bump it only to a commit that is pushed to
`Kita-Ngulang-Foundation/mantau-core`. Point the platform health check at
`/ready`: a deploy missing OIDC, the Fernet key, or FCM settings stays
unhealthy instead of silently serving.

For a Railway Hobby deployment with server inference, build a wheel from the
`mantau-AI` commit pinned by `mantau-core` into
`private-deps/mantau_prototype-0.1.0-py3-none-any.whl` in a clean archive of
this repository's `main` commit. Set Railway's `MANTAU_REQUIRE_DETECTOR=1`
variable and upload that archive with `railway up --no-gitignore`.
`private-deps/` is Git-ignored; do not add the wheel to the repository. The
Dockerfile checks the wheel SHA-256 manifest and fails when the required wheel is absent.
This uses Railway's source upload and needs no private image registry or
GitHub token in Railway. A GitHub-only build without the wheel will fail while
the requirement is enabled; repeat the clean archive upload for each release.

See [OPERATIONS.md](OPERATIONS.md) for single-process limits, persistent storage,
retention, household/account lifecycle, diagnostics and isolated backup/restore.
Integration branches are validation candidates. Main/deployment promotion stays
gated on exact builds, signed upgrade and staging/physical acceptance evidence.
