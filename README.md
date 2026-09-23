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
$env:MANTAU_CONTROL_PLANE_MODE = "local_dev"
.venv\Scripts\python.exe -m uvicorn mantau_ld.main:app --port 8100 --reload
```

```powershell
# enroll an agent -- do this once, before it can send anything
curl -X POST http://localhost:8100/agents/enroll -H "Content-Type: application/json" `
  -d '{"agent_id": "agent-1"}'
# -> {"agent_id": "agent-1", "secret": "...", "claim_code": "..."}

# claim it before it can ingest or own cameras
curl -X POST http://localhost:8100/agent-claims -H "Content-Type: application/json" `
  -H "X-Mantau-User-ID: local-user" `
  -d '{"claim_code": "...", "platform": "linux"}'

curl -X POST http://localhost:8100/cameras -H "Content-Type: application/json" `
  -H "X-Mantau-User-ID: local-user" `
  -d '{"camera_id": "cam-1", "name": "Kamar Ibu", "agent_id": "agent-1"}'

curl http://localhost:8100/ready
```

In explicit `local_dev` mode, alerts fall back to the console when no push or
Telegram channel is configured. Production never uses global Telegram or
console recipients.

## Configuration

Env vars, all prefixed `MANTAU_` (see `mantau_core.config.CoreSettings` for
the inherited ones -- push/Telegram credentials, backoff defaults):

| Var | Default | |
|---|---|---|
| `MANTAU_DB_PATH` | `data/mantau_ld.db` | SQLite file. `:memory:` supported via shared-cache mode (see `store/db.py`). |
| `MANTAU_TELEGRAM_CHAT_IDS` | `""` | Comma-separated. TEMPORARY, see `mantau_core.notify.channels.telegram`. |
| `MANTAU_CONTROL_PLANE_MODE` | `production` | `production` validates OIDC; `local_dev` explicitly enables `X-Mantau-User-ID`; `disabled` fails closed. |
| `MANTAU_OIDC_ISSUER` | empty | Exact trusted JWT issuer. Required in production. |
| `MANTAU_OIDC_AUDIENCE` | empty | Required JWT audience. Required in production. |
| `MANTAU_OIDC_JWKS_URL` | empty | HTTPS JWKS endpoint used for signature-key lookup. Required in production. |
| `MANTAU_OIDC_ALGORITHMS` | `RS256` | Comma-separated JWT signature algorithms accepted from the configured issuer. |
| `MANTAU_OIDC_LEEWAY_S` | `30` | Clock-skew allowance for JWT time validation. |
| `MANTAU_CONTROL_PLANE_AUTH_TOKENS_JSON` | `{}` | Deprecated and ignored; static bearer-token maps are not production authentication. |
| `MANTAU_CONTROL_PLANE_ENCRYPTION_KEY` | empty | Required Fernet key for camera test/config commands. Keep stable across restarts and rotate operationally only after credential commands drain. |
| `MANTAU_COMMAND_TTL_S` | `300` | Command expiry window. |
| `MANTAU_COMMAND_DELIVERY_LEASE_S` | `30` | Re-delivery delay when an agent does not acknowledge a delivered command. |
| `MANTAU_CLAIM_CODE_TTL_S` | `600` | Lifetime of a hashed, single-use enrollment claim. |
| `MANTAU_CLAIM_ATTEMPT_LIMIT` | `5` | Claim attempts allowed per user in one rate window. |
| `MANTAU_CLAIM_ATTEMPT_WINDOW_S` | `60` | Claim rate-limit window. |
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
| `MANTAU_API_DOCS_ENABLED` | `false` | Serve `/docs`, `/redoc`, `/openapi.json` in production. Always on in `local_dev`. |

## API

