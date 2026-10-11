"""Idempotent migration for immutable compute-target job snapshots.

The jobs and remote-jobs stores are separate SQLite files. This command
migrates each independently, so a failed workspace can be retried without a
cross-database transaction or implicit target fallback.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omicsclaw.memory.database import Database
from omicsclaw.tools.builtin.remote import valid_host

MIGRATION_VERSION = 1
CAPABILITIES = ("remote_exec", "remote_submit")
KERNEL_CAPABILITIES = ("remote_kernel",)


@dataclass(frozen=True)
class MigrationReport:
    scope: str
    version: int
    mode: str
    status: str
    migrated: int
    unresolved: tuple[dict[str, str], ...]
    error_code: str = ""
    backup_path: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "version": self.version,
            "mode": self.mode,
            "status": self.status,
            "migrated": self.migrated,
            "unresolved": list(self.unresolved),
            "unresolved_count": len(self.unresolved),
            "error_code": self.error_code,
            "backup_path": self.backup_path,
        }


def _json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _inspect_connection(conn: Any, scope: str) -> tuple[list[tuple[Any, ...]], list[dict[str, str]]]:
    """Read legacy rows without changing the connection or filesystem."""
    unresolved: list[dict[str, str]] = []
    rows_to_migrate: list[tuple[Any, ...]] = []
    if scope == "jobs":
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if not columns:
            return [], [{"record_key": "jobs", "reason": "missing_table", "detail": "jobs"}]
        alias_expr = "target_alias" if "target_alias" in columns else "''"
        workdir_expr = "target_workdir" if "target_workdir" in columns else "''"
        capabilities_expr = "capabilities_json" if "capabilities_json" in columns else "''"
        kind_expr = "kind" if "kind" in columns else "''"
        rows = conn.execute(
            f"SELECT id, {kind_expr}, runtime, inputs_json, {alias_expr}, {workdir_expr}, {capabilities_expr} FROM jobs"
        ).fetchall()
        for row in rows:
            job_id, kind, runtime, inputs_json, existing_alias, existing_workdir, existing_capabilities = row
            runtime = str(runtime or "local")
            kind = str(kind or "")
            decoded = _json_object(inputs_json)
            requested = decoded.get("location")
            if runtime.startswith("remote:"):
                target_runtime = runtime
                capabilities = CAPABILITIES
            elif kind == "code_run" and runtime == "local" and isinstance(requested, str) and requested.startswith("remote:"):
                target_runtime = requested
                capabilities = KERNEL_CAPABILITIES
            else:
                continue
            alias = str(existing_alias or "").strip() or target_runtime[7:]
            if not valid_host(alias):
                unresolved.append({"record_key": str(job_id), "reason": "invalid_runtime", "detail": target_runtime})
                continue
            workdir = str(existing_workdir or "").strip()
            if not workdir:
                candidate = decoded.get("target_workdir")
                if isinstance(candidate, str):
                    workdir = candidate.strip()
            if not workdir:
                unresolved.append({"record_key": str(job_id), "reason": "missing_target_workdir", "detail": alias})
                continue
            try:
                complete_capabilities = tuple(json.loads(existing_capabilities or "[]"))
            except (TypeError, ValueError):
                complete_capabilities = ()
            if (
                str(existing_alias or "").strip()
                and str(existing_workdir or "").strip()
                and complete_capabilities == capabilities
            ):
                continue
            rows_to_migrate.append((job_id, alias, workdir, capabilities))
    elif scope == "remote":
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(remote_jobs)").fetchall()}
        if not columns:
            return [], [{"record_key": "remote_jobs", "reason": "missing_table", "detail": "remote_jobs"}]
        alias_expr = "target_alias" if "target_alias" in columns else "''"
        target_workdir_expr = "target_workdir" if "target_workdir" in columns else "''"
        capabilities_expr = "capabilities_json" if "capabilities_json" in columns else "''"
        rows = conn.execute(
            f"SELECT id, host_alias, workdir, {alias_expr}, {target_workdir_expr}, {capabilities_expr} FROM remote_jobs"
        ).fetchall()
        for row in rows:
            job_id, host_alias, workdir, existing_alias, target_workdir, existing_capabilities = row
            host_alias = str(host_alias or "")
            alias = str(existing_alias or "").strip() or host_alias
            if not valid_host(alias):
                unresolved.append({"record_key": str(job_id), "reason": "invalid_host", "detail": alias})
                continue
            resolved_workdir = str(target_workdir or "").strip() or str(workdir or "").strip()
            if not resolved_workdir:
                unresolved.append({"record_key": str(job_id), "reason": "missing_target_workdir", "detail": alias})
                continue
            try:
                complete_capabilities = tuple(json.loads(existing_capabilities or "[]"))
            except (TypeError, ValueError):
                complete_capabilities = ()
            if (
                str(existing_alias or "").strip()
                and str(target_workdir or "").strip()
                and complete_capabilities == CAPABILITIES
            ):
                continue
            rows_to_migrate.append((job_id, alias, resolved_workdir, CAPABILITIES))
    else:
        raise ValueError(f"unknown migration scope: {scope}")
    return rows_to_migrate, unresolved


def _backup_database(path: Path) -> str:
    """Make a point-in-time file backup, retaining WAL/SHM sidecars."""
    stamp = str(time.time_ns())
    destination = path.with_name(path.name + f".target-migration.{stamp}.bak")
    shutil.copy2(path, destination)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(destination) + suffix))
    return str(destination)


def _write_failure_report(state_dir: Path, report: MigrationReport) -> None:
    """Keep a retryable report when the database cannot be backed up.

    A backup failure happens before a database can safely be opened for the
    migration transaction.  The sidecar is therefore the durable state for
    this failure; it also keeps the command useful for read-only or damaged
    database files.
    """
    stamp = str(time.time_ns())
    path = state_dir / f"target-migration.{report.scope}.{stamp}.failed.json"
    path.write_text(json.dumps(report.as_json(), ensure_ascii=False) + "\n", encoding="utf-8")


def migrate_database(
    database: Database,
    *,
    scope: str,
    dry_run: bool,
) -> MigrationReport:
    """Migrate one jobs database, without writes in dry-run mode."""
    mode = "dry-run" if dry_run else "apply"
    now = time.time()

    jobs, unresolved = database.run(lambda conn: _inspect_connection(conn, scope))
    if dry_run:
        return MigrationReport(
            scope=scope,
            version=MIGRATION_VERSION,
            mode=mode,
            status="unresolved" if unresolved else "complete",
            migrated=len(jobs),
            unresolved=tuple(unresolved),
        )

    def apply(conn: Any) -> None:
        conn.execute(
            "INSERT INTO target_migration_state "
            "(scope, version, status, started_at, completed_at, error_code, unresolved_count) "
            "VALUES (?, ?, 'pending', ?, NULL, '', 0) "
            "ON CONFLICT(scope) DO UPDATE SET version=excluded.version, status='pending', "
            "started_at=excluded.started_at, completed_at=NULL, error_code='', unresolved_count=0",
            (scope, MIGRATION_VERSION, now),
        )
        conn.execute("DELETE FROM target_migration_unresolved WHERE scope = ?", (scope,))
        if scope == "jobs":
            for job_id, alias, workdir, capabilities in jobs:
                conn.execute(
                    "UPDATE jobs SET target_alias=?, target_workdir=?, capabilities_json=? "
                    "WHERE id=?",
                    (alias, workdir, json.dumps(list(capabilities)), job_id),
                )
        else:
            for job_id, alias, workdir, capabilities in jobs:
                conn.execute(
                    "UPDATE remote_jobs SET target_alias=?, target_workdir=?, capabilities_json=? "
                    "WHERE id=?",
                    (alias, workdir, json.dumps(list(capabilities)), job_id),
                )
        for item in unresolved:
            conn.execute(
                "INSERT INTO target_migration_unresolved "
                "(scope, record_key, reason, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (scope, item["record_key"], item["reason"], item["detail"], now),
            )
        status = "unresolved" if unresolved else "complete"
        conn.execute(
            "UPDATE target_migration_state SET status=?, completed_at=?, "
            "unresolved_count=? WHERE scope=?",
            (status, now, len(unresolved), scope),
        )

    try:
        database.run(apply)
    except Exception as exc:  # noqa: BLE001 - persist a retryable failure state
        error_code = type(exc).__name__
        try:
            database.run(
                lambda conn: (
                    conn.execute(
                        "INSERT INTO target_migration_state(scope, version, status, started_at, "
                        "completed_at, error_code, unresolved_count) VALUES (?, ?, 'failed', ?, ?, ?, ?) "
                        "ON CONFLICT(scope) DO UPDATE SET version=excluded.version, status='failed', "
                        "completed_at=excluded.completed_at, error_code=excluded.error_code, unresolved_count=excluded.unresolved_count",
                        (scope, MIGRATION_VERSION, now, time.time(), error_code, len(unresolved)),
                    ),
                    conn.executemany(
                        "INSERT OR REPLACE INTO target_migration_unresolved(scope, record_key, reason, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                        [(scope, item["record_key"], item["reason"], item["detail"], time.time()) for item in unresolved],
                    ),
                )[-1]
            )
        except Exception:
            pass
        return MigrationReport(scope, MIGRATION_VERSION, mode, "failed", 0, tuple(unresolved), error_code)
    return MigrationReport(
        scope=scope,
        version=MIGRATION_VERSION,
        mode=mode,
        status="unresolved" if unresolved else "complete",
        migrated=len(jobs),
        unresolved=tuple(unresolved),
    )


def migrate_workspace(workspace: str | Path, *, dry_run: bool) -> list[MigrationReport]:
    """Migrate jobs.db and remote.db independently for one workspace."""
    root = Path(workspace).expanduser().resolve()
    state_dir = root / ".omicsclaw"
    reports: list[MigrationReport] = []
    for scope, filename in (("jobs", "jobs.db"), ("remote", "remote.db")):
        path = state_dir / filename
        if dry_run:
            if not path.exists():
                reports.append(MigrationReport(scope, MIGRATION_VERSION, "dry-run", "complete", 0, ()))
                continue
            try:
                uri = f"file:{path}?mode=ro"
                with sqlite3.connect(uri, uri=True) as conn:
                    rows, unresolved = _inspect_connection(conn, scope)
                reports.append(MigrationReport(scope, MIGRATION_VERSION, "dry-run", "unresolved" if unresolved else "complete", len(rows), tuple(unresolved)))
            except Exception as exc:  # malformed/legacy DBs remain explicitly unresolved
                reports.append(MigrationReport(scope, MIGRATION_VERSION, "dry-run", "failed", 0, (), type(exc).__name__))
            continue
        state_dir.mkdir(parents=True, exist_ok=True)
        backup = ""
        if path.exists():
            try:
                backup = _backup_database(path)
            except Exception as exc:  # noqa: BLE001 - report and retry later
                report = MigrationReport(
                    scope=scope,
                    version=MIGRATION_VERSION,
                    mode="apply",
                    status="failed",
                    migrated=0,
                    unresolved=(),
                    error_code="backup_copy_failed:" + type(exc).__name__,
                )
                try:
                    _write_failure_report(state_dir, report)
                except OSError:
                    pass
                reports.append(report)
                continue
        with Database(path) as database:
            report = migrate_database(database, scope=scope, dry_run=False)
        reports.append(MigrationReport(report.scope, report.version, report.mode, report.status, report.migrated, report.unresolved, report.error_code, backup))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    reports = migrate_workspace(args.workspace, dry_run=args.dry_run)
    print(json.dumps([report.as_json() for report in reports], ensure_ascii=False))
    return 0 if all(report.status != "failed" for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
