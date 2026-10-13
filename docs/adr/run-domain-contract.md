# ADR: Run domain contract

Updated 2026-10-13. `omicsclaw/runs/models.py` defines the wire objects; `store.py` persists them separately from existing session storage. New fields are additive. This document describes the implemented contract, including its recovery limits.

## Objects and identity

| Object | Identity and relevant fields |
|---|---|
| Run | `run_id`, `session_id`, `plan_id`, `step_id`, `plan_steps`, `origin`, `execution_location`, `status`, `root_artifact_id`, `created_at` |
| Job | Domain `job_id`, `run_id`, actual backend `execution_ref`, `host_id`, `scheduler`, `kernel_id`, `command_digest`, `step_id`, `status` |
| Kernel | `kernel_id`, owning `run_id` plus shared `run_ids`, `session_id`, `language`, `host_id`, `environment`, `pid`, `started_at`, `last_heartbeat`, `status` |
| Artifact | `artifact_id`, `run_id`, `job_id`, `kernel_id`, `uri`, `version`, `parent_artifact_id`, `lineage_key`, `inputs`/`input_artifact_ids`, `code_ref`, `environment_ref`, `execution_ref`, `checksum`, `size`, `content_type`, `host_id`, `created_at` |
| Verification | `verification_id`, `run_id`, `job_id`, optional `artifact_id`, `artifact_version`, `check`, `verdict`, `evidence_ref`, `details`, `reviewer`, `created_at` |
| Activity | `activity_id`, `run_id`, `job_id`, `event_type`, `payload`, `created_at`; durable tool, approval and log timeline |

Old sessions receive an idempotent `legacy:<session_id>` projection with `origin=legacy`, `status=unknown`. This does not fabricate historical execution success. `session-plan:<session_id>` identifies the existing session plan; step IDs and statuses come from its PlanBook. The UI treats historical projections as read-only. `kernel/handles.py` contains analysis-object handles, so execution identity is derived from real `kernel.manager` session state instead. Jobs sharing a kernel retain stable identity; starting a new kernel does not rewrite old Jobs.

## State transitions

Self-transitions are idempotent. Run and Job transitions are checked transactionally:

| From | Allowed next states |
|---|---|
| created | queued, running, cancelled, failed |
| queued | running, cancelled, failed, lost, unknown |
| running | queued, succeeded, failed, cancelled, interrupted, lost, unknown |
| unknown | queued, running, lost, failed, cancelled |
| lost / interrupted | queued, running, failed, cancelled |
| succeeded / failed / cancelled | No further transition |

Legacy `done`, `canceled`, `cancel_requested` normalize to `succeeded`, `cancelled`, `running`. Kernel states are independently observed as `created`, `ready`, `busy`, `unknown`, `lost`, `stopped`; Job completion does not stop a persistent kernel. Retry creates a new execution rather than reversing a terminal Job.

## Persistence, events and verification

The workspace `.omicsclaw/runs.db` uses SQLite WAL with `run_schema`, `run_objects` and append-only `run_events`. Schema version is 1. Write transactions serialize concurrent updates; object and event commit together. Existing payloads may omit new fields; defaults fill them. Unknown payload fields are ignored when reading. Shared-kernel Run references are merged transactionally.

Run SSE uses the database sequence as its cursor. `Last-Event-ID` resumes durable events, control frames do not mutate domain objects, and terminal streams finish. Events include linked Jobs, shared Kernels, Artifacts, Verification and Activity. This is replay plus live incremental delivery; event compaction is not implemented.

Checks record started/finished evidence with the same identity. File existence/size/checksum, CSV/JSON/schema, h5ad structure and execution exit code are distinct checks; a passing exit code alone does not prove a valid artifact. Missing optional validation dependencies and remote-only references are `inconclusive`. Outputs remain remote references until explicitly retrieved; no automatic full download is required for reconciliation. New versions have distinct IDs and point to the prior artifact; multiple output files do not share a version ID.

Authenticated `POST /runs/{run_id}/verifications` accepts an explicit reviewer identity, verdict and matching artifact version. Cross-Run or version-mismatched submissions are rejected. A stored reviewer label is supplied by the authorized client; this is not a separate identity-attestation service.

`RunEventObserver` emits approved identifiers and enum-valued status/check/verdict attributes after commit, without code, paths, environment contents or raw activity payloads. Existing turn telemetry carries `run_id`. Exporter failure is logged and cannot undo a committed execution event. Replay does not re-export past telemetry.

## Backup, migration and rollback

Initialization is additive and does not rewrite old session databases. Direct chat projection logs SQLite/storage failures and retains chat operation. Desktop Jobs startup still requires readable Run storage; a corrupt database is an explicit startup failure, not an empty successful migration.

Before upgrading, stop writes for the deployment and record the workspace, configured database paths, source revision and configuration. Back up every existing state database using SQLite's backup API, not a bare copy of a WAL-mode main file. Example for this new store, run from the backend workspace with a new destination path:

```python
import sqlite3
from pathlib import Path

source = Path('.omicsclaw/runs.db')
backup = Path('/your/backup/directory/runs-before-upgrade.db')
if not source.is_file() or backup.exists():
    raise RuntimeError('source must exist and backup destination must be new')
with sqlite3.connect(f'file:{source.resolve()}?mode=ro', uri=True) as src:
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
        assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
```

Use the same procedure for configured session/job/remote databases; their location may be customized. Validate restoration in an isolated copy before replacing production state. To roll back, stop the affected backend, preserve the failed database together with its WAL/SHM sidecars, restore a coherent backup to a clean destination, deploy the matching previous source and run health/legacy API checks. Do not overwrite an open database or mix old rows with new WAL files. Disabling `OMICSCLAW_REMOTE_EXECUTION` is a compute-plane rollback switch, not a database restore.

New readers tolerate old object fields. Arbitrary older binaries are not guaranteed to understand every new event kind: use the saved compatible source and backup rather than claiming unrestricted downgrade compatibility. Old session data is retained even if Run projection fails.

Evidence lives in `tests/runs/`, `tests/observability/test_run_events.py`, `tests/entry/test_run_projection.py`, Desktop contract tests, real SSH tests and the App live Playwright suite. See the release report for actual commands, exit codes and deployment limitations.
