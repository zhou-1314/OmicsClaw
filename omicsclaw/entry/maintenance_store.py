"""Durable maintenance commands, bounded observers, and fixed owner work items."""

from __future__ import annotations

import base64
import hashlib
import json
import time

from .environment_coordination import CoordinationError, encoded

SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_records(
 maintenance_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
 owner_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL, owner_process_json TEXT NOT NULL,
 document_json TEXT NOT NULL, private_json TEXT NOT NULL, created_seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS maintenance_workspace_created ON maintenance_records(workspace_id,created_seq);
CREATE TABLE IF NOT EXISTS maintenance_commands(
 workspace_id TEXT NOT NULL, command_id TEXT NOT NULL, digest TEXT NOT NULL,
 receipt_json TEXT NOT NULL, PRIMARY KEY(workspace_id,command_id));
CREATE TABLE IF NOT EXISTS maintenance_events(
 maintenance_id TEXT NOT NULL, sequence INTEGER NOT NULL, document_json TEXT NOT NULL,
 PRIMARY KEY(maintenance_id,sequence));
CREATE TABLE IF NOT EXISTS maintenance_impacts(
 maintenance_id TEXT NOT NULL,reservation_id TEXT NOT NULL,document_json TEXT NOT NULL,
 PRIMARY KEY(maintenance_id,reservation_id));
CREATE TABLE IF NOT EXISTS maintenance_actions(
 action_id TEXT PRIMARY KEY, maintenance_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
 owner_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL, kind TEXT NOT NULL,
 document_json TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT);
CREATE INDEX IF NOT EXISTS maintenance_action_owner ON maintenance_actions(workspace_id,owner_id,owner_epoch,state);
"""
TERMINAL = {
    "completed",
    "cancelled_before_mutation",
    "plan_stale",
    "needs_repair",
    "restart_required",
}


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def key(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode()) <= 128
        or any(ord(c) < 33 for c in value)
    ):
        raise CoordinationError("environment_maintenance_invalid_request")
    return value


def impact_row(row):
    identity = json.loads(row["identity_json"])["identity"]
    return {
        "reservationId": row["reservation_id"],
        "workspaceId": row["workspace_id"],
        "kind": row["kind"],
        "state": row["state"],
        "hasAnalysis": bool(row["has_analysis"]),
        "uncertainExit": bool(row["uncertain_exit"]),
        **{
            k: identity[k]
            for k in (
                "notebookId",
                "language",
                "generation",
                "environmentId",
                "environmentRevision",
            )
            if k in identity
        },
        "kernelInstanceId": row["resource_id"]
        if row["kind"] == "kernel"
        else identity.get("kernelInstanceId"),
        "jobId": row["resource_id"] if row["kind"] == "job" else None,
    }


class MaintenanceStore:
    def __init__(self, coordinator, scope, owner):
        self.coordinator, self.scope, self.owner = coordinator, dict(scope), dict(owner)
        with coordinator.transaction() as c:
            c.executescript(SCHEMA)

    def row(self, c, mid, *, scoped=True):
        key(mid)
        row = c.execute(
            "SELECT * FROM maintenance_records WHERE maintenance_id=?", (mid,)
        ).fetchone()
        if row is None or scoped and row["workspace_id"] != self.scope["workspaceId"]:
            raise CoordinationError("environment_maintenance_not_found")
        return row

    def read(self, mid, *, scoped=True):
        with self.coordinator.transaction() as c:
            row = self.row(c, mid, scoped=scoped)
            return json.loads(row["document_json"]), json.loads(row["private_json"])

    def fence_view(self, c, document):
        roots = document["plan"]["writeRoots"]
        fences = list(self.coordinator._conflicts(c, roots))
        return {
            "blocked": bool(fences),
            "maintenanceIds": [r["maintenance_id"] for r in fences],
            "damageEpochs": [
                r["fence_seq"] for r in fences if r["state"] == "needs_repair"
            ],
            "reason": "environment_maintenance_needs_repair"
            if any(r["state"] == "needs_repair" for r in fences)
            else "environment_maintenance_frozen"
            if fences
            else "",
        }

    def public(self, mid, *, scoped=True):
        with self.coordinator.transaction() as c:
            value = json.loads(self.row(c, mid, scoped=scoped)["document_json"])
            value["environmentFence"] = self.fence_view(c, value)
            if len(encoded(value).encode()) > 128 * 1024:
                raise CoordinationError("environment_maintenance_resource_limit")
            return value

    def old(self, action, params):
        command = key(params.get("commandId"))
        with self.coordinator.transaction() as c:
            row = c.execute(
                "SELECT * FROM maintenance_commands WHERE workspace_id=? AND command_id=?",
                (self.scope["workspaceId"], command),
            ).fetchone()
            if row is None:
                return None
            if row["digest"] != digest([action, params]):
                raise CoordinationError("environment_maintenance_command_conflict")
            receipt = json.loads(row["receipt_json"])
        return {
            "receipt": receipt,
            "maintenance": self.public(receipt["maintenanceId"]),
        }

    def receipt(self, params):
        if set(params) - {"commandId", "maintenanceId"}:
            raise CoordinationError("environment_maintenance_invalid_request")
        with self.coordinator.transaction() as c:
            row = c.execute(
                "SELECT receipt_json FROM maintenance_commands WHERE workspace_id=? AND command_id=?",
                (self.scope["workspaceId"], key(params.get("commandId"))),
            ).fetchone()
        if row is None:
            return {"found": False, "receipt": None}
        value = json.loads(row[0])
        if (
            params.get("maintenanceId", value["maintenanceId"])
            != value["maintenanceId"]
        ):
            raise CoordinationError("environment_maintenance_not_found")
        return {
            "found": True,
            "receipt": value,
            "maintenance": self.public(value["maintenanceId"]),
        }

    def accept(self, c, document, action, params):
        receipt = {
            "commandId": params["commandId"],
            "maintenanceId": document["maintenanceId"],
            "action": action,
            "planRevision": document["planRevision"],
            "planDigest": document["planDigest"],
            "acceptedAt": time.time(),
            "status": "accepted",
        }
        c.execute(
            "INSERT INTO maintenance_commands VALUES(?,?,?,?)",
            (
                self.scope["workspaceId"],
                key(params["commandId"]),
                digest([action, params]),
                encoded(receipt),
            ),
        )
        return receipt

    def write(self, c, document, *, event=None):
        document["statusSeq"] = self.coordinator.sequence(c)
        if len(encoded(document).encode()) > 120 * 1024:
            raise CoordinationError("environment_maintenance_resource_limit")
        c.execute(
            "UPDATE maintenance_records SET document_json=? WHERE maintenance_id=?",
            (encoded(document), document["maintenanceId"]),
        )
        if event is not None:
            data = {
                "sequence": document["statusSeq"],
                "time": time.time(),
                "state": document["state"],
                **event,
            }
            if len(encoded(data).encode()) > 8192:
                raise CoordinationError("environment_maintenance_resource_limit")
            c.execute(
                "INSERT INTO maintenance_events VALUES(?,?,?)",
                (document["maintenanceId"], document["statusSeq"], encoded(data)),
            )

    def update(self, mid, *, event=None, **values):
        with self.coordinator.transaction() as c:
            row = self.row(c, mid)
            if (row["owner_id"], row["owner_epoch"]) != (
                self.owner["ownerId"],
                self.owner["ownerEpoch"],
            ):
                raise CoordinationError("environment_maintenance_owner_changed")
            document = json.loads(row["document_json"])
            document.update(values)
            self.write(c, document, event=event)
        return document

    def private_update(self, mid, **values):
        with self.coordinator.transaction() as c:
            row = self.row(c, mid)
            if (row["owner_id"], row["owner_epoch"]) != (
                self.owner["ownerId"],
                self.owner["ownerEpoch"],
            ):
                raise CoordinationError("environment_maintenance_owner_changed")
            private = json.loads(row["private_json"])
            private.update(values)
            c.execute(
                "UPDATE maintenance_records SET private_json=? WHERE maintenance_id=?",
                (encoded(private), mid),
            )
            return private

    def create(self, document, private, impact, action, params):
        with self.coordinator.transaction() as c:
            if (
                c.execute(
                    "SELECT count(*) FROM maintenance_records WHERE workspace_id=?",
                    (self.scope["workspaceId"],),
                ).fetchone()[0]
                >= 256
            ):
                raise CoordinationError("environment_maintenance_resource_limit")
            c.execute(
                "INSERT INTO maintenance_records VALUES(?,?,?,?,?,?,?,?)",
                (
                    document["maintenanceId"],
                    self.scope["workspaceId"],
                    self.owner["ownerId"],
                    self.owner["ownerEpoch"],
                    encoded(self.owner["process"]),
                    encoded(document),
                    encoded(private),
                    self.coordinator.sequence(c),
                ),
            )
            for row in impact:
                value = impact_row(row)
                if len(encoded(value).encode()) > 16384:
                    raise CoordinationError("environment_maintenance_resource_limit")
                c.execute(
                    "INSERT INTO maintenance_impacts VALUES(?,?,?)",
                    (document["maintenanceId"], row["reservation_id"], encoded(value)),
                )
            self.write(c, document, event={"kind": "plan_created"})
            receipt = self.accept(c, document, action, params)
        return receipt

    def page(self, kind, params):
        mid = params.get("maintenanceId")
        if set(params) - {"maintenanceId", "cursor", "limit"} or kind not in {
            "list",
            "impact",
            "events",
        }:
            raise CoordinationError("environment_maintenance_invalid_request")
        limit = params.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 40:
            raise CoordinationError("environment_maintenance_invalid_page")
        cursor = params.get("cursor")
        after = "" if kind == "impact" else 0
        identity = [kind, self.scope, mid]
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor.encode()) > 1024:
                    raise ValueError()
                decoded = json.loads(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                )
                if decoded[:3] != identity or len(decoded) != 4:
                    raise ValueError()
                after = decoded[3]
                if (kind == "impact" and not isinstance(after, str)) or (
                    kind != "impact" and type(after) is not int
                ):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as error:
                raise CoordinationError(
                    "environment_maintenance_invalid_page"
                ) from error
        with self.coordinator.transaction() as c:
            doc = (
                json.loads(self.row(c, mid)["document_json"])
                if kind != "list"
                else None
            )
            table, column, scope_column, scope_value = {
                "list": (
                    "maintenance_records",
                    "created_seq",
                    "workspace_id",
                    self.scope["workspaceId"],
                ),
                "impact": (
                    "maintenance_impacts",
                    "reservation_id",
                    "maintenance_id",
                    mid,
                ),
                "events": ("maintenance_events", "sequence", "maintenance_id", mid),
            }[kind]
            # SQLite bounds document bytes before Python materializes page rows.
            rows = c.execute(
                f"WITH candidates AS (SELECT {column} AS item_key,document_json FROM {table} WHERE {scope_column}=? AND {column}>? ORDER BY {column} LIMIT ?), "
                "sized AS (SELECT *,SUM(length(CAST(document_json AS BLOB))) OVER (ORDER BY item_key) AS total_bytes FROM candidates) "
                "SELECT item_key,document_json FROM sized WHERE total_bytes<=? ORDER BY item_key",
                (scope_value, after, limit, 128 * 1024),
            ).fetchall()
            items, size, last = [], 0, after
            for row in rows:
                value = json.loads(row[1])
                if kind == "list":
                    value["environmentFence"] = self.fence_view(c, value)
                cost = len(encoded(value).encode())
                if size + cost > 192 * 1024 - 2048:
                    break
                size += cost
                items.append(value)
                last = row[0]
            more = (
                c.execute(
                    f"SELECT 1 FROM {table} WHERE {scope_column}=? AND {column}>? LIMIT 1",
                    (scope_value, last),
                ).fetchone()
                is not None
            )
            if more and not items:
                raise CoordinationError("environment_maintenance_resource_limit")
            next_cursor = (
                base64.urlsafe_b64encode(encoded([*identity, last]).encode())
                .decode()
                .rstrip("=")
                if more
                else None
            )
            result = {"items": items, "nextCursor": next_cursor}
            if doc is not None:
                result.update(
                    {
                        k: doc[k]
                        for k in (
                            ("planRevision", "planDigest")
                            if kind == "impact"
                            else ("statusSeq",)
                        )
                    }
                )
            return result
