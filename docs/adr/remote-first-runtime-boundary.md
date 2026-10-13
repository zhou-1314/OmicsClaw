# ADR: Remote-first runtime boundary

Updated 2026-10-13. The desktop Jobs API is the execution boundary. A remote HTTP backend and a compute target reached by SSH are separate connections. Existing Remote Backend mode may execute on the backend host without enabling the SSH compute plane.

## Configuration and compatibility

`OMICSCLAW_REMOTE_EXECUTION` is the compute-plane feature flag (default false). `entry/config.py` parses it; `entry/assembly.py::_build_remote` creates the plane only when enabled. `execution_location=local|remote|unset` is location metadata, not a second feature flag. `desktop/server.py::health_payload` advertises the actual `app.remote` binding. A requested remote runtime without that binding is refused; it does not silently execute locally.

| Setting | Compute plane | Existing Desktop API |
|---|---|---|
| Flag off | Not assembled; remote-specific calls refuse | Chat, Jobs and file routes remain available |
| Flag on | Persistent target registry, SSH jobs and remote kernel binding | Same routes, with additive remote capabilities |

`tests/remote/test_release_switch_contract.py` verifies both settings from environment parsing through real assembly and authenticated health, Chat SSE, Jobs and file download. Existing switch, assembly and server-binding tests cover the same layers individually.

## Recovery guarantees

Remote Jobs persist their execution references before observation. A new backend reconciles those handles without submitting again, restores terminal status, output references and exit evidence, and supports cancellation. Unreachable targets retain an `unknown` state for subsequent observation. Repeated reconciliation must not duplicate artifacts or verification events. Startup lifespan and active Run observation invoke reconciliation.

A live backend can rebuild a broken SSH tunnel and Jupyter channels while preserving the remote kernel namespace. Kernel identity derives from host, session, language, PID and start time; historical Jobs keep their original identity after a new kernel starts. This does not implement cross-process rehydration of Jupyter clients: after backend restart, a recorded kernel without a live manager binding cannot execute or promise its namespace is recoverable. The workbench marks a recorded kernel without a live manager binding as `lost`: this means the backend lost its control handle, not that the remote OS process has died. A bound kernel with an unreachable heartbeat is `unknown`. A fresh kernel receives a different identity. Do not kill an unobserved remote process merely because its local binding was lost.

Real tests use localhost SSH and subprocesses, with actual tunnel termination, kernel execution and backend restart. Slurm lifecycle logic has unit coverage; no live Slurm allocation was available for acceptance. App tests use a real backend, Next proxy and Chromium, with synthetic data and no external model.

## Rollback procedure

1. Stop accepting new submissions; retain remote execution IDs and decide whether to let active jobs finish or cancel them through their owner. Stopping a backend alone does not stop its remote jobs.
2. Stop only the deployment being rolled back. Back up its configured state databases and configuration as described in `run-domain-contract.md`; retain a copy of the tested source and uncommitted integration patch.
3. Set `OMICSCLAW_REMOTE_EXECUTION=false`, deploy the previous compatible source, and restart. Verify health reports `remote_execution=false`, old Chat/Jobs/files work, and remote requests fail explicitly. The flag disables compute-plane assembly; it does not reverse schema changes or delete remote state.
4. To resume the new plane, restore the matching source/configuration, enable the flag and reconcile saved references before submitting replacement work. Never delete a handle to clear an unknown status.

Related code: `remote/{jobs,reconcile,store,kernel}.py`, `entry/desktop/{jobs_manager,jobs,server}.py`, and the App runtime activation and Run SSE proxy paths.
