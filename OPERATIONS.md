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

Stop the server before taking a database and recording filesystem snapshot.
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
foreign keys, exclude interrupted clip parts, and verify every file hash.
First start the restored image against the isolated directory with matching
keys and outbound delivery disabled. Verify household/settings/enrollment,
authenticated clip access and outbox idempotency before a deliberate cutover.
An image rollback alone cannot undo changed database state.

## Resource and delivery diagnostics

Hourly maintenance prunes expired events, inference dedupe results and recordings.
Defaults: 30-day event/clip retention, 20 MiB per upload, 1 GiB per household,
5 GiB globally. Clip admission rejects new data at quota with HTTP 507 and
preserves existing history. Staged replacements require additional free disk
space. These are configured bounds; measured sustainable camera capacity is
still a release gate. Eight detector sessions/two workers is an admission
setting, not a throughput guarantee.

`GET /households/{id}/diagnostics` requires household membership and reports
retention, quota use, inference availability and delivery states. Pending/failed
delivery, HTTP 507, detector unavailable, stale inference and maintenance errors
need operator attention. Log/monitor these states and disk pressure outside the
single server; caregiver notification delivery cannot serve as its own outage
alarm. A provider delivery acknowledgement is not a caregiver read receipt.

Release remains gated on a verified runtime backup, isolated staging fault
tests, real camera/model evaluation, signed upgrade installation and physical
phone/Pi recovery and endurance. Do not fault-test the production environment.
