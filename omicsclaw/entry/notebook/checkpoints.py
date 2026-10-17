"""Owner-owned checkpoint work uses the existing kernel execution queue."""

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from .analysis_store import entity_ref, kernel_ref
from .checkpoint_store import SCHEMA, CheckpointStore, fail
from .contracts import encoded
from .security import private_directory
from .service import TERMINAL
from .store import current_kernel, require_kernel

SCIENCE = Path(__file__).resolve().parents[2] / "skillenv"
SPEC_KEYS = (
    "symbol",
    "adapter",
    "rowIdentity",
    "columnIdentity",
    "tableAxis",
    "declaredMatrix",
)


class OwnerCheckpoints:
    def __init__(self, runtime):
        self.r = runtime
        runtime.db.run(lambda c: c.executescript(SCHEMA))
        self.store = CheckpointStore(runtime)
        from .checkpoint_wire import CheckpointWire

        self.wire = CheckpointWire(self)
        self.directory = private_directory(
            runtime.analysis.files.directory / "checkpoints"
        )
        self.workers, self.waiters = {}, {}
        disk = runtime.limits["diskBytes"]
        self.limits = {
            "maxObjects": runtime.limits["checkpointObjects"],
            "maxFileBytes": min(runtime.limits["checkpointFileBytes"], disk // 2),
            "maxCheckpointBytes": min(runtime.limits["checkpointBytes"], disk // 2),
            "maxWorkspaceBytes": disk,
            "maxStagingBytes": min(runtime.limits["checkpointStagingBytes"], disk // 4),
            "saveTimeoutSeconds": runtime.limits["checkpointTimeoutSeconds"],
            "scienceRssBytes": runtime.limits["scienceRssBytes"],
            "deadlineMode": "interrupt_without_kernel_kill",
        }

    def execution(self, c, eid):
        row = c.execute(
            "SELECT operation_id,phase FROM notebook_checkpoint_executions WHERE execution_id=?",
            (eid,),
        ).fetchone()
        return tuple(row) if row else None

    def allowed_dispatch(self, c, nid, eid):
        if self.execution(c, eid):
            return True
        row = c.execute(
            "SELECT k.language FROM notebook_runtime_executions x JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE execution_id=?",
            (eid,),
        ).fetchone()
        operations, failures = self.store.blocked(c, nid, row[0])
        if not operations and not failures:
            return True
        inspection = c.execute(
            "SELECT command_json FROM notebook_analysis_operations WHERE execution_id=?",
            (eid,),
        ).fetchone()
        return bool(
            not operations
            and inspection
            and json.loads(inspection[0])["action"] == "object.inspect"
            and all(item["stopConfirmed"] for item in failures)
        )

    def claim(self, c, nid, eid, private):
        oid, phase = private
        if self.r.restores.contains(c, oid):
            return self.r.restores.claim(c, nid, eid, oid, phase)
        op = self.store.get(c, nid, "operation", oid)
        require_kernel(
            c,
            nid,
            op["source"]["kernelInstanceId"],
            op["source"]["generation"],
            op["source"]["language"],
        )
        self.check_capture(c, nid, op)
        expected = {
            "inspecting": {"intent"},
            "saving": {"inspecting"},
            "validating": {"saving"},
        }[phase]
        self.store.transition(
            c, nid, oid, phase, expected=expected, executionId=eid, stopConfirmed=False
        )
        c.execute(
            "INSERT INTO notebook_checkpoint_executions VALUES(?,?,?)",
            (eid, oid, phase),
        )

    def check_private(self, c, nid, oid):
        if self.r.restores.contains(c, oid):
            return self.r.restores.check(c, nid, self.r.restores.get(c, nid, oid))
        return self.check_capture(c, nid, self.store.get(c, nid, "operation", oid))

    def check_capture(self, c, nid, op):
        source = op["source"]
        row = current_kernel(c, nid, source["language"])
        if (
            row is None
            or kernel_ref(row) != source
            or self.store.mutation(c, nid, source["language"])
            != op["capturedMutationSeq"]
        ):
            fail("reference_stale", 409)
        if self.store.registration(c, nid, source)[1] != op["registrationDigest"]:
            fail("reference_stale", 409)

    def timeout(self, nid, eid):
        def work(c):
            self.r.fence(c)
            private = self.execution(c, eid)
            if private:
                if self.r.restores.contains(c, private[0]):
                    return self.r.restores.timeout(c, nid, private[0])
                op = self.store.get(c, nid, "operation", private[0])
                if op["state"] in {"inspecting", "saving", "validating"}:
                    self.store.transition(
                        c,
                        nid,
                        private[0],
                        "stopping_unconfirmed",
                        expected={op["state"]},
                        deadlineExceeded=True,
                        stopConfirmed=False,
                    )

        self.r.service._transaction(work)

    def execution_options(self, nid, eid):
        if self.r.db.run(lambda c: self.execution(c, eid)):
            return {"preserve_on_timeout": True}
        return {}

    def finished(self, eid):
        if eid in self.waiters:
            self.waiters[eid].set()

    def schedule(self):
        rows = self.r.db.run(
            lambda c: c.execute(
                "SELECT r.notebook_id,r.entity_id FROM notebook_checkpoint_records r WHERE kind='operation' AND revision=(SELECT MAX(x.revision) FROM notebook_checkpoint_records x WHERE x.notebook_id=r.notebook_id AND x.kind=r.kind AND x.entity_id=r.entity_id) AND json_extract(document,'$.state') IN ('intent','publishing') LIMIT 33"
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

    async def phase(self, nid, oid, phase, code, *, restore=False):
        op = self.r.db.run(
            lambda c: self.store.get(c, nid, "restore" if restore else "operation", oid)
        )
        cid = hashlib.sha256(encoded([oid, phase])).hexdigest()
        eid = hashlib.sha256(json.dumps([nid, cid]).encode()).hexdigest()
        event = self.waiters.setdefault(eid, asyncio.Event())
        semantic = {
            "version": 1,
            "notebookId": nid,
            "cellId": cid,
            "commandId": cid,
            "source": code,
            "expectedRevision": 0,
            **op["target" if restore else "source"],
            "origin": "user",
            "approvalScope": "explicit_user",
            "timeoutSeconds": self.limits["saveTimeoutSeconds"],
            "preconditions": {},
        }
        await self.r._admit(
            {"command": semantic}, agent=False, checkpoint_operation=(oid, phase)
        )
        try:
            while True:
                record = await self.r.service.store.get_execution(eid)
                if record.status in TERMINAL and not (record.output or {}).get(
                    "event_stream_pending", True
                ):
                    return record
                await event.wait()
                event.clear()
        finally:
            self.waiters.pop(eid, None)

    def source(self, phase, op, details, directory):
        language = op["source"]["language"]
        objects = [
            {
                "logicalKey": v["logicalKey"],
                "symbol": v["symbol"],
                "adapter": v["spec"]["adapter"],
            }
            for v in details["group"]
        ]
        if phase == "inspecting":
            request = [
                {
                    "logicalKey": v["logicalKey"],
                    "request": {
                        "schemaVersion": 1,
                        **{key: v["spec"][key] for key in SPEC_KEYS},
                    },
                }
                for v in details["group"]
            ]
            name, function = "checkpoint_science", "inspect_group"
            args = [request, str(directory), self.limits["maxStagingBytes"]]
        elif phase == "saving":
            name, function = "notebook_checkpoint", "save"
            args = [
                {
                    "objects": objects,
                    "maxFileBytes": self.limits["maxFileBytes"],
                    "maxBytes": self.limits["maxCheckpointBytes"],
                    "maxMemoryBytes": self.limits["scienceRssBytes"],
                    "timeoutSeconds": self.limits["saveTimeoutSeconds"],
                },
                str(directory),
            ]
        else:
            name, function = "checkpoint_science", "verify_group"
            args = [details["files"], str(directory)]
        if language == "python":
            code = (
                "(lambda ns: __import__('runpy').run_path("
                + repr(str(SCIENCE / (name + ".py")))
                + ")["
                + repr(function)
                + "]("
                + ("ns, " if phase != "validating" else "")
                + ", ".join(repr(value) for value in args)
                + "))(globals())"
            )
            if phase == "saving":
                code += (
                    "\n__import__('runpy').run_path("
                    + repr(str(SCIENCE / "checkpoint_science.py"))
                    + ')["runtime_evidence"]('
                    + repr(str(directory))
                    + ")"
                )
            return code
        quote = lambda value: json.dumps(str(value), ensure_ascii=False)
        arguments = [".GlobalEnv"] if phase != "validating" else []
        arguments.extend(
            [
                "jsonlite::fromJSON("
                + quote(json.dumps(args[0], ensure_ascii=False))
                + ",simplifyVector=FALSE)",
                quote(directory),
            ]
        )
        if phase == "inspecting":
            arguments += [str(self.limits["maxStagingBytes"]), quote(SCIENCE)]
        elif phase == "saving":
            import sys

            arguments += [quote(sys.executable), quote(SCIENCE / "checkpoint_io.py")]
        else:
            arguments += [quote(SCIENCE)]
        r_function = {
            "inspecting": "checkpoint_inspect_group",
            "saving": "checkpoint_save",
            "validating": "checkpoint_verify_group",
        }[phase]
        code = (
            "local({fixed<-new.env(parent=baseenv());sys.source("
            + quote(SCIENCE / (name + ".R"))
            + ",envir=fixed);fixed$"
            + r_function
            + "("
            + ",".join(arguments)
            + ")})"
        )
        if phase == "saving":
            code += (
                "\nlocal({fixed<-new.env(parent=baseenv());sys.source("
                + quote(SCIENCE / "checkpoint_science.R")
                + ",envir=fixed);fixed$checkpoint_runtime_evidence("
                + quote(directory)
                + ")})"
            )
        return code

    async def run(self, nid, oid):
        op = self.r.db.run(lambda c: self.store.get(c, nid, "operation", oid))
        try:
            if op["state"] == "publishing":
                return await self.publish(nid, oid)
            directory = private_directory(self.directory / oid)
            for phase in ("inspecting", "saving", "validating"):
                op, details = self.r.db.run(
                    lambda c: (
                        self.store.get(c, nid, "operation", oid),
                        self.store.details(c, oid),
                    )
                )
                path = private_directory(
                    directory / ("inspection" if phase == "inspecting" else "data")
                )
                record = await self.phase(
                    nid, oid, phase, self.source(phase, op, details, path)
                )
                usage = (record.output or {}).get("usage", {})
                if record.status != "completed":
                    self.r.service._transaction(
                        lambda c, phase=phase, usage=usage, record=record: (
                            self.store.failed(
                                c,
                                nid,
                                oid,
                                "checkpoint_" + phase + "_failed",
                                stopped=bool(
                                    usage.get(
                                        "stop_confirmed",
                                        record.status
                                        in {"completed", "failed", "interrupted"}
                                        and bool(usage),
                                    )
                                ),
                                deadline=bool(usage.get("deadline_exceeded")),
                            )
                        )
                    )
                    return
                if phase == "inspecting":
                    await asyncio.to_thread(
                        self.import_group, nid, oid, record.execution_id, path
                    )
                elif phase == "saving":
                    await asyncio.to_thread(self.prepare_files, nid, oid, path)
            await self.publish(nid, oid)
        except sqlite3.Error:
            self.r.durability_failed = True
            raise
        except asyncio.CancelledError:
            # Owner shutdown is not proof that a worker thread or kernel stopped.
            self.r.service._transaction(lambda c: self.store.recover(c))
            raise
        except Exception as error:  # noqa: BLE001 - accepted failures retain a durable blocker
            op = self.r.db.run(lambda c: self.store.get(c, nid, "operation", oid))
            self.r.service._transaction(
                lambda c, error=error: self.store.failed(
                    c,
                    nid,
                    oid,
                    getattr(error, "code", "checkpoint_operation_failed"),
                    stopped=op["stopConfirmed"],
                )
            )
        finally:
            await asyncio.to_thread(self.cleanup_failed, oid)
            self.r.wake()

    async def close(self):
        tasks = list(self.workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def import_group(self, nid, oid, eid, directory):
        from .checkpoint_store import digest

        def work(c):
            self.r.fence(c)
            op, details = (
                self.store.get(c, nid, "operation", oid),
                self.store.details(c, oid),
            )
            self.check_capture(c, nid, op)
            bindings = []
            for item in details["group"]:
                self.r.analysis.import_inspection(
                    c,
                    {
                        "notebook_id": nid,
                        "object_id": item["objectId"],
                        "spec_json": encoded(item["spec"]).decode(),
                        "directory": str(directory / item["logicalKey"]),
                        "execution_id": eid,
                    },
                )
                obj = self.r.analysis.store.get(c, nid, "object", item["objectId"])
                if obj["availability"] != "verified":
                    fail("invalid_reference")
                extension = ".py" if op["source"]["language"] == "python" else ".R"
                bindings.append(
                    {
                        "logicalKey": item["logicalKey"],
                        "sourceObject": entity_ref(obj, "object"),
                        "symbol": item["symbol"],
                        "adapter": obj["adapter"],
                        "serializerId": "omicsclaw-" + obj["adapter"],
                        "serializerVersion": 1,
                        "serializerSourceSha256": hashlib.sha256(
                            (SCIENCE / ("notebook_checkpoint" + extension)).read_bytes()
                        ).hexdigest(),
                        "typeSchemaDigest": digest([obj["type"], obj["adapter"], 1]),
                        "dataset": obj["dataset"],
                        "axes": obj["axes"],
                        "matrix": obj["matrix"],
                    }
                )
            details["bindings"] = bindings
            details["checkedShapes"] = [
                self.r.analysis.store.get(c, nid, "object", item["objectId"])["shape"]
                for item in details["group"]
            ]
            details["fileIntents"] = [
                {
                    "logicalKey": item["logicalKey"],
                    "file": item["logicalKey"]
                    + {"anndata": ".h5ad", "python_table": ".pkl", "r_rds": ".rds"}[
                        item["adapter"]
                    ],
                    "artifactId": hashlib.sha256(
                        encoded([oid, item["logicalKey"]])
                    ).hexdigest(),
                }
                for item in bindings
            ]
            c.execute(
                "UPDATE notebook_checkpoint_intents SET document=? WHERE operation_id=?",
                (encoded(details).decode(), oid),
            )
            self.store.transition(
                c,
                nid,
                oid,
                "inspecting",
                expected={"inspecting"},
                stopConfirmed=True,
                bindingsDigest=digest(bindings),
            )

        self.r.service._transaction(work)

    def hash_file(self, path):
        sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
        with self.r.analysis.files.reading(path, self.limits["maxFileBytes"]) as stream:
            while block := stream.read(128 * 1024):
                sha.update(block)
                md5.update(block)
                size += len(block)
        return {"sha256": sha.hexdigest(), "md5": md5.hexdigest(), "bytes": size}

    def prepare_files(self, nid, oid, directory):
        from . import analysis_contracts as wire

        manifest = self.r.analysis.files.read_json(directory / "saved.json", 64 * 1024)
        details = self.r.db.run(lambda c: self.store.details(c, oid))
        evidence = self.r.analysis.files.read_json(
            directory / "runtime.json", 32 * 1024
        )
        wire.fields(
            evidence,
            {
                "schemaVersion",
                "language",
                "runtimeVersion",
                "executable",
                "prefix",
                "libraryPaths",
                "packages",
            },
        )
        if (
            type(evidence["schemaVersion"]) is not int
            or evidence["schemaVersion"] != 1
            or evidence["language"] != details["group"][0]["spec"]["kernel"]["language"]
        ):
            fail("invalid_reference")
        for key in ("runtimeVersion", "executable", "prefix"):
            wire.text(evidence[key], 4096, empty=False)
        for path in wire.sequence(evidence["libraryPaths"], 32):
            wire.text(path, 4096, empty=False)
        packages = evidence["packages"]
        if not isinstance(packages, dict) or len(packages) > 16:
            fail("invalid_reference")
        for name, package in packages.items():
            wire.text(name, 128, empty=False)
            if package is not None:
                wire.fields(package, {"version", "path"})
                for key in ("version", "path"):
                    if package[key] is not None:
                        wire.text(package[key], 4096, empty=False)
        wire.fields(manifest, {"schemaVersion", "objects", "totalBytes"})
        if type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != 1:
            fail("invalid_reference")
        items = manifest.get("objects")
        if not isinstance(items, list) or len(items) != len(details["group"]):
            fail("invalid_reference")
        files, total = [], 0
        for item, group, binding, shape, intent in zip(
            items,
            details["group"],
            details["bindings"],
            details["checkedShapes"],
            details["fileIntents"],
            strict=True,
        ):
            wire.fields(
                item,
                {
                    "logicalKey",
                    "symbol",
                    "adapter",
                    "file",
                    "format",
                    "type",
                    "shape",
                    "bytes",
                    "serializerVersion",
                },
            )
            if (
                item["logicalKey"] != group["logicalKey"]
                or item["symbol"] != group["symbol"]
                or item["adapter"] != group["spec"]["adapter"]
                or item["serializerVersion"] != 1
                or item["file"] != intent["file"]
                or item["shape"] != shape
                or hashlib.sha256(
                    encoded([item["type"], item["adapter"], 1])
                ).hexdigest()
                != binding["typeSchemaDigest"]
            ):
                fail("invalid_reference")
            suffix = {"h5ad": ".h5ad", "rds3": ".rds", "pickle5": ".pkl"}.get(
                item["format"]
            )
            if suffix is None or item["file"] != group["logicalKey"] + suffix:
                fail("invalid_reference")
            wire.text(item["type"], 256)
            for size in wire.sequence(item["shape"], 2, 2):
                wire.number(size)
            measured = self.hash_file(directory / item["file"])
            if measured["bytes"] != item["bytes"]:
                fail("reference_stale", 409)
            total += measured["bytes"]
            if total > self.limits["maxCheckpointBytes"]:
                fail("limit", 413)
            files.append(
                {
                    **item,
                    **measured,
                    "artifactId": intent["artifactId"],
                }
            )
        if type(manifest["totalBytes"]) is not int or manifest["totalBytes"] != total:
            fail("invalid_reference")

        def work(c):
            self.r.fence(c)
            op = self.store.get(c, nid, "operation", oid)
            self.check_capture(c, nid, op)
            details["files"] = files
            details["runtimeEvidence"] = evidence
            c.execute(
                "UPDATE notebook_checkpoint_intents SET document=? WHERE operation_id=?",
                (encoded(details).decode(), oid),
            )
            self.store.transition(
                c, nid, oid, "saving", expected={"saving"}, stopConfirmed=True
            )

        self.r.service._transaction(work)

    async def publish(self, nid, oid):
        import time

        from omicsclaw.memory.artifacts import ArtifactRecord

        op, details = self.r.db.run(
            lambda c: (
                self.store.get(c, nid, "operation", oid),
                self.store.details(c, oid),
            )
        )
        directory = self.directory / oid / "data"
        bindings = []
        for binding, item in zip(details["bindings"], details["files"], strict=True):
            measured = await asyncio.to_thread(self.hash_file, directory / item["file"])
            if any(measured[key] != item[key] for key in ("sha256", "bytes")):
                fail("reference_stale", 409)
            bindings.append(
                {
                    **binding,
                    "artifact": {
                        "artifactId": item["artifactId"],
                        "sha256": item["sha256"],
                        "bytes": item["bytes"],
                    },
                }
            )
        manifest = {
            "schemaVersion": 1,
            "source": op["source"],
            "capturedMutationSeq": op["capturedMutationSeq"],
            "registrationDigest": op["registrationDigest"],
            "bindingsDigest": op["bindingsDigest"],
            "bindings": bindings,
            "stage": op["stage"],
            "metadata": details["metadata"],
            "metadataEvidence": "declared",
            "componentSourceDigest": details["componentSourceDigest"],
            "environmentEvidence": details["environmentEvidence"],
            "runtimeEvidence": details["runtimeEvidence"],
            "sourceExecutionId": details["sourceExecutionId"],
        }
        if op["state"] != "publishing":

            def fixed(c):
                self.r.fence(c)
                self.check_capture(c, nid, op)
                self.store.transition(
                    c,
                    nid,
                    oid,
                    "publishing",
                    expected={"validating"},
                    stopConfirmed=True,
                )

            self.r.service._transaction(fixed)
        for item in details["files"]:
            path = str(directory / item["file"])
            record = ArtifactRecord(
                id=item["artifactId"],
                path=path,
                kind={"h5ad": "h5ad", "rds3": "rds"}.get(item["format"], "other"),
                sha256=item["sha256"],
                size=item["bytes"],
                produced_by="notebook_analysis",
                meta={
                    "notebookId": nid,
                    "checkpointId": op["checkpointId"],
                    "operationId": oid,
                },
                created_at=time.time(),
            )
            await asyncio.to_thread(self.r.artifacts.insert_artifact, record)
            actual = self.r.artifacts.get_artifact(record.id)
            if actual is None or (
                actual.path,
                actual.sha256,
                actual.size,
                actual.meta,
            ) != (record.path, record.sha256, record.size, record.meta):
                fail("durability_failed", 503)

        def ready(c):
            self.r.fence(c)
            return self.store.published(c, nid, oid, manifest)

        result = self.r.service._transaction(ready)
        await asyncio.to_thread(self.collect, nid)
        return result

    def inject(self, state):
        eid = state.current_execution_token

        def read(c):
            if (
                self.execution(c, eid)
                or c.execute(
                    "SELECT 1 FROM notebook_analysis_operations WHERE execution_id=?",
                    (eid,),
                ).fetchone()
            ):
                return None
            row = c.execute(
                "SELECT semantic_json FROM notebook_runtime_executions WHERE execution_id=?",
                (eid,),
            ).fetchone()
            return json.loads(row[0]) if row else None

        command = self.r.db.run(read)
        if command is None:
            return
        directory = private_directory(self.directory / ("stage-" + eid))
        strict = command["origin"] == "agent"
        if state.language == "python":
            source = f"""def __omicsclaw_stage_install():
    ns = globals()
    if 'notebook_stage' in ns and ns['notebook_stage'] is not ns.get('__omicsclaw_stage_function'):
        if {strict!r}: raise RuntimeError('notebook_stage conflicts with an existing name')
        return
    function = __import__('runpy').run_path({str(SCIENCE / "checkpoint_science.py")!r})['stage_writer']({str(directory)!r})
    ns['notebook_stage'] = function
    ns['__omicsclaw_stage_function'] = function
try:
    __omicsclaw_stage_install()
finally:
    del __omicsclaw_stage_install
"""
        else:
            quote = lambda value: json.dumps(str(value), ensure_ascii=False)
            source = (
                "local({g<-.GlobalEnv;name<-'notebook_stage';marker<-'__omicsclaw_stage_function';conflict<-exists(name,g,inherits=FALSE)&&(bindingIsActive(name,g)||!exists(marker,g,inherits=FALSE)||bindingIsActive(marker,g)||!identical(get(name,g,inherits=FALSE),get(marker,g,inherits=FALSE)));"
                + (
                    "if(conflict) stop('notebook_stage conflicts with an existing name');"
                    if strict
                    else ""
                )
                + "if(!conflict){fixed<-new.env(parent=baseenv());sys.source("
                + quote(SCIENCE / "checkpoint_science.R")
                + ",envir=fixed);fn<-fixed$checkpoint_stage_writer("
                + quote(directory)
                + ");assign(name,fn,g);assign(marker,fn,g)}})"
            )
        result = state.kernel.execute(source, store_history=False, timeout_s=15)
        if result.status != "ok":
            fail("unavailable", 503)

    def terminal(self, c, nid, eid, status, kernel):
        private = self.execution(c, eid)
        if private:
            if self.r.restores.contains(c, private[0]):
                return self.r.restores.terminal(c, nid, private[0])
            op = self.store.get(c, nid, "operation", private[0])
            if op["state"] in {
                "inspecting",
                "saving",
                "validating",
                "stopping_unconfirmed",
            }:
                self.store.transition(
                    c,
                    nid,
                    private[0],
                    op["state"],
                    expected={op["state"]},
                    stopConfirmed=True,
                )
            return
        if (
            status != "completed"
            or c.execute(
                "SELECT 1 FROM notebook_checkpoint_stages WHERE execution_id=?", (eid,)
            ).fetchone()
        ):
            return
        directory = self.directory / ("stage-" + eid)
        path = directory / "stage.json"
        if not path.exists():
            return
        reason = None
        try:
            item = self.r.analysis.files.read_json(path, 4096)
            if (
                set(item) != {"schemaVersion", "stage", "ordinal", "metadata"}
                or item["schemaVersion"] != 1
                or item["ordinal"] != 1
                or item["stage"]
                not in {
                    "qc",
                    "normalization",
                    "integration",
                    "clustering",
                    "annotation",
                    "differential_expression",
                }
                or (directory / "duplicate").exists()
            ):
                raise ValueError("invalid_checkpoint_stage")
        except (ValueError, TypeError, OSError):
            item, reason = {"stage": None, "metadata": None}, "invalid_checkpoint_stage"
        stage = (
            {"executionId": eid, "stage": item["stage"], "ordinal": 1}
            if item["stage"]
            else None
        )
        group = self.store.registration(c, nid, kernel_ref(kernel))[0]
        c.execute(
            "INSERT INTO notebook_checkpoint_stages VALUES(?,?,?)",
            (
                eid,
                nid,
                encoded(
                    {
                        "stage": stage,
                        "metadata": item["metadata"],
                        "reason": reason
                        or (None if group else "no_registered_objects"),
                    }
                ).decode(),
            ),
        )
        if group:
            self.store.intent(
                c,
                nid,
                kernel_ref(kernel),
                stage=stage,
                metadata=item["metadata"],
                automatic=True,
                failure_reason=reason,
            )

    def usage(self, c):
        physical = self.r.analysis.files.disk_usage()
        staging, reserved = 0, 0
        for oid, amount, kind in c.execute(
            "SELECT operation_id,reserved_bytes,'checkpoints' FROM notebook_checkpoint_intents WHERE released=0 UNION ALL SELECT operation_id,reserved_bytes,'restores' FROM notebook_restore_intents WHERE gate_active=1"
        ):
            size = 0
            directory = self.r.analysis.files.directory / kind / oid
            if directory.exists():
                for path in directory.rglob("*"):
                    if path.is_symlink():
                        fail("unavailable", 503)
                    if path.is_file():
                        size += path.stat().st_size
            staging += size
            reserved += max(0, amount - size)
        return {
            "workspaceBytes": physical,
            "stagingBytes": staging,
            "reservedBytes": reserved,
        }

    def collect(self, nid):
        """Retire unheld groups first, then clean only their fixed owned paths."""

        def reserve(c):
            self.r.fence(c)
            rows = c.execute(
                "SELECT document FROM notebook_checkpoint_records r WHERE notebook_id=? AND kind='checkpoint' AND revision=(SELECT MAX(x.revision) FROM notebook_checkpoint_records x WHERE x.notebook_id=r.notebook_id AND x.kind=r.kind AND x.entity_id=r.entity_id) AND json_extract(document,'$.state')='ready' AND NOT EXISTS(SELECT 1 FROM notebook_checkpoint_gc g WHERE g.checkpoint_id=r.entity_id) ORDER BY json_extract(document,'$.createdAt') DESC LIMIT 45",
                (nid,),
            ).fetchall()
            for row in rows[5:]:
                item = json.loads(row[0])
                cid = item["checkpointId"]
                if (
                    item["pinned"]
                    or c.execute(
                        "SELECT 1 FROM notebook_checkpoint_holds WHERE checkpoint_id=? LIMIT 1",
                        (cid,),
                    ).fetchone()
                ):
                    continue
                op = self.store.get(c, nid, "operation", item["operationId"])
                if op["state"] != "ready":
                    continue
                details = self.store.details(c, item["operationId"])
                c.execute(
                    "INSERT OR IGNORE INTO notebook_checkpoint_gc VALUES(?,?,?,0)",
                    (
                        cid,
                        nid,
                        encoded(
                            {
                                "operationId": item["operationId"],
                                "files": details["files"],
                            }
                        ).decode(),
                    ),
                )
                self.r.analysis.store.advance(c, nid)

        self.r.service._transaction(reserve)
        pending = self.r.db.run(
            lambda c: c.execute(
                "SELECT checkpoint_id,document FROM notebook_checkpoint_gc WHERE notebook_id=? AND finished=0 LIMIT 40",
                (nid,),
            ).fetchall()
        )
        for cid, raw in pending:
            details = json.loads(raw)
            for item in details["files"]:
                path = self.directory / details["operationId"] / "data" / item["file"]
                if path.exists():
                    self.hash_file(path)
                    path.unlink()
                self.r.artifact_db.run(
                    lambda c, aid=item["artifactId"], fixed=str(path): c.execute(
                        "DELETE FROM artifacts WHERE id=? AND path=?", (aid, fixed)
                    )
                )
            self.r.db.run(
                lambda c, cid=cid: c.execute(
                    "UPDATE notebook_checkpoint_gc SET finished=1 WHERE checkpoint_id=?",
                    (cid,),
                )
            )

    def cleanup_failed(self, oid):
        import shutil

        op = self.r.db.run(
            lambda c: c.execute(
                "SELECT notebook_id FROM notebook_checkpoint_intents WHERE operation_id=?",
                (oid,),
            ).fetchone()
        )
        if op is None:
            return
        current = self.r.db.run(lambda c: self.store.get(c, op[0], "operation", oid))
        if current["state"] != "failed" or not current["stopConfirmed"]:
            return
        details = self.r.db.run(lambda c: self.store.details(c, oid))
        directory = self.directory / oid
        if directory.is_symlink():
            fail("unavailable", 503)
        for item in details.get("files", []):
            path = str(directory / "data" / item["file"])
            self.r.artifact_db.run(
                lambda c, aid=item["artifactId"], path=path: c.execute(
                    "DELETE FROM artifacts WHERE id=? AND path=? AND json_extract(meta_json,'$.operationId')=?",
                    (aid, path, oid),
                )
            )
        if directory.exists():
            shutil.rmtree(directory)

    def context(self, c, nid):
        languages, waiting = {}, False
        for language in ("python", "r"):
            ops, failures = self.store.blocked(c, nid, language)
            waiting |= any(
                op["state"]
                in {"intent", "inspecting", "saving", "validating", "publishing"}
                for op in ops
            )
            languages[language] = {
                "operations": [
                    {
                        "operationId": op["operationId"],
                        "state": op["state"],
                        "stopConfirmed": op["stopConfirmed"],
                    }
                    for op in ops
                ],
                "failures": [
                    {
                        "failureId": failure["failureId"],
                        "failureRevision": failure["failureRevision"],
                        "stopConfirmed": failure["stopConfirmed"],
                        "reasonCode": failure["reasonCode"],
                    }
                    for failure in failures
                ],
            }
        return {
            "waiting": waiting,
            "languages": languages,
            "coverage": "registered-only",
            "unregisteredState": "unknown",
        }

    def decision_idle(self, c, nid):
        if c.execute(
            "SELECT 1 FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE e.notebook_id=? AND k.state NOT IN ('lost','released') AND (e.status IN ('running','outcome_unknown') OR json_extract(e.output_json,'$.event_stream_pending')=1) LIMIT 1",
            (nid,),
        ).fetchone():
            fail("busy", 409)

    def inspection_idle(self, c, nid):
        """A fixed inspector may pass only work held by stopped save failures."""
        if c.execute(
            "SELECT 1 FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE e.notebook_id=? AND k.state NOT IN ('lost','released') AND (e.status IN ('running','outcome_unknown') OR json_extract(e.output_json,'$.event_stream_pending')=1) LIMIT 1",
            (nid,),
        ).fetchone():
            self.r.idle(c, nid)
        queued = c.execute(
            "SELECT e.execution_id,k.language FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE e.notebook_id=? AND k.state NOT IN ('lost','released') AND e.status='queued' LIMIT ?",
            (nid, self.r.limits["pending"] + 1),
        ).fetchall()
        if len(queued) > self.r.limits["pending"]:
            self.r.idle(c, nid)
        for eid, language in queued:
            operations, failures = self.store.blocked(c, nid, language)
            if (
                operations
                or not failures
                or any(not item["stopConfirmed"] for item in failures)
                or self.allowed_dispatch(c, nid, eid)
            ):
                self.r.idle(c, nid)
