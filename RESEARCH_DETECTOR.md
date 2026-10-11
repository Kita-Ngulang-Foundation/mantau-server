# Configure, review and roll back research fall detection

Use the existing authenticated settings API. `GET
/cameras/{camera_id}/detection-settings` returns the current `settings`; update
its `fall` object, then `PUT` the complete `settings` back to the same URL,
preserving `version`. Defaults:

```json
{"model":"research","tau_loose":0.92,"tau_high":0.98,"confirm_loose_s":15.0,"confirm_high_s":5.0,"down_share":0.6,"min_track_age_s":0.0,"max_edge_frac":1.0}
```

Set `fall.model` to `s1b` for rollback. The next uploaded frame applies the new
configuration; any pending candidate is logged as interrupted. Activity rules
retain their existing thresholds and logic, including the 30 s floor backup.
The older `fall.min_confidence` field filters S1b events; research uses its
explicit `tau_loose` and `tau_high` thresholds instead.

Research candidates are stored in `events` under the camera's household and
agent, with one event id from candidacy through resolution. `signals` records
`peak_p`, `down_fraction`, `tier` (1=A, 2=B), `candidate_outcome` (0=pending,
1=confirmed, 2=got up, 3=insufficient down, 4=settings changed), and timing.
Only outcome 1 enters the FCM outbox; configured exclusions/disabled detection
also suppress delivery. Half-second snapshots support recovery after restart.
An independent timer confirms a lost track last seen down at its original
deadline, including when uploads stop. Restart downtime can delay delivery.

Latency traces start at candidate frame capture and stamp confirmation separately
from FCM submission. FCM transport acceptance does not measure phone receipt;
the 5/15 s confirmation plus under 5 s delivery target needs a staged camera to
phone measurement before rollout. Models run in shared, CPU-only ONNX sessions;
per-camera tracking state remains separate. Settings are exposed through this
API and preserved by the app; no additional Flutter settings UI is introduced.

Candidate updates preserve the history cursor and human review metadata.
Duplicate requests/timer retries cannot resend an alert or regress a terminal
candidate to pending. Restart recovery checks the original agent, camera and
household ownership before storing or queuing an event.
