# Backend Location / Compute Targets — Code Review Round 6

Date: 2026-10-11

## Scope

This round reviewed the release-blocker fixes after round 5:

- immutable target workdir binding for one C3 session;
- refusal of C2 resumes without a frozen target workdir;
- migration of legacy C3 `code_run` rows;
- restriction of that migration to `kind = 'code_run'`;
- unresolved reporting for legacy rows whose target workdir cannot be recovered.

## Independent review

The independent reviewer found no remaining code-level blocker.

The C3 manager now binds the first remote session workdir and rejects a later
job whose snapshot is missing or differs. The C2 bridge refuses a remote row
without an immutable alias/workdir snapshot instead of allowing the plane to
generate a new workdir. Migration recognizes `runtime='local'` plus
`inputs.location='remote:<alias>'` only when `kind='code_run'`; other local job
kinds remain untouched. Legacy C2/C3 rows without a recoverable workdir are
recorded as unresolved.

## Evidence

- Focused migration/bridge/kernel tests: **75 passed**.
- Backend release-risk suite: **319 passed, 14 skipped**.
- Backend `compileall`: passed.
- Backend `git diff --check`: passed.
- App unit suite covering migration, aliases/files, onboarding and runtime
  activation: **43 passed**.
- App `tsc --noEmit`: passed.
- App `git diff --check`: passed.

## Release status

**NEEDS_REVISION for release validation; code review passes.**

The following evidence is still required before calling the release complete:

1. live FastAPI/uvicorn plus SSH/SFTP checks, including non-GNU hosts,
   symlink handling, Range responses and the 8 MiB preview limit;
2. real C1/C2/C3 runs against two compute targets, target edit/delete and
   Backend restart recovery;
3. Electron end-to-end checks across profiles and provider/key boundaries.

No commit or push was performed.
