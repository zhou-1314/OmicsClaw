"""Same-user environment reservations and maintenance fences shared across workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .notebook.security import private_directory, private_file, same_process
from .process_containment import capture_containment, contained_exit

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
INSERT OR IGNORE INTO metadata VALUES(1,0);
CREATE TABLE IF NOT EXISTS reservations(
 reservation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL,
 workspace_id TEXT NOT NULL, workspace_path TEXT NOT NULL, kind TEXT NOT NULL,
 resource_id TEXT NOT NULL, roots_json TEXT NOT NULL, identity_json TEXT NOT NULL,
 process_json TEXT, state TEXT NOT NULL, has_analysis INTEGER NOT NULL DEFAULT 0,
 created_seq INTEGER NOT NULL, updated_seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS reservations_state ON reservations(state,created_seq);
CREATE TABLE IF NOT EXISTS maintenance_fences(
 maintenance_id TEXT PRIMARY KEY, roots_json TEXT NOT NULL, state TEXT NOT NULL,
 fence_seq INTEGER NOT NULL, owner_json TEXT NOT NULL);
"""


class CoordinationError(ValueError):
    pass


def coordinator_directory():
    """Use the account database, never an App-supplied workspace or HOME override."""
    import pwd

    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    machine = Path("/etc/machine-id").read_text().strip()
    host = hashlib.sha256((machine + ":" + os.uname().nodename).encode()).hexdigest()[
        :24
    ]
    return home / ".local" / "state" / "omicsclaw" / host / "environments"


def root_evidence(path, *, role):
    selected = Path(path)
    if not selected.is_absolute() or role not in {
        "runtime_read",
        "install_write",
        "control_read",
    }:
        raise CoordinationError("environment_maintenance_invalid_scope")
    canonical = selected.resolve(strict=True)
    info = canonical.stat()
    ancestors = []
    for parent in canonical.parents:
        stat = parent.stat()
        ancestors.append([stat.st_dev, stat.st_ino])
    return {
        "path": str(selected),
        "canonicalPath": str(canonical),
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "mode": info.st_mode & 0o7777,
        "role": role,
        "ancestors": ancestors,
    }


def overlaps(left, right):
    one, two = [left["device"], left["inode"]], [right["device"], right["inode"]]
    return one == two or one in right["ancestors"] or two in left["ancestors"]


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_roots(roots, role):
    if (
        not isinstance(roots, list)
        or not 1 <= len(roots) <= 64
        or len(encoded(roots).encode()) > 65536
    ):
        raise CoordinationError("environment_maintenance_invalid_scope")
    fresh = []
    for root in roots:
        if not isinstance(root, dict) or root.get("role") not in role:
            raise CoordinationError("environment_maintenance_invalid_scope")
        now = root_evidence(root["path"], role=root["role"])
        if now != root:
            raise CoordinationError("environment_maintenance_scope_changed")
        if now not in fresh:
            fresh.append(now)
    return fresh