| Route | What |
|---|---|
| `GET /health`, `GET /ready` | Liveness; readiness answers 503 with the *names* of missing production settings (OIDC, Fernet key, FCM) or an unreachable database. Use `/ready` as the deploy health check. Per-agent heartbeats only in `local_dev`. |
| `POST /agent-control/claim-code` | Agent-authenticated. Fresh single-use claim code for an unclaimed agent; 409 once claimed. Never rotates the secret. |
| `POST /agents/enroll` | Create-only initial enrollment (existing id without proof: 409 `agent_id_taken`), or secret rotation with `X-Mantau-Agent-ID`/`-Secret` proof. Secret shown once. |
| `GET /agents`, `DELETE /agents/{id}` | List enrolled agents; revoke one (its envelopes fail verification from then on). |
| `POST /ingest` | The one endpoint an agent calls. See `../protocol/PROTOCOL.md`. |
| `POST/GET/DELETE /cameras[/{id}]` | Register a camera's display name (this server holds no RTSP URL or credentials -- the agent owns those). |
| `GET /events`, `GET /events/{id}` | Household event history, newest first; `limit`, `before` (the last `created_at` shown), `kind` filters. Includes camera name, signals, ack/review attribution, and whether a clip exists. |
| `POST /events/{id}/ack` | First acknowledgement is recorded durably for the calling user; closes the latency trace's ACKED stage. |
| `POST /events/{id}/status` | Human triage: `needs_review` / `dismissed` / `confirmed`. |
| `GET /households` | The signed-in user's households (`household_id`, `name`, `role`). The only user route that needs no household selection. |
| `POST /households/join` | Join a household with a single-use invite code (rate-limited). Needs no household selection. |
| `PATCH /households/{id}`, `GET /households/{id}/members`, `POST /households/{id}/invites`, `DELETE /households/{id}/members/{user_id}` | Rename, list members, invite (owner/admin; admin invites only by owners), remove or leave. The last owner cannot leave. Push alerts go to every current member. |
| `GET/PUT /cameras/{id}/detection-settings` | Zones, per-feature thresholds, night window, timezone. Members read; owners/admins write. Each change is a new version delivered to the agent (`apply_detection_settings`); `applied_version` shows what the agent runs. |
| `POST /events/{id}/recording` | Agent-signed MP4 clip upload for its own event (`HMAC(secret, "<event_id>." + body)`). Refused for bathroom-duration events. Size-limited. |
| `GET /events/{id}/recording` | Clip download for household members. |
| `GET /inference/capability` | Whether this server runs the fall detector, with its frame limits. No tenant data; agents read it at startup. |
| `POST /agents/{id}/inference` | Agent-signed JPEG frame (`mantau_core.contracts.inference`: HMAC over a versioned message covering every header and the body). The server runs the same detector as the agents in one session per agent+camera+session id. Falls it detects are stored and pushed like ingested events and returned; frames with `X-Mantau-Event-Ids` are HYBRID confirmations, stored against that agent's own events (`server_confirmed` on `GET /events/{id}`). Retries with the same frame id return the first answer. Frames are never stored. |
| `POST /devices/register`, `DELETE /devices/{device_id}` | Household-owned push token lifecycle; raw tokens never appear in URLs. |
| `POST/GET/PUT/DELETE /contacts[/{id}]`, `PUT /contacts/order` | Emergency contacts (max 10, validated phone numbers); order decides who is called first. |

## Firebase Authentication

The Mantau app signs users in with Firebase Authentication (project
`mantau-fce89`). Firebase ID tokens are RS256 JWTs, so the OIDC validator
accepts them with configuration only:

```sh
MANTAU_CONTROL_PLANE_MODE=production
MANTAU_OIDC_ISSUER=https://securetoken.google.com/mantau-fce89
MANTAU_OIDC_AUDIENCE=mantau-fce89
MANTAU_OIDC_JWKS_URL=https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com
MANTAU_OIDC_ALGORITHMS=RS256
```

Users are keyed by `(issuer, sub)`, where `sub` is the Firebase UID. A first
sign-in creates the user and a household they own. A user with several
memberships must send `X-Mantau-Household-ID` (one of the ids from
`GET /households`); without it, household-scoped routes answer
`400 household_required`.

## Additive control plane

Enrollment still returns the one-time agent secret and now also returns a claim
code. The app authenticates, exchanges that short-lived single-use code at
`POST /agent-claims`, and can then see or control only agents owned by its
household. In `local_dev` mode the explicit development identity is
`X-Mantau-User-ID`; in production, `Authorization: Bearer ...` is validated
against the configured issuer, audience, JWKS signature keys, and expiry.
Missing production OIDC configuration fails closed.

App routes are `GET /agents`, `GET /agents/{id}/setup`, discovery command/result,
camera test/configuration, inference-mode update, restart, and reconfigure.
Every command-creating request requires `Idempotency-Key`. Agents authenticate
with their existing enrolled id/secret at
`POST /agent-control/commands/poll` and submit structured state/results at
`POST /agent-control/commands/{id}/results`.

Commands persist as `queued -> delivered -> running -> succeeded|failed`, or
`expired`. A lost delivery is leased and re-queued; an offline agent receives
unexpired work when it returns. Camera username/password fields are encrypted
with Fernet in a separate short-lived blob, never copied into metadata or
results, and the blob is deleted on the first running/final acknowledgement.
Database/log backups still contain the enrollment HMAC secret from the legacy
design, so protect them accordingly.

Rollout is expand-only: deploy the additive schema before enabling production
traffic, configure OIDC, then opt new agents into polling. Use `local_dev` only
for explicit legacy testing. Rollback means disabling user traffic and agent
polling while keeping the additive tables and columns; no down migration or
data deletion is required.

## Test

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

56 tests, all offline: real JWT and HMAC signing/verification,
real SQLite round-trips including the dedupe ledger's uniqueness constraint,
cross-household denial and migration coverage, and a real ASGI `TestClient`
exercising the full ingest -> dispatch -> event path together.

## Known gaps

- **Escalation isn't triggered on a timeout.** Same reasoning as
  mantau-backend-rtsp: `mantau_core.notify.escalation` exists and is tested,
  `/contacts` populates the chain, but there's no real channel to escalate
  *through* yet (push/Telegram target devices and chat ids, not phone
  numbers).
- **No out-of-order reassembly.** `ingest/dedupe.py` accepts an
  out-of-order envelope and flags it; it does not hold it back to restore
  strict per-agent sequence. See that module's docstring for what a reorder
  buffer would need.
- **No clip generation.**
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
