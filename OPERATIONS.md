# Integration operations

Run one server process/replica with a persistent volume mounted at `/data`.
SQLite serialization, live frames and detector sessions are process-local.
Do not increase replicas or uvicorn workers without implementing shared state
and routing. Require HTTPS for family and native clients.

The Docker build requires the private wheel from the selected immutable AI
revision and verifies `private-deps/SHA256SUMS`. Keep that directory private and
untracked. `mantau-core.ref` selects the immutable shared code. Preserve the
existing control-plane encryption key across upgrades; rotating it without a
credential migration makes queued camera credentials unreadable.

## Backup and isolated restore

Stop the server before taking a database snapshot. The server stores no clips; on
every start it deletes any clip an older version left under `/data/recordings`.
The operator must restrict access to the backup directory: it contains personal
data, agent credentials and encrypted camera commands. Store existing Firebase
and control-plane keys in the operator secret store separately. Keep backup
retention consistent with household deletion policy; restore must reapply any
deletions made after the snapshot before exposing the restored service.

```sh
python ops/backup_restore.py --service-stopped backup \
  --database /data/mantau_ld.db --recordings /data/recordings \
  --target /private-backups/NEW-SNAPSHOT --source-id TESTED-IMAGE-ID
python ops/backup_restore.py --service-stopped restore \
  --archive /private-backups/NEW-SNAPSHOT --target /isolated/NEW-RESTORE
```

These commands refuse existing destinations, check SQLite integrity and
foreign keys, exclude interrupted clip parts, and verify every file hash. A
restored snapshot from before clip removal loses its clips on first start.
First start the restored image against the isolated directory with matching
keys and outbound delivery disabled. Verify household/settings/enrollment,
relayed clip access and outbox idempotency before a deliberate cutover.
An image rollback alone cannot undo changed database state.

## Resource and delivery diagnostics

Hourly maintenance prunes expired events and inference dedupe results.
Defaults: 30-day event retention, 20 MiB per relayed clip. Clips stay on the
agents; the server passes one clip through memory per download and stores
none. These are configured bounds; measured sustainable camera capacity is
still a release gate. Eight detector sessions/two workers is an admission
setting, not a throughput guarantee.

`GET /households/{id}/diagnostics` requires household membership and reports
retention, quota use, inference availability and delivery states. Pending/failed
delivery, detector unavailable, stale inference and maintenance errors
need operator attention. Log/monitor these states and disk pressure outside the
single server; caregiver notification delivery cannot serve as its own outage
alarm. A provider delivery acknowledgement is not a caregiver read receipt.

Release remains gated on a verified runtime backup, isolated staging fault
tests, real camera/model evaluation, signed upgrade installation and physical
phone/Pi recovery and endurance. Do not fault-test the production environment.

## Field trial telemetry

The trial's telemetry lives in the same SQLite database as everything else, so
the database must survive redeploys before any household joins.

Pre-install gates:

- **G1, persistent volume.** The service must mount a persistent volume at
  `/data` with `MANTAU_DB_PATH=/data/mantau_ld.db`. Without the volume, a
  redeploy deletes the whole database. Redeploy once and confirm that an
  existing event survives it.
- **G2, build id.** `GET /health` reports the running build under `build`
  (`server`, `core`, `ai`). The server reads `MANTAU_BUILD_ID`, then
  `RAILWAY_GIT_COMMIT_SHA`, then `unknown`. After deploy, confirm the build id
  is non-empty and record which variable Railway sets.

Cohort rules:

- Telemetry is written only for households in `trial_cohort`, and only inside
  their trial dates. Add a household only after its participation agreement is
  signed.

Commands (run from `/app` in the server container against the live database
file; they wait for locks):

```sh
python ops/trial_cohort.py --db /data/mantau_ld.db add --household ID --code H01 --start 2026-10-20 --end 2026-11-02 --tz Asia/Jakarta
python ops/trial_cohort.py --db /data/mantau_ld.db end --household ID --date 2026-11-02
python ops/trial_cohort.py --db /data/mantau_ld.db list
python ops/trial_cohort.py --db /data/mantau_ld.db missed --household ID --from 2026-10-21T07:00 --to 2026-10-21T08:00 --kind fall
python ops/trial_export.py --db /data/mantau_ld.db --out DIR --kind engineering [--include-notes]
python ops/trial_export.py --db /data/mantau_ld.db --out DIR --kind investor
```

- `missed` records a fall, stillness, nocturnal movement, bathroom duration or
  other event that a family reported to the team over WhatsApp. It stores no
  free text, accepts windows of at most 24 hours, and a naive time is read in
  the household's cohort time zone.
- `trial_export.py` reads a read-only snapshot and writes only into `--out`,
  which must be empty or missing. `--kind engineering` writes combined CSV
  files, one per table, with every row keyed by cohort code in the `household`
  column; cameras, agents and members appear as indexes that are unique within
  a household. `--include-notes` adds the free-text dismiss note to
  `events.csv` and nowhere else.
  `--kind investor` writes `summary.csv` only. Each figure needs at least 3
  contributing households, and latency percentiles also need at least 10
  samples; otherwise the figure is suppressed. With fewer than 3 cohort
  households the file has only its header.
- The tz database must be present: the python slim image ships tzdata. To run
  the scripts on Windows against a copy of the database, install the `tzdata`
  package first.
- Exports contain pseudonymous, not anonymous, data. Store them privately,
  restrict access to them, and keep them out of the repository.
