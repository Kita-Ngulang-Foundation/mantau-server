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

Requires Python 3.10–3.12, and `mantau-core` checked out two levels up
(`../../mantau-core`) -- it isn't published anywhere yet.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```powershell
.venv\Scripts\python.exe -m uvicorn mantau_ld.main:app --port 8100 --reload
```

```powershell
# enroll an agent -- do this once, before it can send anything
curl -X POST http://localhost:8100/agents/enroll -H "Content-Type: application/json" `
  -d '{"agent_id": "agent-1"}'
# -> {"agent_id": "agent-1", "secret": "..."}  -- copy this into the agent's config

curl -X POST http://localhost:8100/cameras -H "Content-Type: application/json" `
  -d '{"camera_id": "cam-1", "name": "Kamar Ibu", "agent_id": "agent-1"}'

curl http://localhost:8100/ready
```

With no `MANTAU_FCM_*`/`MANTAU_TELEGRAM_*` configured, alerts print to
stdout via a console channel.

## Configuration

Env vars, all prefixed `MANTAU_` (see `mantau_core.config.CoreSettings` for
the inherited ones -- push/Telegram credentials, backoff defaults):

| Var | Default | |
|---|---|---|
| `MANTAU_DB_PATH` | `data/mantau_ld.db` | SQLite file. `:memory:` supported via shared-cache mode (see `store/db.py`). |
| `MANTAU_TELEGRAM_CHAT_IDS` | `""` | Comma-separated. TEMPORARY, see `mantau_core.notify.channels.telegram`. |
| `MANTAU_CONTROL_PLANE_MODE` | `disabled` | `disabled`, explicit `local_dev`, or `production`. |
| `MANTAU_CONTROL_PLANE_AUTH_TOKENS_JSON` | `{}` | Production bearer-token to account-id map. Production fails closed when empty/invalid. |
| `MANTAU_CONTROL_PLANE_ENCRYPTION_KEY` | empty | Required Fernet key for camera test/config commands. Keep stable across restarts and rotate operationally only after credential commands drain. |
| `MANTAU_COMMAND_TTL_S` | `300` | Command expiry window. |
| `MANTAU_COMMAND_DELIVERY_LEASE_S` | `30` | Re-delivery delay when an agent does not acknowledge a delivered command. |

## API

| Route | What |
|---|---|
| `GET /health`, `GET /ready` | Liveness, and per-agent status from its last heartbeat. |
| `POST /agents/enroll` | Issue a new HMAC secret for an agent id. Shown once. |
| `GET /agents`, `DELETE /agents/{id}` | List enrolled agents; revoke one (its envelopes fail verification from then on). |
| `POST /ingest` | The one endpoint an agent calls. See `../protocol/PROTOCOL.md`. |
| `POST/GET/DELETE /cameras[/{id}]` | Register a camera's display name (this server holds no RTSP URL or credentials -- the agent owns those). |
| `GET /events`, `GET /events/{id}` | List / inspect events. |
| `POST /events/{id}/ack` | A device saw the alert -- closes the latency trace's ACKED stage. |
| `POST /events/{id}/status` | Human triage: `needs_review` / `dismissed` / `confirmed`. |
| `POST /devices/register`, `DELETE /devices/{token}` | Push token lifecycle. |
| `POST/GET/DELETE /contacts[/{id}]` | Emergency-contact CRUD (see "Known gaps"). |

## Additive control plane

Enrollment still returns the one-time agent secret and now also returns a claim
code. The app authenticates, exchanges that code at `POST /agent-claims`, and
can then see or control only agents owned by its account. In `local_dev` mode
the explicit development identity is `X-Mantau-User-ID`; in `production` use
`Authorization: Bearer ...` with the configured token map. Control endpoints
never fall back to anonymous production access.

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

Rollout is expand-only: leave `MANTAU_CONTROL_PLANE_MODE=disabled` while old
servers/agents coexist, deploy the schema/server, then opt new agents into
polling. Old agents keep using `/ingest`, heartbeats, frames, and events; queued
commands are simply unused. Rollback means disabling the control plane and
turning off agent polling. Keep the additive tables and columns in place; no
down migration or data deletion is required.

## Test

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

46 tests, all offline: real HMAC signing/verification (not stubbed crypto),
real SQLite round-trips including the dedupe ledger's uniqueness constraint,
and a real ASGI `TestClient` exercising the full ingest -> dispatch -> event
path together.

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
- **Contacts and push devices remain account-global.** Agent control is
  owner-scoped, but those older subsystems have not been migrated in this
  additive stage.
- **Agent secrets are stored in plain SQLite columns**, same simplification
  as mantau-backend-rtsp's camera passwords.
- **Docker Compose is unverified end-to-end** (`../docker/compose.yaml`) --
  run it against a live Docker engine before a demo depends on it.