class EnvironmentCoordinator:
    """Short SQLite transactions never call another owner or wait for computation."""

    def __init__(self, directory, *, process_probe=same_process):
        self.directory = private_directory(Path(directory))
        self.path = self.directory / "coordinator.db"
        os.close(private_file(self.path))
        self.process_probe = process_probe
        with self.transaction() as connection:
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(reservations)")
            }
            if "containment_json" not in columns:
                connection.execute(
                    "ALTER TABLE reservations ADD COLUMN containment_json TEXT"
                )
            if "uncertain_exit" not in columns:
                connection.execute(
                    "ALTER TABLE reservations ADD COLUMN uncertain_exit INTEGER NOT NULL DEFAULT 0"
                )
            if "dispatch_state" not in columns:
                connection.execute(
                    "ALTER TABLE reservations ADD COLUMN dispatch_state TEXT NOT NULL DEFAULT 'pending'"
                )

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            if connection.in_transaction:
                connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def sequence(c):
        c.execute("UPDATE metadata SET sequence=sequence+1 WHERE singleton=1")
        return c.execute("SELECT sequence FROM metadata WHERE singleton=1").fetchone()[
            0
        ]

    @staticmethod
    def _conflicts(c, roots):
        for row in c.execute(
            "SELECT * FROM maintenance_fences WHERE state!='closed' ORDER BY maintenance_id"
        ):
            if any(
                overlaps(a, b) for a in roots for b in json.loads(row["roots_json"])
            ):
                yield row

    def get(self, reservation_id):
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            return dict(row) if row else None

    def workspace_reservations(self, workspace_id):
        with self.transaction() as c:
            return [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM reservations WHERE workspace_id=? AND state!='closed' ORDER BY reservation_id LIMIT 4097",
                    (workspace_id,),
                )
            ]

    def adopt(self, reservation_id, owner, *, previous_owner_process):
        """Only a dead owner can be replaced; pending claims remain pending."""
        if not previous_owner_process or self.process_probe(previous_owner_process):
            raise CoordinationError("environment_maintenance_owner_alive")
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise CoordinationError("environment_maintenance_unconfirmed")
            original = json.loads(row["identity_json"])["identity"].get("ownerProcess")
            if original != previous_owner_process or self.process_probe(
                previous_owner_process
            ):
                raise CoordinationError("environment_maintenance_owner_changed")
            uncertain = (
                row["kind"] == "kernel"
                and (
                    row["process_json"] is not None
                    or row["dispatch_state"] != "pending"
                )
                and not self.process_probe(json.loads(row["process_json"] or "null"))
                and not contained_exit(
                    json.loads(row["containment_json"] or "null"),
                    json.loads(row["process_json"] or "null"),
                )
            )
            if uncertain:
                c.execute(
                    "UPDATE reservations SET uncertain_exit=1 WHERE reservation_id=?",
                    (reservation_id,),
                )
            # Persist the replacement birth for another crash during reconciliation.
            document = json.loads(row["identity_json"])
            document["identity"]["ownerProcess"] = owner["process"]
            c.execute(
                "UPDATE reservations SET owner_id=?,owner_epoch=?,identity_json=?,updated_seq=? WHERE reservation_id=?",
                (
                    owner["ownerId"],
                    owner["ownerEpoch"],
                    encoded(document),
                    self.sequence(c),
                    reservation_id,
                ),
            )

    def reap_dead_controls(self):
        with self.transaction() as c:
            for row in c.execute(
                "SELECT * FROM reservations WHERE kind='control' AND state!='closed'"
            ).fetchall():
                evidence = json.loads(row["process_json"] or "null")
                if evidence is None and row["state"] == "pending":
                    evidence = json.loads(row["identity_json"])["identity"].get(
                        "ownerProcess"
                    )
                if evidence is not None and not self.process_probe(evidence):
                    c.execute(
                        "UPDATE reservations SET state='closed',updated_seq=? WHERE reservation_id=?",
                        (self.sequence(c), row["reservation_id"]),
                    )

    def confirm_containment(self, reservation_id, owner):
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            root = json.loads(row["process_json"] or "null")
            proof = capture_containment(root)
            if proof is not None:
                c.execute(
                    "UPDATE reservations SET containment_json=?,updated_seq=? WHERE reservation_id=?",
                    (encoded(proof), self.sequence(c), reservation_id),
                )
            return proof

    def recover_kernel_containment(self, reservation_id, owner, process):
        """Recover startup evidence only from the original still-live launch."""
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            recorded = json.loads(row["process_json"] or "null")
            proof = json.loads(row["containment_json"] or "null")
            if (
                row["kind"] != "kernel"
                or row["state"] == "closed"
                or row["dispatch_state"] not in {"dispatching", "started"}
                or (recorded is not None and recorded != process)
            ):
                raise CoordinationError("environment_maintenance_process_changed")
            if proof is not None:
                if proof.get("root") != process:
                    raise CoordinationError("environment_maintenance_process_changed")
                return proof
            proof = capture_containment(process)
            if proof is not None and self.process_probe(process):
                c.execute(
                    "UPDATE reservations SET process_json=?,containment_json=?,updated_seq=? WHERE reservation_id=?",
                    (
                        encoded(process),
                        encoded(proof),
                        self.sequence(c),
                        reservation_id,
                    ),
                )
                return proof
            return None

    def reap_contained(self):
        with self.transaction() as c:
            for row in c.execute(
                "SELECT * FROM reservations WHERE containment_json IS NOT NULL "
                "AND ((kind IN ('kernel','job','probe') AND state='finished') "
                "OR (kind='job' AND state='active' AND dispatch_state='started'))"
            ).fetchall():
                if contained_exit(
                    json.loads(row["containment_json"]),
                    json.loads(row["process_json"] or "null"),
                ):
                    c.execute(
                        "UPDATE reservations SET state='closed',dispatch_state='terminal',uncertain_exit=0,updated_seq=? WHERE reservation_id=?",
                        (self.sequence(c), row["reservation_id"]),
                    )

    def starting_probe(self, reservation_id, owner):
        self.check(reservation_id, owner)
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            if row["kind"] != "probe" or row["dispatch_state"] != "pending":
                raise CoordinationError("environment_maintenance_unconfirmed")
            c.execute(
                "UPDATE reservations SET dispatch_state='dispatching',updated_seq=? WHERE reservation_id=?",
                (self.sequence(c), reservation_id),
            )

    def starting_kernel(self, reservation_id, owner):
        self.check(reservation_id, owner, draining=True)
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            if row["kind"] != "kernel" or row["dispatch_state"] != "pending":
                raise CoordinationError("environment_maintenance_unconfirmed")
            c.execute(
                "UPDATE reservations SET dispatch_state='dispatching',updated_seq=? WHERE reservation_id=?",
                (self.sequence(c), reservation_id),
            )

    def dispatch_job(self, reservation_id, owner):
        self.check(reservation_id, owner, draining=True)
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            if row["kind"] != "job" or row["dispatch_state"] != "pending":
                raise CoordinationError("environment_maintenance_unconfirmed")
            # The fence may have advanced after check; installation must still
            # wait for this already admitted job, whose reservation stays live.
            roots = validate_roots(json.loads(row["roots_json"]), {"runtime_read"})
            for fence in self._conflicts(c, roots):
                if (
                    fence["state"] not in {"fencing", "draining"}
                    or row["created_seq"] >= fence["fence_seq"]
                ):
                    raise CoordinationError("environment_maintenance_frozen")
            c.execute(
                "UPDATE reservations SET dispatch_state='dispatching',updated_seq=? WHERE reservation_id=?",
                (self.sequence(c), reservation_id),
            )

    def close_unsent_job(self, reservation_id, owner):
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            if (
                row["kind"] == "job"
                and row["dispatch_state"] == "pending"
                and row["process_json"] is None
            ):
                c.execute(
                    "UPDATE reservations SET state='closed',updated_seq=? WHERE reservation_id=?",
                    (self.sequence(c), reservation_id),
                )

    def reap_unsent_jobs(self):
        with self.transaction() as c:
            for row in c.execute(
                "SELECT * FROM reservations WHERE kind='job' AND state!='closed' AND dispatch_state='pending' AND process_json IS NULL"
            ).fetchall():
                owner_process = json.loads(row["identity_json"])["identity"].get(
                    "ownerProcess"
                )
                if owner_process is not None and not self.process_probe(owner_process):
                    c.execute(
                        "UPDATE reservations SET state='closed',updated_seq=? WHERE reservation_id=?",
                        (self.sequence(c), row["reservation_id"]),
                    )

    def finish_job(self, reservation_id, owner, process):
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            if (
                row["kind"] != "job"
                or json.loads(row["process_json"] or "null") != process
                or not (
                    self.process_probe(process)
                    or contained_exit(
                        json.loads(row["containment_json"] or "null"), process
                    )
                )
            ):
                raise CoordinationError("environment_maintenance_process_changed")
            c.execute(
                "UPDATE reservations SET state='finished',dispatch_state='terminal',updated_seq=? WHERE reservation_id=?",
                (self.sequence(c), reservation_id),
            )

    def reap_finished_jobs(self):
        self.reap_contained()
        with self.transaction() as c:
            for row in c.execute(
                "SELECT reservation_id,process_json FROM reservations WHERE kind='job' AND state='finished' AND uncertain_exit=0"
            ).fetchall():
                evidence = json.loads(row["process_json"] or "null")
                if evidence is not None and not self.process_probe(evidence):
                    # Worker completion does not contain detached descendants.
                    c.execute(
                        "UPDATE reservations SET uncertain_exit=1,updated_seq=? WHERE reservation_id=?",
                        (self.sequence(c), row["reservation_id"]),
                    )

    def reserve(self, document, *, allowed_fences=()):
        fields = {
            "reservationId",
            "ownerId",
            "ownerEpoch",
            "workspaceId",
            "workspacePath",
            "kind",
            "resourceId",
            "readRoots",
            "identity",
        }
        if (
            not isinstance(document, dict)
            or set(document) != fields
            or document["kind"]
            not in {"kernel", "admission", "job", "control", "probe"}
        ):
            raise CoordinationError("environment_maintenance_invalid_reservation")
        if (
            any(
                not isinstance(document[k], str)
                or not 1 <= len(document[k]) <= 256
                or any(ord(c) < 32 for c in document[k])
                for k in ("reservationId", "ownerId", "workspaceId", "resourceId")
            )
            or type(document["ownerEpoch"]) is not int
            or document["ownerEpoch"] < 1
            or not isinstance(document["workspacePath"], str)
            or not Path(document["workspacePath"]).is_absolute()
            or not isinstance(document["identity"], dict)
        ):
            raise CoordinationError("environment_maintenance_invalid_reservation")
        if len(encoded(document).encode()) > 98304:
            raise CoordinationError("environment_maintenance_invalid_reservation")
        roots = validate_roots(document["readRoots"], {"runtime_read", "control_read"})
        with self.transaction() as c:
            old = c.execute(
                "SELECT identity_json FROM reservations WHERE reservation_id=?",
                (document["reservationId"],),
            ).fetchone()
            if old:
                if json.loads(old[0]) != document:
                    raise CoordinationError(
                        "environment_maintenance_reservation_conflict"
                    )
                return document["reservationId"]
            if any(
                f["maintenance_id"] not in allowed_fences
                for f in self._conflicts(c, roots)
            ) or (allowed_fences and document["kind"] != "probe"):
                raise CoordinationError("environment_maintenance_frozen")
            if (
                c.execute(
                    "SELECT COUNT(*) FROM reservations WHERE state!='closed'"
                ).fetchone()[0]
                >= 4096
            ):
                raise CoordinationError("environment_maintenance_resource_limit")
            seq = self.sequence(c)
            c.execute(
                "INSERT INTO reservations(reservation_id,owner_id,owner_epoch,workspace_id,workspace_path,kind,resource_id,roots_json,identity_json,process_json,state,has_analysis,created_seq,updated_seq) VALUES(?,?,?,?,?,?,?,?,?,NULL,'pending',0,?,?)",
                (
                    document["reservationId"],
                    document["ownerId"],
                    document["ownerEpoch"],
                    document["workspaceId"],
                    document["workspacePath"],
                    document["kind"],
                    document["resourceId"],
                    encoded(roots),
                    encoded(document),
                    seq,
                    seq,
                ),
            )
        return document["reservationId"]

    def update(
        self,
        reservation_id,
        owner,
        *,
        process=None,
        committed=False,
        has_analysis=False,
    ):
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            if row["state"] == "closed":
                raise CoordinationError("environment_maintenance_reservation_closed")
            if process is not None and not self.process_probe(process):
                raise CoordinationError("environment_maintenance_process_changed")
            if (
                row["process_json"]
                and process is not None
                and json.loads(row["process_json"]) != process
            ):
                raise CoordinationError("environment_maintenance_process_changed")
            if process is not None and row["kind"] == "job":
                if row["dispatch_state"] != "dispatching":
                    raise CoordinationError("environment_maintenance_unconfirmed")
                c.execute(
                    "UPDATE reservations SET dispatch_state='started' WHERE reservation_id=?",
                    (reservation_id,),
                )
            c.execute(
                "UPDATE reservations SET process_json=COALESCE(?,process_json), state=?,has_analysis=MAX(has_analysis,?),updated_seq=? WHERE reservation_id=?",
                (
                    encoded(process) if process else None,
                    "active" if committed or row["state"] == "active" else "pending",
                    int(has_analysis),
                    self.sequence(c),
                    reservation_id,
                ),
            )

    @staticmethod
    def _owned(c, reservation_id, owner):
        row = c.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None or (row["owner_id"], row["owner_epoch"]) != (
            owner["ownerId"],
            owner["ownerEpoch"],
        ):
            raise CoordinationError("environment_maintenance_owner_changed")
        return row

    def check(self, reservation_id, owner, *, draining=False, allow_pending=False):
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            if row["uncertain_exit"]:
                raise CoordinationError("environment_maintenance_unconfirmed")
            if row["state"] != "active" and not (
                allow_pending and row["state"] == "pending"
            ):
                raise CoordinationError("environment_maintenance_unconfirmed")
            roots = validate_roots(
                json.loads(row["roots_json"]), {"runtime_read", "control_read"}
            )
            for fence in self._conflicts(c, roots):
                if row["kind"] == "probe" and fence["maintenance_id"] in json.loads(
                    row["identity_json"]
                )["identity"].get("maintenanceFences", []):
                    continue
                if not (
                    draining
                    and fence["state"] in {"fencing", "draining"}
                    and row["created_seq"] < fence["fence_seq"]
                ):
                    raise CoordinationError("environment_maintenance_frozen")

    def close(self, reservation_id, owner, *, local_terminal=False):
        with self.transaction() as c:
            row = self._owned(c, reservation_id, owner)
            evidence = json.loads(row["process_json"] or "null")
            if not local_terminal:
                raise CoordinationError("environment_maintenance_unconfirmed")
            if (
                row["kind"] in {"kernel", "job", "probe"}
                and (
                    row["process_json"] is not None
                    or row["dispatch_state"] != "pending"
                )
                and not contained_exit(
                    json.loads(row["containment_json"] or "null"), evidence
                )
            ):
                # Root exit and a backend setting are not a proof that every
                # descendant exited. Keep the physical scope until proven.
                c.execute(
                    "UPDATE reservations SET state='finished',uncertain_exit=1,updated_seq=? WHERE reservation_id=?",
                    (self.sequence(c), reservation_id),
                )
                return False
            if row["uncertain_exit"] and not contained_exit(
                json.loads(row["containment_json"] or "null"), evidence
            ):
                raise CoordinationError("environment_maintenance_unconfirmed")
            if evidence and self.process_probe(evidence):
                raise CoordinationError("environment_maintenance_process_alive")
            c.execute(
                "UPDATE reservations SET state='closed',dispatch_state='terminal',uncertain_exit=0,updated_seq=? WHERE reservation_id=?",
                (self.sequence(c), reservation_id),
            )
            return True

    def impact(self, write_roots, *, limit=4097):
        roots = validate_roots(write_roots, {"install_write"})
        with self.transaction() as c:
            return self._impact(c, roots, limit=limit)

    @staticmethod
    def _impact(c, roots, *, limit):
        result = []
        for row in c.execute(
            "SELECT * FROM reservations WHERE state!='closed' ORDER BY reservation_id LIMIT 4097"
        ):
            if any(
                overlaps(a, b) for a in roots for b in json.loads(row["roots_json"])
            ):
                result.append(dict(row))
                if len(result) >= limit:
                    break
        return result

    def fence(self, maintenance_id, roots, owner, expected_impact_digest):
        roots = validate_roots(roots, {"install_write"})
        with self.transaction() as c:
            if next(self._conflicts(c, roots), None):
                raise CoordinationError("environment_maintenance_scope_busy")
            impact = self._impact(c, roots, limit=4097)
            if self.impact_digest(impact) != expected_impact_digest:
                raise CoordinationError("environment_maintenance_plan_stale")
            if any(row["kind"] == "control" for row in impact):
                raise CoordinationError("environment_maintenance_control_environment")
            c.execute(
                "INSERT INTO maintenance_fences VALUES(?,?,'fencing',?,?)",
                (maintenance_id, encoded(roots), self.sequence(c), encoded(owner)),
            )
            return impact

    @staticmethod
    def impact_digest(rows):
        return hashlib.sha256(
            encoded(
                [
                    {
                        k: row[k]
                        for k in (
                            "reservation_id",
                            "owner_id",
                            "owner_epoch",
                            "kind",
                            "state",
                            "has_analysis",
                            "process_json",
                            "dispatch_state",
                            "uncertain_exit",
                            "containment_json",
                        )
                    }
                    for row in rows
                ]
            ).encode()
        ).hexdigest()

    def advance_fence(self, maintenance_id, owner, state):
        if state not in {
            "draining",
            "checkpoint_barrier",
            "stopping_old_kernels",
            "installing",
            "verifying",
            "needs_repair",
            "closed",
        }:
            raise CoordinationError("environment_maintenance_invalid_state")
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM maintenance_fences WHERE maintenance_id=?",
                (maintenance_id,),
            ).fetchone()
            if row is None or json.loads(row["owner_json"]) != owner:
                raise CoordinationError("environment_maintenance_owner_changed")
            if row["state"] == "needs_repair" and state != "needs_repair":
                raise CoordinationError("environment_maintenance_needs_repair")
            transitions = {
                "fencing": {"draining", "checkpoint_barrier", "closed"},
                "draining": {"checkpoint_barrier", "closed"},
                "checkpoint_barrier": {"stopping_old_kernels", "closed"},
                "stopping_old_kernels": {"installing", "closed"},
                "installing": {"verifying", "needs_repair"},
                "verifying": {"closed", "needs_repair"},
                "needs_repair": set(),
                "closed": set(),
            }
            if state != row["state"] and state not in transitions.get(
                row["state"], set()
            ):
                raise CoordinationError("environment_maintenance_invalid_state")
            impact = self._impact(c, json.loads(row["roots_json"]), limit=4097)
            if state == "checkpoint_barrier" and any(
                r["kind"] in {"admission", "job"} or r["state"] == "pending"
                for r in impact
            ):
                raise CoordinationError("environment_maintenance_unconfirmed")
            if state == "installing" and impact:
                raise CoordinationError("environment_maintenance_unconfirmed")
            c.execute(
                "UPDATE maintenance_fences SET state=? WHERE maintenance_id=?",
                (state, maintenance_id),
            )
