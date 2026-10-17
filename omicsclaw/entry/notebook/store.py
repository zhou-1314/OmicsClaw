"""Owner metadata beside the existing cells, executions and event tables."""

import json

from .contracts import MAX_RESPONSE_BYTES, bounded, encoded, page_limit, public_semantic
from .service import NotebookError, identifier

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_runtime_store (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
 authority_id TEXT NOT NULL, workspace_id TEXT NOT NULL, workspace_path TEXT NOT NULL,
 owner_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL, source_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_runtime_kernels (
 instance_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, generation INTEGER NOT NULL,
 session_key TEXT NOT NULL UNIQUE, environment_id TEXT NOT NULL, environment_revision INTEGER NOT NULL,
 environment_json TEXT NOT NULL, maintenance_fence INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL, process_json TEXT, created_at REAL NOT NULL, confirmed_at REAL NOT NULL,
 UNIQUE(notebook_id,generation));
CREATE INDEX IF NOT EXISTS idx_notebook_kernel_generation
 ON notebook_runtime_kernels(notebook_id,generation DESC);
CREATE TABLE IF NOT EXISTS notebook_runtime_executions (
 execution_id TEXT PRIMARY KEY, digest TEXT NOT NULL, semantic_json TEXT NOT NULL,
 instance_id TEXT NOT NULL, generation INTEGER NOT NULL, dispatch_state TEXT NOT NULL,
 cancel_requested INTEGER NOT NULL DEFAULT 0, durability TEXT NOT NULL DEFAULT 'pending',
 owner_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_runtime_commands (
 command_id TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_runtime_sequences (
 notebook_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_notebook_event_turn_seq ON turn_events(turn_id,seq);
CREATE INDEX IF NOT EXISTS idx_notebook_execution_latest
 ON notebook_executions(notebook_id,created_at DESC,execution_id DESC);
"""


def bump(connection, notebook_id):
    connection.execute(
        "INSERT INTO notebook_runtime_sequences VALUES(?,1) "
        "ON CONFLICT(notebook_id) DO UPDATE SET seq=seq+1",
        (notebook_id,),
    )


def kernel_row(row):
    if row is None:
        return None
    environment = json.loads(row["environment_json"])
    return {
        "kernelInstanceId": row["instance_id"],
        "notebookId": row["notebook_id"],
        "generation": row["generation"],
        "environmentId": row["environment_id"],
        "environmentRevision": row["environment_revision"],
        "language": row["language"],
        "state": row["state"],
        "process": json.loads(row["process_json"] or "null"),
        "confirmedAt": row["confirmed_at"],
        "maintenanceFence": row["maintenance_fence"],
        "componentSourceDigest": environment.get("componentSourceDigest", "unknown"),
        "environmentFingerprint": environment["fingerprint"],
        "environmentScope": {
            k: v
            for k, v in (environment.get("observation") or {}).items()
            if k in {"prefix", "libraryPaths", "maintenanceRoots"}
        },
    }


def migrate(c):
    columns = {
        row[1] for row in c.execute("PRAGMA table_info(notebook_runtime_kernels)")
    }
    if "language" not in columns:
        c.execute(
            "ALTER TABLE notebook_runtime_kernels ADD COLUMN language TEXT NOT NULL DEFAULT 'python'"
        )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_notebook_kernel_language ON notebook_runtime_kernels(notebook_id,language,generation DESC)"
    )


def current_kernel(c, nid, language="python"):
    return c.execute(
        "SELECT * FROM notebook_runtime_kernels WHERE notebook_id=? AND language=? ORDER BY generation DESC LIMIT 1",
        (nid, language),
    ).fetchone()


def kernel_set(c, nid):
    return {
        language: kernel_row(current_kernel(c, nid, language))
        for language in ("python", "r")
    }


def next_generation(c, nid):
    return c.execute(
        "SELECT COALESCE(MAX(generation),0)+1 FROM notebook_runtime_kernels WHERE notebook_id=?",
        (nid,),
    ).fetchone()[0]


def require_kernel(c, nid, instance, generation, language=None):
    selected = c.execute(
        "SELECT language FROM notebook_runtime_kernels WHERE notebook_id=? AND instance_id=?",
        (nid, instance),
    ).fetchone()
    if not selected or (language is not None and selected[0] != language):
        raise NotebookError("notebook_generation_changed", 409)
    row = current_kernel(c, nid, selected[0])
    if row is None or row["instance_id"] != instance or row["generation"] != generation:
        raise NotebookError("notebook_generation_changed", 409)
    if row["state"] not in {"reserved", "ready", "busy"}:
        raise NotebookError("notebook_kernel_unavailable", 409)
    return row


class OwnerViews:
    """SQL limits and serialized byte limits both apply before a response leaves."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.db

    def _cursor(self, scope, after):
        return {"store": self.runtime.store_identity, "scope": scope, "after": after}

    def _after(self, cursor, scope, initial):
        if cursor is None:
            return initial
        if (
            not isinstance(cursor, dict)
            or set(cursor) != {"store", "scope", "after"}
            or cursor["store"] != self.runtime.store_identity
            or cursor["scope"] != scope
            or type(cursor["after"]) is not type(initial)
        ):
            raise NotebookError("notebook_cursor_mismatch", 409)
        if isinstance(initial, int) and cursor["after"] < 0:
            raise NotebookError("invalid_notebook_page")
        if isinstance(initial, str) and len(cursor["after"]) > 64:
            raise NotebookError("invalid_notebook_page")
        return cursor["after"]

    def _page(self, rows, convert, key, scope, limit):
        items, size = [], 2048
        for row in rows[:limit]:
            item = convert(row)
            cost = len(encoded(item))
            if cost + size > MAX_RESPONSE_BYTES - 8192:
                break
            items.append(item)
            size += cost
        more = len(rows) > len(items)
        return {
            "items": items,
            "nextCursor": self._cursor(scope, key(rows[len(items) - 1]))
            if items and more
            else None,
        }

    def listing(self, cursor=None, limit=20):
        limit = page_limit(limit)
        after = self._after(cursor, "notebooks", "")

        def read(c):
            rows = c.execute(
                "SELECT notebook_id FROM notebook_bindings WHERE notebook_id>? "
                "ORDER BY notebook_id LIMIT ?",
                (after, limit + 1),
            ).fetchall()

            # Do not call Database.run recursively while holding its lock.
            def item(row):
                nid = row[0]
                binding = c.execute(
                    "SELECT * FROM notebook_bindings WHERE notebook_id=?", (nid,)
                ).fetchone()
                return {
                    "notebook_id": nid,
                    "binding": dict(binding),
                    "kernel": kernel_row(current_kernel(c, nid)),
                    "kernels": kernel_set(c, nid),
                    "notebookSeq": self.sequence(c, nid),
                }

            return self._page(rows, item, lambda r: r[0], "notebooks", limit)

        return bounded(self.db.run(read))

    @staticmethod
    def sequence(c, nid):
        row = c.execute(
            "SELECT seq FROM notebook_runtime_sequences WHERE notebook_id=?", (nid,)
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _cell(row):
        result = dict(row)
        for key in ("output_json", "provenance_json"):
            result[key[:-5]] = json.loads(result.pop(key) or "null")
        return result

    @staticmethod
    def _record(row):
        result = OwnerViews._cell(row)
        semantic = json.loads(result.pop("semantic_json") or "null")
        if semantic:
            result["runtime"] = public_semantic(semantic)
        else:
            result["runtime"] = {"state": "legacy_unknown"}
        result["cancelRequested"] = bool(result.pop("cancel_requested"))
        result["dispatchState"] = result.pop("dispatch_state") or "legacy_unknown"
        result["durability"] = result.get("durability") or "unknown"
        result["runtime"]["componentSourceDigest"] = result.pop("component_source_digest") or "unknown"
        return result

    @staticmethod
    def _execution_select():
        return (
            "SELECT e.execution_id,e.notebook_id,e.cell_id,e.session_epoch,e.client_command_id,"
            "e.status,e.revision,e.created_at,e.finished_at,e.trust_label,"
            "CASE WHEN length(CAST(e.output_json AS BLOB))<=65536 THEN e.output_json "
            "ELSE '{\"truncated\":true}' END AS output_json,"
            "CASE WHEN length(CAST(e.provenance_json AS BLOB))<=73728 THEN e.provenance_json "
            "ELSE '{\"truncated\":true}' END AS provenance_json,"
            "x.semantic_json,x.cancel_requested,x.dispatch_state,x.durability,p.source_digest AS component_source_digest "
            "FROM notebook_executions e LEFT JOIN notebook_runtime_executions x USING(execution_id) "
            "LEFT JOIN notebook_component_provenance p ON p.kind='execution' AND p.record_id=e.execution_id "
        )

    def execution(self, nid, eid):
        identifier(nid)
        identifier(eid)

        def read(c):
            row = c.execute(
                self._execution_select() + "WHERE e.notebook_id=? AND e.execution_id=?",
                (nid, eid),
            ).fetchone()
            if row is None:
                raise NotebookError("execution_not_found", 404)
            return self._record(row)

        return bounded(self.db.run(read))

    def snapshot(self, nid, *, cell_cursor=None, execution_cursor=None, limit=20):
        identifier(nid)
        limit = page_limit(limit)
        ca = self._after(cell_cursor, "cells:" + nid, "")
        ea = self._after(execution_cursor, "executions:" + nid, "")

        def read(c):
            cells = c.execute(
                "SELECT notebook_id,cell_id,language,revision,status,trust_label,session_epoch,"
                "client_command_id,execution_id,updated_at,"
                "CASE WHEN length(CAST(source AS BLOB))<=65536 THEN source ELSE '' END AS source,"
                "CASE WHEN length(CAST(source AS BLOB))>65536 THEN 1 ELSE 0 END AS sourceTruncated,"
                "CASE WHEN length(CAST(output_json AS BLOB))<=16384 THEN output_json ELSE '{\"truncated\":true}' END AS output_json,"
                "'{}' AS provenance_json FROM notebook_cells WHERE notebook_id=? AND cell_id>? "
                "ORDER BY cell_id LIMIT ?",
                (nid, ca, limit + 1),
            ).fetchall()
            executions = c.execute(
                self._execution_select() + "WHERE e.notebook_id=? AND e.execution_id>? "
                "ORDER BY e.execution_id LIMIT ?",
                (nid, ea, limit + 1),
            ).fetchall()
            # Divide the response budget between the independent pages.
            cp = self._page(
                cells, self._cell, lambda r: r["cell_id"], "cells:" + nid, min(limit, 1)
            )
            ep = self._page(
                executions,
                self._record,
                lambda r: r["execution_id"],
                "executions:" + nid,
                min(limit, 1),
            )
            binding = c.execute(
                "SELECT * FROM notebook_bindings WHERE notebook_id=?", (nid,)
            ).fetchone()
            kernel = current_kernel(c, nid)
            active = c.execute(
                self._execution_select()
                + "WHERE e.notebook_id=? AND e.status IN ('queued','running','outcome_unknown') "
                "AND x.instance_id IN (SELECT k.instance_id FROM notebook_runtime_kernels k WHERE k.notebook_id=e.notebook_id AND k.generation=(SELECT MAX(k2.generation) FROM notebook_runtime_kernels k2 WHERE k2.notebook_id=k.notebook_id AND k2.language=k.language) AND k.state NOT IN ('released','lost')) "
                "ORDER BY CASE e.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,e.created_at LIMIT 1",
                (nid,),
            ).fetchone()
            active = self._record(active) if active else None
            if active:
                active.pop("provenance", None)
                active.pop("output", None)
            latest = c.execute(
                self._execution_select() + "WHERE e.notebook_id=? "
                "ORDER BY e.created_at DESC,e.execution_id DESC LIMIT 1",
                (nid,),
            ).fetchone()
            latest = self._record(latest) if latest else None
            if latest:
                latest.pop("provenance", None)
                latest.pop("output", None)
            return {
                "notebook_id": nid,
                "binding": dict(binding) if binding else None,
                "cells": cp["items"],
                "executions": ep["items"],
                "cellCursor": cp["nextCursor"],
                "executionCursor": ep["nextCursor"],
                "kernel": kernel_row(kernel),
                "kernels": kernel_set(c, nid),
                "selections": {
                    language: self.runtime.requests.selection(
                        c, current_kernel(c, nid, language), language
                    )
                    for language in ("python", "r")
                },
                **self.runtime.requests.readiness(c, nid),
                "notebookSeq": self.sequence(c, nid),
                "store": self.runtime.store_identity,
                "activeExecution": active,
                "latestExecution": latest,
                "control": {
                    **self.runtime.requests.control(c, nid),
                    "helperRevision": (
                        c.execute(
                            "SELECT helper_revision FROM notebook_request_sessions WHERE session_id=?",
                            (binding["session_id"],),
                        ).fetchone()
                        or [0]
                    )[0]
                    if binding
                    else 0,
                },
            }

        return bounded(self.db.run(read))

    def events(self, nid, eid, cursor=None, limit=20):
        record = self.execution(nid, eid)
        limit = page_limit(limit)
        scope = "events:" + eid
        after = self._after(cursor, scope, 0)

        def read(c):
            rows = c.execute(
                "SELECT seq,type,CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json "
                "ELSE '{\"truncated\":true}' END AS payload_json,created_at FROM turn_events WHERE turn_id=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (eid, after, limit + 1),
            ).fetchall()

            def convert(row):
                payload = json.loads(row["payload_json"])
                return {
                    "seq": row["seq"],
                    "type": "notebook_output" if "notebookOutput" in payload else row["type"],
                    "payload": {"outputVersion": 1, "output": payload["notebookOutput"]} if "notebookOutput" in payload else payload,
                    "createdAt": row["created_at"],
                }

            page = self._page(rows, convert, lambda r: r["seq"], scope, limit)
            last = page["items"][-1]["seq"] if page["items"] else after
            committed = c.execute(
                "SELECT COALESCE(MAX(seq),0) FROM turn_events WHERE turn_id=?", (eid,)
            ).fetchone()[0]
            return {
                **page,
                "cursor": self._cursor(scope, last),
                "committedSeq": committed,
                "gap": bool(rows and rows[0]["seq"] != after + 1),
                "durability": record["durability"],
            }

        return bounded(self.db.run(read))
