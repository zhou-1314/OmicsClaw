"""Explicit whole-group restoration into a fresh, durably gated kernel."""

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import time
import uuid

from .analysis_store import entity_ref, kernel_ref
from .checkpoint_store import digest, fail, reference
from .checkpoints import SCIENCE, SPEC_KEYS
from .contracts import encoded
from .security import private_directory
from .store import current_kernel, require_kernel

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_restore_intents (
 operation_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL,
 instance_id TEXT UNIQUE NOT NULL, document TEXT NOT NULL,
 gate_active INTEGER NOT NULL DEFAULT 1, reserved_bytes INTEGER NOT NULL);
"""
TERMINAL = {"restored", "failed", "unknown"}
POLICY = {
    "id": "omicsclaw-checkpoint-reader-v1",
    "runtime": "same-major-minor",
    "dependencies": "exact-loaded-critical-versions",
    "group": "complete",
}


class OwnerRestores:
    def __init__(self, runtime):
        self.r, self.cp = runtime, runtime.checkpoints
        self.s = self.cp.store
        runtime.db.run(lambda c: c.executescript(SCHEMA))
        self.workers = {}
        self.directory = private_directory(
            runtime.analysis.files.directory / "restores"
        )

    def contains(self, c, oid):
        return (
            c.execute(
                "SELECT 1 FROM notebook_restore_intents WHERE operation_id=?", (oid,)
            ).fetchone()
            is not None
        )

    def get(self, c, nid, oid):
        return self.s.get(c, nid, "restore", oid)

    def details(self, c, oid):
        row = c.execute(
            "SELECT document FROM notebook_restore_intents WHERE operation_id=?", (oid,)
        ).fetchone()
        if row is None:
            fail("not_found", 404)
        return json.loads(row[0])

    def put(self, c, nid, op, **changes):
        op = {**op, **changes, "statusSeq": op["statusSeq"] + 1}
        self.s.put(c, nid, "restore", op)
        self.r.analysis.store.advance(c, nid)
        return op

    def active(self, c, nid, language):
        intents = c.execute(
            "SELECT i.operation_id FROM notebook_restore_intents i "
            "JOIN notebook_runtime_kernels k ON k.instance_id=i.instance_id "
            "WHERE i.notebook_id=? AND k.language=? AND i.gate_active=1 LIMIT 2",
            (nid, language),
        ).fetchall()
        if len(intents) > 1:
            fail("blocked", 409)
        return self.get(c, nid, intents[0][0]) if intents else None

    def require_clear(self, c, nid, language):
        if self.active(c, nid, language):
            fail("blocked", 409)

    def eligible(self, c, nid, target):
        if self.active(c, nid, target["language"]):
            return False
        row = current_kernel(c, nid, target["language"])
        if row is None or kernel_ref(row) != target or row["state"] != "reserved":
            return False
        try:
            self.r.registry.require_ready(
                target["environmentId"], target["environmentRevision"]
            )
            if self.r.environment_guard:
                guard = self.r.environment_guard
                guard.coordinator.check(
                    guard.key("kernel", target["kernelInstanceId"]), guard.owner
                )
        except ValueError:
            return False
        instance = row["instance_id"]
        return not (
            c.execute(
                "SELECT 1 FROM notebook_runtime_executions WHERE instance_id=? LIMIT 1",
                (instance,),
            ).fetchone()
            or c.execute(
                "SELECT 1 FROM notebook_restore_intents WHERE instance_id=?",
                (instance,),
            ).fetchone()
            or c.execute(
                "SELECT 1 FROM notebook_analysis_entities WHERE notebook_id=? AND kind='object' AND json_extract(document,'$.kernel.kernelInstanceId')=? LIMIT 1",
                (nid, instance),
            ).fetchone()
        )

    @staticmethod
    def environment(identity, evidence):
        if "environment_json" in evidence:
            evidence = json.loads(evidence["environment_json"])
        return {
            "environmentId": identity["environmentId"],
            "environmentRevision": identity["environmentRevision"],
            "fingerprint": evidence.get("fingerprint"),
            "evidenceDigest": digest(evidence) if evidence else None,
            "runtimeVersion": evidence.get("observation", {}).get("version"),
        }

    def source_hash(self, language):
        extension = ".py" if language == "python" else ".R"
        return digest(
            {
                name: hashlib.sha256(
                    (SCIENCE / (name + extension)).read_bytes()
                ).hexdigest()
                for name in (
                    "checkpoint_restore",
                    "notebook_checkpoint",
                    "checkpoint_science",
                    "notebook_inspect",
                    "notebook_identity",
                )
            }
        )

    def intent(self, c, nid, args):
        target, ref = args["target"], args["checkpoint"]
        item = self.s.get(c, nid, "checkpoint", ref["checkpointId"])
        if item["state"] != "ready" or reference(item, "checkpoint") != ref:
            fail("reference_stale", 409)
        details = self.s.details(c, item["operationId"])
        manifest = details["manifest"]
        if (
            digest(manifest) != ref["manifestSha256"]
            or manifest["source"]["language"] != target["language"]
        ):
            fail("invalid_reference")
        if not self.eligible(c, nid, target):
            fail("blocked", 409)
        self.r.idle(c, nid)
        for language in ("python", "r"):
            self.cp.store.require_clear(c, nid, language)
            self.require_clear(c, nid, language)
        row = current_kernel(c, nid, target["language"])
        if self.r.environment_guard:
            guard = self.r.environment_guard
            guard.coordinator.check(
                guard.key("kernel", target["kernelInstanceId"]), guard.owner
            )
        count = len(manifest["bindings"])
        if not 1 <= count <= 32 or len(details["group"]) != count:
            fail("invalid_reference")
        amount = self.cp.limits["maxStagingBytes"]
        usage = self.cp.usage(c)
        if (
            usage["workspaceBytes"] + usage["reservedBytes"] + amount
            > self.r.limits["diskBytes"]
        ):
            fail("limit", 413)
        oid = uuid.uuid4().hex
        mapping = [
            {
                "logicalKey": v["logicalKey"],
                "symbol": v["symbol"],
                "objectId": uuid.uuid4().hex,
            }
            for v in manifest["bindings"]
        ]
        if len({v["symbol"] for v in mapping}) != count:
            fail("invalid_reference")
        op = {
            "operationId": oid,
            "statusSeq": 1,
            "executionId": None,
            "checkpoint": ref,
            "target": target,
            "state": "intent",
            "gateActive": True,
            "targetReleased": False,
            "stopConfirmed": True,
            "compatibility": {
                "status": "pending",
                "policyId": POLICY["id"],
                "policyDigest": digest(POLICY),
                "reasonCode": "",
                "source": self.environment(
                    manifest["source"], manifest["environmentEvidence"]
                ),
                "target": self.environment(target, json.loads(row["environment_json"])),
            },
            "readerSourceSha256": self.source_hash(target["language"]),
            "mappingDigest": digest(mapping),
            "resultObjectRefs": [],
            "reasonCode": "",
            "createdAt": time.time(),
            "finishedAt": None,
        }
        fixed = {
            "checkpointOperationId": item["operationId"],
            "mapping": mapping,
            "manifest": manifest,
            "group": details["group"],
            "files": details["files"],
            "componentSourceDigest": self.r.source_digest,
            "targetMutationSeq": self.s.mutation(c, nid, target["language"]),
        }
        c.execute(
            "INSERT INTO notebook_restore_intents VALUES(?,?,?,?,1,?)",
            (oid, nid, target["kernelInstanceId"], encoded(fixed).decode(), amount),
        )
        c.execute(
            "INSERT INTO notebook_checkpoint_holds(checkpoint_id,hold_id,kind,reference_id,created_at) VALUES(?,?,?,?,?)",
            (ref["checkpointId"], "restore-" + oid, "restore", oid, time.time()),
        )
        self.s.put(c, nid, "restore", op)
        self.r.analysis.store.advance(c, nid)
        return oid

    def check(self, c, nid, op):
        if not op["gateActive"] or op["state"] in TERMINAL:
            fail("blocked", 409)
        target = op["target"]
        require_kernel(
            c, nid, target["kernelInstanceId"], target["generation"], target["language"]
        )
        if (
            self.s.mutation(c, nid, target["language"])
            != self.details(c, op["operationId"])["targetMutationSeq"]
        ):
            fail("reference_stale", 409)
        if op["readerSourceSha256"] != self.source_hash(target["language"]) or op[
            "compatibility"
        ]["policyDigest"] != digest(POLICY):
            fail("policy_changed", 409)
        self.cp.store.require_clear(c, nid, target["language"])

    def claim(self, c, nid, eid, oid, phase):
        op = self.get(c, nid, oid)
        self.check(c, nid, op)
        expected = {"loading": "validating_source", "committing": "validating_target"}
        if expected.get(phase) != op["state"]:
            fail("reference_stale", 409)
        self.put(c, nid, op, state=phase, executionId=eid, stopConfirmed=False)
        c.execute(
            "INSERT INTO notebook_checkpoint_executions VALUES(?,?,?)",
            (eid, oid, phase),
        )

    def timeout(self, c, nid, oid):
        op = self.get(c, nid, oid)
        if op["gateActive"] and op["state"] not in TERMINAL:
            self.put(
                c,
                nid,
                op,
                state="stopping_unconfirmed",
                stopConfirmed=False,
                reasonCode="notebook_checkpoint_stop_unconfirmed",
            )

    def terminal(self, c, nid, oid):
        op = self.get(c, nid, oid)
        if op["gateActive"] and op["state"] not in TERMINAL:
            self.put(c, nid, op, stopConfirmed=True)

    def recover(self, c):
        for nid, oid in c.execute(
            "SELECT notebook_id,operation_id FROM notebook_restore_intents WHERE gate_active=1"
        ).fetchall():
            op = self.get(c, nid, oid)
            if op["state"] not in TERMINAL:
                self.put(
                    c,
                    nid,
                    op,
                    state="unknown",
                    stopConfirmed=False,
                    reasonCode="notebook_outcome_unknown",
                    finishedAt=time.time(),
                )

    def schedule(self):
        rows = self.r.db.run(
            lambda c: c.execute(
                "SELECT i.notebook_id,i.operation_id FROM notebook_restore_intents i JOIN notebook_checkpoint_records r ON r.entity_id=i.operation_id AND r.notebook_id=i.notebook_id AND r.kind='restore' WHERE i.gate_active=1 AND r.revision=(SELECT MAX(x.revision) FROM notebook_checkpoint_records x WHERE x.notebook_id=r.notebook_id AND x.kind=r.kind AND x.entity_id=r.entity_id) AND json_extract(r.document,'$.state')='intent' LIMIT 33"
            ).fetchall()
        )
        if len(rows) > 32:
            fail("limit", 413)
        for nid, oid in rows:
            if oid not in self.workers:
                task = self.r._own(self.run(nid, oid))
                self.workers[oid] = task
                task.add_done_callback(
                    lambda done, key=oid: self.workers.pop(key, None)
                )

    def update(self, nid, oid, **changes):
        def work(c):
            self.r.fence(c)
            op = self.get(c, nid, oid)
            if not op["gateActive"] or op["state"] in TERMINAL:
                return op
            return self.put(c, nid, op, **changes)

        return self.r.service._transaction(work)

    def validate_source(self, nid, oid):
        op, fixed = self.r.db.run(
            lambda c: (self.get(c, nid, oid), self.details(c, oid))
        )
        manifest = fixed["manifest"]
        if (
            digest(manifest) != op["checkpoint"]["manifestSha256"]
            or "runtimeEvidence" not in manifest
        ):
            fail("invalid_reference")
        self.runtime_evidence(manifest["runtimeEvidence"], op["target"]["language"])
        files = []
        for binding, item in zip(manifest["bindings"], fixed["files"], strict=True):
            suffix = ".py" if op["target"]["language"] == "python" else ".R"
            if (
                binding["serializerVersion"] != 1
                or binding["serializerSourceSha256"]
                != hashlib.sha256(
                    (SCIENCE / ("notebook_checkpoint" + suffix)).read_bytes()
                ).hexdigest()
            ):
                fail("unsupported_serializer")
            artifact = self.r.artifacts.get_artifact(binding["artifact"]["artifactId"])
            path = (
                self.cp.directory
                / fixed["checkpointOperationId"]
                / "data"
                / item["file"]
            )
            if (
                artifact is None
                or artifact.path != str(path)
                or artifact.produced_by != "notebook_analysis"
                or artifact.meta
                != {
                    "notebookId": nid,
                    "checkpointId": op["checkpoint"]["checkpointId"],
                    "operationId": fixed["checkpointOperationId"],
                }
            ):
                fail("invalid_reference")
            measured = self.cp.hash_file(path)
            if (
                any(measured[k] != item[k] for k in ("sha256", "bytes"))
                or artifact.sha256 != item["sha256"]
                or artifact.size != item["bytes"]
                or binding["artifact"]
                != {
                    "artifactId": artifact.id,
                    "sha256": artifact.sha256,
                    "bytes": artifact.size,
                }
            ):
                fail("reference_stale", 409)
            files.append({**item, "path": str(path)})
        return {
            "token": "__omicsclaw_restore_" + oid,
            "runtimeEvidence": manifest["runtimeEvidence"],
            "files": files,
            "maxStagingBytes": self.cp.limits["maxStagingBytes"],
            "inspection": [
                {
                    "logicalKey": v["logicalKey"],
                    "request": {
                        "schemaVersion": 1,
                        **{k: v["spec"][k] for k in SPEC_KEYS},
                    },
                }
                for v in fixed["group"]
            ],
        }

    @staticmethod
    def runtime_evidence(evidence, language):
        names = (
            {"anndata", "pandas", "numpy", "scipy", "h5py"}
            if language == "python"
            else {
                "DESeq2",
                "SummarizedExperiment",
                "S4Vectors",
                "IRanges",
                "GenomicRanges",
                "GenomeInfoDb",
                "BiocGenerics",
                "Biobase",
                "Matrix",
                "jsonlite",
            }
        )
        if (
            not isinstance(evidence, dict)
            or set(evidence)
            != {
                "schemaVersion",
                "language",
                "runtimeVersion",
                "executable",
                "prefix",
                "libraryPaths",
                "packages",
            }
            or type(evidence["schemaVersion"]) is not int
            or evidence["schemaVersion"] != 1
            or evidence["language"] != language
            or not isinstance(evidence["packages"], dict)
            or set(evidence["packages"]) != names
            or len(encoded(evidence)) > 32768
            or not isinstance(evidence["runtimeVersion"], str)
            or len(evidence["runtimeVersion"].encode()) > 128
            or re.search(r"\d+\.\d+", evidence["runtimeVersion"]) is None
        ):
            fail("invalid_reference")
        for value in evidence["packages"].values():
            if value is not None and (
                not isinstance(value, dict)
                or set(value) != {"version", "path"}
                or not all(isinstance(value[k], str) and value[k] for k in value)
            ):
                fail("invalid_reference")
        return evidence

    def record_environment(self, nid, oid, directory):
        op = self.r.db.run(lambda c: self.get(c, nid, oid))
        target = self.runtime_evidence(
            self.r.analysis.files.read_json(directory / "runtime.json", 32768),
            op["target"]["language"],
        )

        def write(c):
            self.r.fence(c)
            fixed = self.details(c, oid)
            fixed["targetRuntimeEvidence"] = target
            c.execute(
                "UPDATE notebook_restore_intents SET document=? WHERE operation_id=?",
                (encoded(fixed).decode(), oid),
            )

        self.r.service._transaction(write)
        return target

    def code(self, op, request, directory, *, commit=False):
        language = op["target"]["language"]
        args = [request["token"] if commit else request, str(directory)]
        if language == "python":
            return (
                "(lambda ns: __import__('runpy').run_path("
                + repr(str(SCIENCE / "checkpoint_restore.py"))
                + ")["
                + repr("commit" if commit else "prepare")
                + "](ns,"
                + ",".join(repr(x) for x in args)
                + "))(globals())"
            )
        quote = lambda value: json.dumps(str(value), ensure_ascii=False)
        first = (
            quote(args[0])
            if commit
            else "jsonlite::fromJSON("
            + quote(json.dumps(request, ensure_ascii=False))
            + ",simplifyVector=FALSE)"
        )
        return (
            "local({fixed<-new.env(parent=baseenv());sys.source("
            + quote(SCIENCE / "checkpoint_restore.R")
            + ",envir=fixed);fixed$restore_"
            + ("commit" if commit else "prepare")
            + "(.GlobalEnv,"
            + first
            + ","
            + quote(directory)
            + ","
            + quote(SCIENCE)
            + ")})"
        )

    def import_group(self, c, nid, oid, eid, directory, *, publish=False):
        op, fixed = self.get(c, nid, oid), self.details(c, oid)
        self.check(c, nid, op)
        refs = []
        c.execute("SAVEPOINT restore_inspection")
        try:
            for group, binding, mapping in zip(
                fixed["group"],
                fixed["manifest"]["bindings"],
                fixed["mapping"],
                strict=True,
            ):
                spec = {**group["spec"], "kernel": op["target"]}
                self.r.analysis.import_inspection(
                    c,
                    {
                        "notebook_id": nid,
                        "object_id": mapping["objectId"],
                        "spec_json": encoded(spec).decode(),
                        "directory": str(directory / mapping["logicalKey"]),
                        "execution_id": eid,
                        "dataset": binding["dataset"],
                    },
                )
                obj = self.r.analysis.store.get(c, nid, "object", mapping["objectId"])
                if (
                    obj["availability"] != "verified"
                    or obj["axes"] != binding["axes"]
                    or obj["matrix"] != binding["matrix"]
                    or digest([obj["type"], obj["adapter"], 1])
                    != binding["typeSchemaDigest"]
                ):
                    fail("invalid_reference")
                refs.append(entity_ref(obj, "object"))
            if not publish:
                c.execute("ROLLBACK TO restore_inspection")
        finally:
            c.execute("RELEASE restore_inspection")
        return refs

    async def run(self, nid, oid):
        try:
            op = self.update(nid, oid, state="validating_source")
            request = await asyncio.to_thread(self.validate_source, nid, oid)
            directory = private_directory(self.directory / oid)
            result = await self.cp.phase(
                nid, oid, "loading", self.code(op, request, directory), restore=True
            )
            if result.status != "completed":
                self.failed_execution(nid, oid, result)
                return
            self.update(nid, oid, state="validating_target", stopConfirmed=True)
            await asyncio.to_thread(
                self.r.service._transaction,
                lambda c: self.import_group(
                    c, nid, oid, result.execution_id, directory
                ),
            )
            target_evidence = self.record_environment(nid, oid, directory)
            op = self.r.db.run(lambda c: self.get(c, nid, oid))
            result = await self.cp.phase(
                nid,
                oid,
                "committing",
                self.code(op, request, directory, commit=True),
                restore=True,
            )
            if result.status != "completed":
                self.failed_execution(nid, oid, result)
                return
            if self.r.analysis.files.read_json(directory / "committed.json", 4096) != {
                "token": request["token"]
            }:
                fail("invalid_reference")

            def published(c):
                self.r.fence(c)
                refs = self.import_group(
                    c, nid, oid, result.execution_id, directory, publish=True
                )
                op = self.get(c, nid, oid)
                compatibility = {
                    **op["compatibility"],
                    "status": "verified",
                    "reasonCode": "",
                    "target": {
                        **op["compatibility"]["target"],
                        "runtimeVersion": target_evidence["runtimeVersion"],
                        "evidenceDigest": digest(target_evidence),
                    },
                }
                c.execute(
                    "UPDATE notebook_restore_intents SET gate_active=0,reserved_bytes=0 WHERE operation_id=?",
                    (oid,),
                )
                c.execute(
                    "DELETE FROM notebook_checkpoint_holds WHERE hold_id=?",
                    ("restore-" + oid,),
                )
                self.s.mutation(
                    c,
                    nid,
                    op["target"]["language"],
                    advance=True,
                    execution_id=result.execution_id,
                )
                self.put(
                    c,
                    nid,
                    op,
                    state="restored",
                    gateActive=False,
                    stopConfirmed=True,
                    resultObjectRefs=refs,
                    compatibility=compatibility,
                    finishedAt=time.time(),
                )

            self.r.service._transaction(published)
        except sqlite3.Error:
            self.r.durability_failed = True
            raise
        except asyncio.CancelledError:
            self.r.service._transaction(self.recover)
            raise
        except Exception as error:  # noqa: BLE001 - accepted targets remain gated
            op = self.r.db.run(lambda c: self.get(c, nid, oid))
            self.update(
                nid,
                oid,
                state="failed",
                reasonCode=getattr(
                    error, "code", "notebook_checkpoint_invalid_reference"
                ),
                compatibility={
                    **op["compatibility"],
                    "status": "unverified",
                    "reasonCode": "notebook_checkpoint_invalid_reference",
                },
                finishedAt=time.time(),
            )
        finally:
            await asyncio.to_thread(self.cleanup, nid, oid)
            self.r.wake()

    def failed_execution(self, nid, oid, result):
        usage = (result.output or {}).get("usage", {})
        stopped = bool(
            usage.get(
                "stop_confirmed",
                result.status in {"failed", "interrupted"} and bool(usage),
            )
        )
        op = self.r.db.run(lambda c: self.get(c, nid, oid))
        error = (result.output or {}).get("error") or {}
        reason = error.get("evalue", "").strip()
        incompatible = reason in {
            "restore_runtime_incompatible",
            "restore_dependency_incompatible",
        }
        compatibility = {
            **op["compatibility"],
            "status": "incompatible" if incompatible else "unverified",
            "reasonCode": reason
            if incompatible
            else "notebook_checkpoint_invalid_reference",
        }
        if stopped and incompatible:
            target = self.record_environment(nid, oid, self.directory / oid)
            compatibility["target"] = {
                **compatibility["target"],
                "runtimeVersion": target["runtimeVersion"],
                "evidenceDigest": digest(target),
            }
        self.update(
            nid,
            oid,
            state="failed" if stopped else "unknown",
            stopConfirmed=stopped,
            reasonCode="notebook_checkpoint_invalid_reference"
            if stopped
            else "notebook_checkpoint_stop_unconfirmed",
            compatibility=compatibility,
            finishedAt=time.time(),
        )

    def cleanup(self, nid, oid):
        op = self.r.db.run(lambda c: self.get(c, nid, oid))
        if not op["stopConfirmed"] or op["state"] not in TERMINAL:
            return
        directory = self.directory / oid
        if directory.is_symlink():
            fail("unavailable", 503)
        if directory.exists():
            try:
                shutil.rmtree(directory)
            except OSError:
                pass  # Remaining files stay charged to the physical budget.

    def release_allowed(self, c, nid, language):
        op = self.active(c, nid, language)
        row = current_kernel(c, nid, language)
        if op and (row is None or kernel_ref(row) != op["target"]):
            fail("reference_stale", 409)
        if op and op["state"] not in {"failed", "unknown", "stopping_unconfirmed"}:
            fail("blocked", 409)
        return op is not None

    def released(self, c, nid, language):
        op = self.active(c, nid, language)
        if op is None:
            return
        self.release_allowed(c, nid, language)
        if self.r.environment_guard is not None:
            guard = self.r.environment_guard
            reservation = guard.coordinator.get(
                guard.key("kernel", op["target"]["kernelInstanceId"])
            )
            if (
                reservation is None
                or reservation["state"] != "closed"
                or reservation["uncertain_exit"]
            ):
                fail("stop_unconfirmed", 409)
        c.execute(
            "UPDATE notebook_restore_intents SET gate_active=0,reserved_bytes=0 WHERE operation_id=?",
            (op["operationId"],),
        )
        c.execute(
            "DELETE FROM notebook_checkpoint_holds WHERE hold_id=?",
            ("restore-" + op["operationId"],),
        )
        self.put(
            c,
            nid,
            op,
            state=op["state"] if op["state"] in TERMINAL else "unknown",
            gateActive=False,
            targetReleased=True,
            stopConfirmed=True,
            finishedAt=op["finishedAt"] or time.time(),
        )
        return op["operationId"]

    def overview(self, c, nid):
        languages = {}
        for language in ("python", "r"):
            row = current_kernel(c, nid, language)
            kernel = (
                kernel_ref(row)
                if row is not None and row["state"] not in {"released", "lost"}
                else None
            )
            op = self.active(c, nid, language)
            clear = op is None
            languages[language] = {
                "kernel": kernel,
                "eligible": bool(kernel and self.eligible(c, nid, kernel)),
                "activeOperationId": op["operationId"] if op else None,
                "canRun": clear,
                "canInspect": clear,
                "canRelease": clear
                or op["state"] in {"failed", "unknown", "stopping_unconfirmed"},
                "reasonCode": "" if clear else "notebook_checkpoint_blocked",
            }
        ready = all(v["canRun"] for v in languages.values())
        return {
            "languages": languages,
            "continuationReady": ready,
            "reasonCode": "" if ready else "notebook_checkpoint_blocked",
        }

    async def close(self):
        tasks = list(self.workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
