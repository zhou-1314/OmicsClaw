"""Workspace registration for mutable scientific environments, separate from supply pins."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from omicsclaw.memory.database import Database
from omicsclaw.entry.notebook.security import environment_gate, environment_gate_sync

from .environment_probe import probe_environment

REGISTRY_SCHEMA_VERSION = 1
MAX_ENVIRONMENTS = 64
PAGE_BYTES = 384 * 1024


def derive_environment_display_name(executable: str, language: str = "python") -> str:
    if not isinstance(executable, str) or not executable:
        return language
    m = re.search(r"/envs/([^/]+)/bin/[^/]+$", executable)
    if m:
        return m.group(1)
    m = re.search(r"/(\.venv[^/]*)/bin/[^/]+$", executable)
    if m:
        return m.group(1)
    m = re.search(
        r"/(?:conda|miniconda\d*|anaconda\d*|miniforge\d*|mambaforge)/bin/[^/]+$",
        executable,
    )
    if m:
        return "base"
    parts = Path(executable).parts
    if (
        len(parts) >= 3
        and parts[-2] == "bin"
        and parts[-3]
        not in {
            "/",
            "usr",
            "local",
            "opt",
            "root",
            "home",
            "var",
            "tmp",
            "etc",
            "System",
            "Library",
        }
    ):
        return parts[-3]
    return language


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def validate_spec(document):
    if (
        not isinstance(document, dict)
        or set(document)
        != {"language", "executable", "libraryPaths", "requiredPackages"}
        or not isinstance(document["language"], str)
        or document["language"] not in {"python", "r"}
    ):
        raise ValueError("invalid_environment_spec")
    paths, packages = document["libraryPaths"], document["requiredPackages"]
    if (
        not isinstance(paths, list)
        or len(paths) > 16
        or (document["language"] == "r" and not paths)
        or (document["language"] == "python" and paths)
        or not isinstance(packages, list)
        or len(packages) > 32
        or any(
            not isinstance(p, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,95}", p)
            for p in packages
        )
    ):
        raise ValueError("invalid_environment_spec")
    for value in [document["executable"], *paths]:
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or len(value.encode()) > 4096
            or any(c in value for c in "\0\n\r")
            or any(part in {".", ".."} for part in value.split("/"))
        ):
            raise ValueError("invalid_environment_path")
    return {
        **document,
        "executable": str(Path(document["executable"])),
        "libraryPaths": list(dict.fromkeys(str(Path(p)) for p in paths)),
        "requiredPackages": sorted(set(packages)),
    }


class EnvironmentRegistry:
    """Persist observations; a changed environment stays blocked until explicitly accepted."""

    def __init__(
        self,
        database: Database,
        workspace: Path,
        *,
        wrapper=None,
        probe=probe_environment,
        read_guard=None,
    ):
        self.database, self.workspace = database, workspace
        self.wrapper, self.probe = wrapper, probe
        self.read_guard = read_guard
        self._probe_lock = asyncio.Lock()

    def _row(self, connection, environment_id):
        row = connection.execute(
            "SELECT * FROM registered_environments WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
        if row is None:
            raise ValueError("environment_not_found")
        revision = connection.execute(
            "SELECT * FROM registered_environment_revisions WHERE environment_id=? AND revision=?",
            (environment_id, row["revision"]),
        ).fetchone()
        spec = json.loads(row["spec_json"])
        display_name = derive_environment_display_name(
            spec.get("executable", ""), spec.get("language", "python")
        )
        return {
            "schemaVersion": REGISTRY_SCHEMA_VERSION,
            "kind": "registered",
            "environmentId": row["environment_id"],
            "displayName": display_name,
            "condaEnv": display_name,
            "spec": spec,
            "revision": row["revision"],
            "acceptedRevision": row["accepted_revision"],
            "status": row["status"],
            "reason": row["reason"],
            "checkedAt": row["checked_at"],
            "fingerprint": revision["fingerprint"] if revision else None,
            "observation": json.loads(revision["observation_json"])
            if revision
            else None,
        }

    def get(self, environment_id):
        if not isinstance(environment_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", environment_id
        ):
            raise ValueError("invalid_environment_id")
        return self.database.run(lambda c: self._row(c, environment_id))

    def get_revision(self, environment_id, revision):
        def read(c):
            row = c.execute(
                "SELECT observation_json FROM registered_environment_revisions WHERE environment_id=? AND revision=?",
                (environment_id, revision),
            ).fetchone()
            if row is None:
                raise ValueError("environment_revision_not_found")
            return json.loads(row[0])

        return self.database.run(read)

    def page(self, *, after="", limit=20):
        if (
            not isinstance(after, str)
            or after
            and not re.fullmatch(r"[a-f0-9]{32}", after)
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValueError("invalid_environment_page")

        def read(c):
            ids = c.execute(
                "SELECT environment_id FROM registered_environments WHERE environment_id>? ORDER BY environment_id LIMIT ?",
                (after, limit + 1),
            ).fetchall()
            rows, size = [], 0
            for item in ids[:limit]:
                row = self._row(c, item[0])
                # Inventories remain available through get; lists fit the control frame.
                if row["observation"]:
                    row["observation"] = {
                        k: v for k, v in row["observation"].items() if k != "packages"
                    }
                cost = len(_json(row).encode())
                if size + cost > PAGE_BYTES:
                    break
                rows.append(row)
                size += cost
            more = len(ids) > len(rows)
            return {
                "environments": rows,
                "nextCursor": rows[-1]["environmentId"] if more and rows else None,
            }

        return self.database.run(read)

    async def register(self, document):
        spec = validate_spec(document)
        encoded = _json(spec)
        digest = hashlib.sha256(encoded.encode()).hexdigest()

        def insert(c):
            existing = c.execute(
                "SELECT environment_id FROM registered_environments WHERE spec_hash=?",
                (digest,),
            ).fetchone()
            if existing:
                return existing[0]
            if (
                c.execute("SELECT count(*) FROM registered_environments").fetchone()[0]
                >= MAX_ENVIRONMENTS
            ):
                raise ValueError("environment_registry_full")
            identifier = uuid.uuid4().hex
            c.execute(
                "INSERT INTO registered_environments(environment_id,spec_hash,spec_json,created_at) VALUES(?,?,?,?)",
                (identifier, digest, encoded, time.time()),
            )
            return identifier

        return await self.preflight(self.database.run(insert))

    async def preflight(self, environment_id):
        async with environment_gate(self.workspace):
            return await self._preflight(environment_id)

    async def _preflight(self, environment_id):
        """Called by an owner that already holds the cross-process gate."""
        # One probe per controller bounds subprocess count and serializes observations.
        async with self._probe_lock:
            before = self.get(environment_id)
            if self.read_guard:
                self.read_guard.check(before)
            self.database.run(
                lambda c: c.execute(
                    "UPDATE registered_environments SET status='unverified',reason='probe_in_progress' WHERE environment_id=?",
                    (environment_id,),
                )
            )
            try:
                observation = await self.read_guard.probe(before, self.probe, wrapper=self.wrapper) if self.read_guard else await self.probe(
                    before["spec"], self.workspace, wrapper=self.wrapper
                )
            except (ValueError, OSError) as error:
                from omicsclaw.entry.environment_coordination import CoordinationError
                if isinstance(error, CoordinationError):
                    raise
                reason = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "environment_path_unavailable"
                )
                self.database.run(
                    lambda c: c.execute(
                        "UPDATE registered_environments SET status='unavailable',reason=?,checked_at=? WHERE environment_id=?",
                        (reason, time.time(), environment_id),
                    )
                )
                return self.get(environment_id)
            digest = hashlib.sha256(_json(observation).encode()).hexdigest()

            def save(c):
                current = self._row(c, environment_id)
                revision = current["revision"]
                if digest != current["fingerprint"]:
                    revision += 1
                    c.execute(
                        "INSERT INTO registered_environment_revisions VALUES(?,?,?,?,?)",
                        (
                            environment_id,
                            revision,
                            digest,
                            _json(observation),
                            time.time(),
                        ),
                    )
                accepted = current["acceptedRevision"]
                if current["revision"] == 0 and not observation["missing"]:
                    accepted = revision
                status = (
                    "missing_dependencies"
                    if observation["missing"]
                    else "ready"
                    if revision == accepted
                    else "changed"
                )
                reason = (
                    "missing:" + ",".join(observation["missing"])
                    if observation["missing"]
                    else "environment_changed"
                    if status == "changed"
                    else ""
                )
                c.execute(
                    "UPDATE registered_environments SET revision=?,accepted_revision=?,status=?,reason=?,checked_at=? WHERE environment_id=?",
                    (revision, accepted, status, reason, time.time(), environment_id),
                )
                return self._row(c, environment_id)

            return self.database.run(save)

    def accept(self, environment_id, revision):
        with environment_gate_sync(self.workspace):
            return self._accept(environment_id, revision)

    def _accept(self, environment_id, revision):
        current = self.get(environment_id)
        if self.read_guard:
            self.read_guard.check(current)
        notebook_path = self.workspace / ".omicsclaw/notebooks.db"
        if notebook_path.exists():
            import sqlite3
            with sqlite3.connect(f"file:{notebook_path}?mode=ro", uri=True) as notebook:
                exists = notebook.execute("SELECT 1 FROM sqlite_master WHERE name='notebook_runtime_kernels'").fetchone()
                if exists and notebook.execute(
                    "SELECT 1 FROM notebook_runtime_kernels WHERE environment_id=? "
                    "AND environment_revision!=? AND state NOT IN ('released','lost') LIMIT 1",
                    (environment_id, revision),
                ).fetchone():
                    raise ValueError("environment_busy")

        def update(c):
            row = self._row(c, environment_id)
            if type(revision) is not int or revision != row["revision"]:
                raise ValueError("environment_revision_conflict")
            if row["status"] not in {"changed", "ready"}:
                raise ValueError("environment_not_ready")
            c.execute(
                "UPDATE registered_environments SET accepted_revision=?,status='ready',reason='' WHERE environment_id=?",
                (revision, environment_id),
            )
            return self._row(c, environment_id)

        return self.database.run(update)

    def require_ready(self, environment_id, revision):
        """The owner must preflight, then fence admission on the accepted revision."""
        row = self.get(environment_id)
        if (
            row["status"] != "ready"
            or type(revision) is not int
            or row["revision"] != revision
        ):
            raise ValueError("environment_revision_conflict")
        return row
