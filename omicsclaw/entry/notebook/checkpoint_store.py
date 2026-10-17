"""Checkpoint intents and blockers share the scientific execution transaction."""

import hashlib
import json
import time
import uuid

from .analysis_store import entity_ref, kernel_ref
from .contracts import encoded
from .service import NotebookError
from .store import current_kernel

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_checkpoint_records (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, notebook_id TEXT NOT NULL,
 kind TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL,
 document TEXT NOT NULL CHECK(length(CAST(document AS BLOB))<=16384),
 UNIQUE(notebook_id,kind,entity_id,revision));
CREATE INDEX IF NOT EXISTS notebook_checkpoint_lookup
 ON notebook_checkpoint_records(notebook_id,kind,entity_id,revision DESC);
CREATE TABLE IF NOT EXISTS notebook_checkpoint_intents (
 operation_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, document TEXT NOT NULL,
 reserved_bytes INTEGER NOT NULL, released INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notebook_checkpoint_executions (
 execution_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, phase TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_checkpoint_mutations (
 notebook_id TEXT NOT NULL, language TEXT NOT NULL, sequence INTEGER NOT NULL, execution_id TEXT,
 PRIMARY KEY(notebook_id,language));
CREATE TABLE IF NOT EXISTS notebook_checkpoint_stages (
 execution_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_checkpoint_gc (checkpoint_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, document TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notebook_checkpoint_holds (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, checkpoint_id TEXT NOT NULL, hold_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 reference_id TEXT NOT NULL, created_at REAL NOT NULL);
"""
VERSIONS = {
    "checkpoint": "manifestRevision",
    "failure": "failureRevision",
    "operation": "statusSeq",
    "restore": "statusSeq",
}
IDS = {
    "checkpoint": "checkpointId",
    "failure": "failureId",
    "operation": "operationId",
    "restore": "operationId",
}
ACTIVE = frozenset(
    {
        "intent",
        "inspecting",
        "saving",
        "validating",
        "publishing",
        "stopping_unconfirmed",
    }
)


def fail(code="invalid_command", status=422):
    raise NotebookError("notebook_checkpoint_" + code, status)


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def reference(value, kind):
    keys = (IDS[kind], VERSIONS[kind])
    if kind == "checkpoint":
        keys += ("manifestSha256",)
    return {key: value[key] for key in keys}


class CheckpointStore:
    def __init__(self, runtime):
        self.r = runtime

    @staticmethod
    def put(c, nid, kind, value):
        raw = encoded(value)
        if len(raw) > 16384:
            fail("limit", 413)
        # Pending checkpoints may change without inventing a manifest revision.
        row = c.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM notebook_checkpoint_records WHERE notebook_id=? AND kind=? AND entity_id=?",
            (nid, kind, value[IDS[kind]]),
        ).fetchone()
        c.execute(
            "INSERT INTO notebook_checkpoint_records(notebook_id,kind,entity_id,revision,document) VALUES(?,?,?,?,?)",
            (nid, kind, value[IDS[kind]], row[0], raw.decode()),
        )

    @staticmethod
    def get(c, nid, kind, eid, revision=None):
        if (
            kind == "checkpoint"
            and c.execute(
                "SELECT 1 FROM notebook_checkpoint_gc WHERE notebook_id=? AND checkpoint_id=?",
                (nid, eid),
            ).fetchone()
        ):
            fail("not_found", 404)
        row = c.execute(
            "SELECT document FROM notebook_checkpoint_records WHERE notebook_id=? AND kind=? AND entity_id=? "
            + ("AND json_extract(document,?)=? " if revision is not None else "")
            + "ORDER BY revision DESC LIMIT 1",
            (
                nid,
                kind,
                eid,
                *(["$." + VERSIONS[kind], revision] if revision is not None else []),
            ),
        ).fetchone()
        if row is None:
            fail("not_found", 404)
        return json.loads(row[0])

    @staticmethod
    def latest(c, nid, kind, where, parameters=()):
        rows = c.execute(
            "SELECT document FROM notebook_checkpoint_records r WHERE notebook_id=? AND kind=? "
            "AND revision=(SELECT MAX(x.revision) FROM notebook_checkpoint_records x WHERE x.notebook_id=r.notebook_id AND x.kind=r.kind AND x.entity_id=r.entity_id) AND "
            + where
            + " LIMIT 33",
            (nid, kind, *parameters),
        ).fetchall()
        if len(rows) > 32:
            fail("limit", 413)
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def mutation(c, nid, language, *, advance=False, execution_id=None):
        if advance:
            c.execute(
                "INSERT INTO notebook_checkpoint_mutations VALUES(?,?,1,?) ON CONFLICT(notebook_id,language) DO UPDATE SET sequence=sequence+1,execution_id=excluded.execution_id",
                (nid, language, execution_id),
            )
        row = c.execute(
            "SELECT sequence FROM notebook_checkpoint_mutations WHERE notebook_id=? AND language=?",
            (nid, language),
        ).fetchone()
        return row[0] if row else 0

    def registration(self, c, nid, source):
        group = []
        records = c.execute(
            "SELECT document FROM notebook_analysis_entities e WHERE notebook_id=? AND kind='object' AND json_extract(document,'$.kernel.kernelInstanceId')=? AND COALESCE(json_extract(document,'$.registrationState'),'active')!='removed' AND revision=(SELECT MAX(x.revision) FROM notebook_analysis_entities x WHERE x.notebook_id=e.notebook_id AND x.kind=e.kind AND x.entity_id=e.entity_id) ORDER BY entity_id LIMIT 33",
            (nid, source["kernelInstanceId"]),
        ).fetchall()
        for record in records:
            obj = json.loads(record[0])
            if obj["kernel"] != source:
                continue
            row = c.execute(
                "SELECT document FROM notebook_analysis_specs WHERE notebook_id=? AND object_id=?",
                (nid, obj["objectId"]),
            ).fetchone()
            if row is None:
                fail("reference_stale", 409)
            spec = json.loads(row[0])
            group.append(
                {
                    "objectId": obj["objectId"],
                    "logicalKey": obj["objectId"],
                    "symbol": obj["symbol"],
                    "spec": spec,
                    "source": source,
                }
            )
        group.sort(key=lambda value: value["objectId"])
        return group, digest(group)

    def blocked(self, c, nid, language):
        operations = self.latest(
            c,
            nid,
            "operation",
            "json_extract(document,'$.source.language')=? AND json_extract(document,'$.state') NOT IN ('ready','failed')",
            (language,),
        )
        failures = self.latest(
            c,
            nid,
            "failure",
            "json_extract(document,'$.source.language')=? AND json_extract(document,'$.blocking')=1",
            (language,),
        )
        return operations, failures

    def require_clear(self, c, nid, language, *, inspect=False):
        operations, failures = self.blocked(c, nid, language)
        if (
            operations
            or (failures and not inspect)
            or any(not v["stopConfirmed"] for v in failures)
        ):
            fail("blocked", 409)

    def intent(
        self,
        c,
        nid,
        source,
        *,
        stage=None,
        metadata=None,
        retry=None,
        automatic=False,
        failure_reason=None,
    ):
        """Called inside the source terminal transaction for automatic stages."""
        group, registration = self.registration(c, nid, source)
        refs = [
            entity_ref(
                self.r.analysis.store.get(c, nid, "object", v["objectId"]), "object"
            )
            for v in group
        ]
        operation_id, checkpoint_id, now = (
            uuid.uuid4().hex,
            uuid.uuid4().hex,
            time.time(),
        )
        sequence = self.mutation(c, nid, source["language"])
        limits = self.r.checkpoints.limits
        reserved = limits["maxCheckpointBytes"] + limits["maxStagingBytes"]
        outstanding = (
            self.r.checkpoints.usage(c)["reservedBytes"]
            if hasattr(self.r.checkpoints, "usage")
            else c.execute(
                "SELECT COALESCE(SUM(reserved_bytes),0) FROM notebook_checkpoint_intents WHERE released=0"
            ).fetchone()[0]
        )
        reason = failure_reason
        if (
            not group
            or len(group) > limits["maxObjects"]
            or len({v["symbol"] for v in group}) != len(group)
        ):
            reason = "invalid_registered_group"
        elif (
            self.r.analysis.files.disk_usage() + outstanding + reserved
            > limits["maxWorkspaceBytes"]
        ):
            reason = "checkpoint_byte_limit"
        if reason and not automatic:
            fail(
                "limit" if reason == "checkpoint_byte_limit" else "invalid_reference",
                413 if reason == "checkpoint_byte_limit" else 422,
            )
        source_execution = c.execute(
            "SELECT execution_id FROM notebook_checkpoint_mutations WHERE notebook_id=? AND language=?",
            (nid, source["language"]),
        ).fetchone()
        details = {
            "sourceExecutionId": stage["executionId"]
            if stage
            else source_execution[0]
            if source_execution
            else None,
            "group": group,
            "objects": refs,
            "metadata": {} if metadata is None else metadata,
            "componentSourceDigest": self.r.source_digest,
            "environmentEvidence": dict(current_kernel(c, nid, source["language"])),
        }
        c.execute(
            "INSERT INTO notebook_checkpoint_intents VALUES(?,?,?,?,0)",
            (operation_id, nid, encoded(details).decode(), 0 if reason else reserved),
        )
        operation = {
            "operationId": operation_id,
            "checkpointId": checkpoint_id,
            "executionId": None,
            "kind": "retry" if retry else "save",
            "state": "intent",
            "statusSeq": 1,
            "source": source,
            "capturedMutationSeq": sequence,
            "registrationDigest": registration,
            "bindingsDigest": None,
            "stage": stage,
            "failureRef": None,
            "retryOf": retry,
            "stopConfirmed": True,
            "deadlineExceeded": False,
            "createdAt": now,
            "finishedAt": None,
        }
        coverage = {
            "coverage": "registered-only",
            "source": source,
            "mutationSeq": sequence,
            "objectCount": len(group),
            "registrationDigest": registration,
            "bindingsDigest": None,
            "unregisteredState": "unknown",
        }
        checkpoint = {
            "checkpointId": checkpoint_id,
            "manifestRevision": 0,
            "manifestSha256": None,
            "state": "pending",
            "coverage": coverage,
            "stage": stage,
            "policyRevision": 1,
            "operationId": operation_id,
            "totalBytes": 0,
            "createdAt": now,
            "pinRevision": 0,
            "pinned": False,
            "failureRef": None,
        }
        self.put(c, nid, "operation", operation)
        self.put(c, nid, "checkpoint", checkpoint)
        self.r.analysis.store.advance(c, nid)
        if reason:
            self.failed(c, nid, operation_id, reason, stopped=True)
        return checkpoint_id, operation_id

    @staticmethod
    def details(c, operation_id):
        row = c.execute(
            "SELECT document FROM notebook_checkpoint_intents WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            fail("not_found", 404)
        return json.loads(row[0])

    def transition(self, c, nid, operation_id, state, *, expected, **changes):
        item = self.get(c, nid, "operation", operation_id)
        if item["state"] not in expected:
            fail("reference_stale", 409)
        item.update(changes, state=state, statusSeq=item["statusSeq"] + 1)
        self.put(c, nid, "operation", item)
        self.r.analysis.store.advance(c, nid)
        return item

    def failed(self, c, nid, operation_id, reason, *, stopped, deadline=False):
        op = self.get(c, nid, "operation", operation_id)
        if op["state"] in {"ready", "failed"}:
            return op["failureRef"]
        fid = uuid.uuid4().hex
        failure = {
            "failureId": fid,
            "failureRevision": 1,
            "checkpointId": op["checkpointId"],
            "operationId": operation_id,
            "source": op["source"],
            "capturedMutationSeq": op["capturedMutationSeq"],
            "registrationDigest": op["registrationDigest"],
            "bindingsDigest": op["bindingsDigest"],
            "objects": self.details(c, operation_id)["objects"],
            "reasonCode": reason,
            "stopConfirmed": stopped,
            "blocking": True,
            "continuedAt": None,
            "recoveryGapId": None,
            "resolvedByCheckpoint": None,
            "supersededByFailure": None,
            "rootFailureId": fid,
        }
        if op["retryOf"]:
            old = self.get(c, nid, "failure", op["retryOf"]["failureId"])
            failure["rootFailureId"] = old["rootFailureId"]
            old.update(
                blocking=False,
                supersededByFailure=reference(failure, "failure"),
                failureRevision=old["failureRevision"] + 1,
            )
            self.put(c, nid, "failure", old)
        self.put(c, nid, "failure", failure)
        ref = reference(failure, "failure")
        self.transition(
            c,
            nid,
            operation_id,
            "failed",
            expected=ACTIVE | {"unknown"},
            failureRef=ref,
            stopConfirmed=stopped,
            deadlineExceeded=deadline or op["deadlineExceeded"],
            finishedAt=time.time(),
        )
        checkpoint = self.get(c, nid, "checkpoint", op["checkpointId"])
        checkpoint.update(state="failed" if stopped else "unknown", failureRef=ref)
        self.put(c, nid, "checkpoint", checkpoint)
        if stopped:
            c.execute(
                "UPDATE notebook_checkpoint_intents SET released=1 WHERE operation_id=?",
                (operation_id,),
            )
        return ref

    def decision(self, c, nid, ref):
        failure = self.get(c, nid, "failure", ref["failureId"])
        if reference(failure, "failure") != ref or not failure["blocking"]:
            fail("reference_stale", 409)
        source = failure["source"]
        kernel = current_kernel(c, nid, source["language"])
        if (
            kernel is None
            or kernel_ref(kernel) != source
            or self.mutation(c, nid, source["language"])
            != failure["capturedMutationSeq"]
        ):
            fail("reference_stale", 409)
        _, registration = self.registration(c, nid, source)
        if registration != failure["registrationDigest"]:
            fail("reference_stale", 409)
        if not failure["stopConfirmed"] or self.blocked(c, nid, source["language"])[0]:
            fail("stop_unconfirmed", 409)
        return failure

    def continued(self, c, nid, ref):
        item = self.decision(c, nid, ref)
        item.update(
            blocking=False,
            continuedAt=time.time(),
            recoveryGapId=uuid.uuid4().hex,
            failureRevision=item["failureRevision"] + 1,
        )
        self.put(c, nid, "failure", item)
        self.r.analysis.store.advance(c, nid)
        return item

    def published(self, c, nid, operation_id, manifest):
        op = self.get(c, nid, "operation", operation_id)
        if op["state"] != "publishing" or op["bindingsDigest"] is None:
            fail("reference_stale", 409)
        checkpoint = self.get(c, nid, "checkpoint", op["checkpointId"])
        checkpoint.update(
            state="ready",
            manifestRevision=1,
            manifestSha256=digest(manifest),
            totalBytes=sum(v["artifact"]["bytes"] for v in manifest["bindings"]),
        )
        checkpoint["coverage"]["bindingsDigest"] = op["bindingsDigest"]
        details = self.details(c, operation_id)
        details["manifest"] = manifest
        c.execute(
            "UPDATE notebook_checkpoint_intents SET document=?,released=1 WHERE operation_id=?",
            (encoded(details).decode(), operation_id),
        )
        self.put(c, nid, "checkpoint", checkpoint)
        self.transition(
            c,
            nid,
            operation_id,
            "ready",
            expected={"publishing"},
            finishedAt=time.time(),
            stopConfirmed=True,
        )
        if op["retryOf"]:
            old = self.get(c, nid, "failure", op["retryOf"]["failureId"])
            old.update(
                blocking=False,
                resolvedByCheckpoint=reference(checkpoint, "checkpoint"),
                failureRevision=old["failureRevision"] + 1,
            )
            self.put(c, nid, "failure", old)
        return checkpoint

    def recover(self, c):
        rows = c.execute(
            "SELECT DISTINCT notebook_id FROM notebook_checkpoint_intents WHERE released=0"
        ).fetchall()
        for row in rows:
            nid = row[0]
            for op in self.latest(
                c,
                nid,
                "operation",
                "json_extract(document,'$.state') NOT IN ('ready','failed')",
            ):
                if op["state"] in ACTIVE - {"intent", "publishing"}:
                    self.transition(
                        c,
                        nid,
                        op["operationId"],
                        "unknown",
                        expected=ACTIVE,
                        stopConfirmed=False,
                    )
                    item = self.get(c, nid, "checkpoint", op["checkpointId"])
                    item["state"] = "unknown"
                    self.put(c, nid, "checkpoint", item)
