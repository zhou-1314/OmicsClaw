"""Fixed inspection and explicit selections over the existing owner queue."""

import asyncio
import hashlib
import json
import math
import time
import uuid
from pathlib import Path

from omicsclaw.skillenv import notebook_identity as codec

from . import analysis_contracts as wire
from .analysis_files import AnalysisFiles
from .analysis_helpers import SelectionHelpers
from .analysis_store import AnalysisStore, entity_ref, kernel_ref
from .contracts import bounded, encoded
from .security import private_directory
from .store import OwnerViews, current_kernel


class OwnerAnalysis:
    def __init__(self, runtime):
        self.r = runtime
        self.store = AnalysisStore(runtime)
        self.files = AnalysisFiles(runtime)
        self.selection_helpers = SelectionHelpers(self)

    def expected(self, c, nid, command):
        expected, args, action = (
            command["expected"],
            command["arguments"],
            command["action"],
        )
        if self.store.state(c, nid)["analysisRevision"] != expected["analysisRevision"]:
            wire.fail("reference_stale", 409)
        if action == "selection.clear":
            selected = self.store.state(c, nid)["activeSelection"]
            if selected != args["selection"]:
                wire.fail("reference_stale", 409)
            required = {"kernels": [], "objects": [], "selections": [selected]}
            self.store.validate_selection(c, nid, selected, valid=False)
        elif action == "object.remove":
            obj = self.store.get(c, nid, "object", args["object"]["objectId"])
            if (
                entity_ref(obj, "object") != args["object"]
                or obj["registrationState"] == "removed"
            ):
                wire.fail("reference_stale", 409)
            required = {"kernels": [], "objects": [args["object"]], "selections": []}
        elif action == "object.register":
            required = {"kernels": [args["kernel"]], "objects": [], "selections": []}
        else:
            if action == "object.inspect":
                obj = self.store.get(c, nid, "object", args["object"]["objectId"])
                if (
                    entity_ref(obj, "object") != args["object"]
                    or obj["registrationState"] != "active"
                ):
                    wire.fail("reference_stale", 409)
            else:
                view_ref = (
                    args["view"] if action == "selection.create" else args["targetView"]
                )
                view = self.store.get(
                    c, nid, "view", view_ref["viewId"], view_ref["viewRevision"]
                )
                if not view["valid"]:
                    wire.fail("reference_stale", 409)
                obj = self.store.validate_object(c, nid, view["object"])
            required = {
                "kernels": [obj["kernel"]],
                "objects": [entity_ref(obj, "object")],
                "selections": [args["selection"]]
                if action == "selection.remap"
                else [],
            }
        if {key: expected[key] for key in required} != required:
            wire.fail("invalid_reference")
        for identity in required["kernels"]:
            row = current_kernel(c, nid, identity["language"])
            if (
                row is None
                or kernel_ref(row) != identity
                or row["state"] not in {"ready", "reserved"}
            ):
                wire.fail("reference_stale", 409)
            if self.r.environment_guard is not None:
                guard = self.r.environment_guard
                guard.coordinator.check(
                    guard.key("kernel", identity["kernelInstanceId"]), guard.owner
                )
        for language in ("python", "r"):
            self.r.restores.require_clear(c, nid, language)
            self.r.checkpoints.store.require_clear(
                c,
                nid,
                language,
                inspect=action in {"object.inspect", "selection.clear"},
            )
        return required

    @staticmethod
    def receipt(command, digest, entity_id, operation_id):
        return {
            "kind": "analysis",
            "schemaVersion": 1,
            "commandId": command["commandId"],
            "action": command["action"],
            "payloadSha256": digest,
            "acceptedAt": time.time(),
            "status": "accepted",
            "result": {
                "entityKind": "object"
                if command["action"].startswith("object.")
                else "selection",
                "entityId": entity_id,
                "operationId": operation_id,
            },
        }

    def old(self, c, nid, command, digest):
        row = c.execute(
            "SELECT digest,receipt_json FROM notebook_runtime_commands WHERE command_id=?",
            (command["commandId"],),
        ).fetchone()
        if row:
            if row[0] != digest:
                from .service import NotebookError

                raise NotebookError("command_conflict", 409)
            return {
                "receipt": json.loads(row[1]),
                "notebookSeq": OwnerViews.sequence(c, nid),
            }
        return None

    def remember(self, c, nid, command, digest, result, *, agent=None):
        c.execute(
            "INSERT INTO notebook_analysis_receipts VALUES(?,?)",
            (command["commandId"], nid),
        )
        c.execute(
            "INSERT INTO notebook_runtime_commands VALUES(?,?,?)",
            (command["commandId"], digest, encoded(result).decode()),
        )
        if agent:
            c.execute(
                "INSERT INTO notebook_analysis_agent_receipts VALUES(?,?,?,?)",
                (
                    command["commandId"],
                    agent["sessionId"],
                    nid,
                    hashlib.sha256(
                        encoded([agent["action"], agent["arguments"]])
                    ).hexdigest(),
                ),
            )
        return {"receipt": result, "notebookSeq": OwnerViews.sequence(c, nid)}

    def agent_scope(self, c, params):
        old = self.r.requests.decode(
            params["token"], params["sessionId"], params["requestId"]
        )
        facts, binding, _, _ = self.r.requests.facts(
            c, params["sessionId"], params["requestId"]
        )
        if binding is None or old["notebookId"] != facts["notebookId"]:
            wire.fail("unavailable", 503)
        return facts["notebookId"]

    def agent_receipt(self, c, nid, params, cid, *, digest=None):
        row = c.execute(
            "SELECT a.session_id,a.notebook_id,a.invocation_digest,r.receipt_json FROM notebook_analysis_agent_receipts a JOIN notebook_runtime_commands r USING(command_id) WHERE a.command_id=?",
            (cid,),
        ).fetchone()
        if row is None or row[0] != params["sessionId"] or row[1] != nid:
            return {"found": False, "receipt": None}
        if digest is not None and row[2] != digest:
            from .service import NotebookError

            raise NotebookError("command_conflict", 409)
        return {"found": True, "receipt": json.loads(row[3])}

    async def agent(self, params):
        wire.fields(
            params,
            {
                "action",
                "arguments",
                "sessionId",
                "requestId",
                "token",
                "toolPosition",
                "commandId",
            },
        )
        for key in ("sessionId", "requestId", "commandId"):
            wire.identity(params[key])
        wire.number(params["toolPosition"])
        if len(encoded(params)) > 80 * 1024:
            wire.fail("limit", 413)
        action, args = params["action"], params["arguments"]
        if (
            not isinstance(args, dict)
            or "notebookId" in args
            or action
            not in {
                "state",
                "page",
                "receipt",
                "object.register",
                "object.inspect",
                "object.remove",
                "checkpoint.save",
                "checkpoint.retry",
                "checkpoint.pin",
                "selection.create",
                "selection.remap",
                "selection.clear",
            }
        ):
            wire.fail()
        self.r.authorize()
        nid = self.r.db.run(lambda c: self.agent_scope(c, params))
        if action == "receipt":
            wire.fields(args, {"commandId"})
            wire.identity(args["commandId"])
            return self.r.db.run(
                lambda c: self.agent_receipt(c, nid, params, args["commandId"])
            )
        if action == "object.register" and "symbol" in args:
            args = {
                "adapter": "anndata",
                "sourceRefs": [],
                "rowIdentity": {"kind": "index"},
                "columnIdentity": {"kind": "index"},
                "tableAxis": "row",
                "declaredMatrix": {
                    "assay": None,
                    "layer": None,
                    "semantics": "unknown",
                },
                "declaredMetadata": {
                    "organism": None,
                    "sampleColumn": None,
                    "conditionColumn": None,
                    "donorColumn": None,
                },
                **args,
            }
            if isinstance(args.get("declaredMatrix"), dict):
                args["declaredMatrix"] = {
                    "assay": None,
                    "layer": None,
                    "semantics": "unknown",
                    **args["declaredMatrix"],
                }
            if isinstance(args.get("declaredMetadata"), dict):
                args["declaredMetadata"] = {
                    "organism": None,
                    "sampleColumn": None,
                    "conditionColumn": None,
                    "donorColumn": None,
                    **args["declaredMetadata"],
                }
            if "kernel" not in args and nid:
                lang = "r" if args.get("adapter") == "r_rds" else "python"
                k_row = self.r.db.run(lambda c: current_kernel(c, nid, lang))
                if k_row is not None:
                    args["kernel"] = kernel_ref(k_row)
            params = {**params, "arguments": args}
        digest = hashlib.sha256(encoded([action, args])).hexdigest()
        old = self.r.db.run(
            lambda c: self.agent_receipt(
                c, nid, params, params["commandId"], digest=digest
            )
        )
        if old["found"]:
            return old
        self.r.db.run(lambda c: self.check_agent(c, params))
        if action in {"state", "page"}:
            return (
                self.store.state_response if action == "state" else self.store.page
            )({"notebookId": nid, **args})

        if action.startswith("checkpoint."):
            from .checkpoint_wire import validate

            validate(
                nid,
                {
                    "schemaVersion": 1,
                    "commandId": params["commandId"],
                    "action": action,
                    "arguments": args,
                    "expected": {
                        "analysisRevision": 0,
                        "kernels": [],
                        "objects": [],
                        "selections": [],
                    },
                },
            )
            expected = self.r.db.run(
                lambda c: {
                    "analysisRevision": self.store.state(c, nid)["analysisRevision"],
                    **self.r.checkpoints.wire.required(c, nid, action, args),
                }
            )
            return await self.r.checkpoints.wire.command(
                {
                    "notebookId": nid,
                    "command": {
                        "schemaVersion": 1,
                        "commandId": params["commandId"],
                        "action": action,
                        "arguments": args,
                        "expected": expected,
                    },
                },
                agent=params,
            )

        wire.command(
            nid,
            {
                "schemaVersion": 1,
                "commandId": params["commandId"],
                "action": action,
                "arguments": args,
                "expected": {
                    "analysisRevision": 0,
                    "kernels": [],
                    "objects": [],
                    "selections": [],
                },
            },
        )

        def prepare(c):
            self.check_agent(c, params)
            expected = {
                "analysisRevision": self.store.state(c, nid)["analysisRevision"],
                "kernels": [],
                "objects": [],
                "selections": [],
            }
            if action == "object.register":
                expected["kernels"] = [args.get("kernel")]
            elif action == "object.remove":
                expected["objects"] = [args.get("object")]
            elif action == "selection.clear":
                expected["selections"] = [args.get("selection")]
            elif action in {"object.inspect", "selection.create", "selection.remap"}:
                if action == "object.inspect":
                    obj = self.store.get(
                        c, nid, "object", args.get("object", {}).get("objectId")
                    )
                else:
                    view = args.get(
                        "view" if action == "selection.create" else "targetView", {}
                    )
                    target = self.store.get(
                        c, nid, "view", view.get("viewId"), view.get("viewRevision")
                    )
                    obj = self.store.get(c, nid, "object", target["object"]["objectId"])
                expected["kernels"] = [obj["kernel"]]
                expected["objects"] = [entity_ref(obj, "object")]
                if action == "selection.remap":
                    expected["selections"] = [args.get("selection")]
            return {
                "schemaVersion": 1,
                "commandId": params["commandId"],
                "action": action,
                "arguments": args,
                "expected": expected,
            }

        command = self.r.db.run(prepare)
        return await self.command({"notebookId": nid, "command": command}, agent=params)

    async def command(self, params, *, agent=None):
        wire.fields(params, {"notebookId", "command"})
        nid, command = params["notebookId"], params["command"]
        if isinstance(command, dict) and str(command.get("action", "")).startswith(
            "checkpoint."
        ):
            return await self.r.checkpoints.wire.command(params, agent=agent)
        digest = wire.command(nid, command)
        self.r.authorize()
        old = self.r.db.run(lambda c: self.old(c, nid, command, digest))
        if old:
            return old
        if command["action"] in {"object.register", "object.inspect"}:
            return await self.inspect(nid, command, digest, agent=agent)
        async with self.r.operation_lock:
            if command["action"] not in {"selection.clear", "object.remove"}:
                self.r._budget()

            def write(c):
                self.r.authorize()
                self.r.fence(c)
                old = self.old(c, nid, command, digest)
                if old:
                    return old
                self.r.idle(c, nid)
                if agent:
                    self.check_agent(c, agent)
                self.expected(c, nid, command)
                args, action = command["arguments"], command["action"]
                if action == "object.remove":
                    entity_id = args["object"]["objectId"]
                    obj = self.store.raw(c, nid, "object", entity_id)
                    obj.update(
                        objectRevision=obj["objectRevision"] + 1,
                        registrationState="removed",
                        availability="unavailable",
                        reason="registration_removed",
                        defaultView=None,
                    )
                    self.store.put(c, nid, "object", obj)
                    self.store.advance(c, nid)
                elif action == "selection.clear":
                    entity_id = args["selection"]["selectionId"]
                    self.store.advance(c, nid, active=None)
                else:
                    reference = (
                        args["view"]
                        if action == "selection.create"
                        else args["targetView"]
                    )
                    view = self.store.get(
                        c, nid, "view", reference["viewId"], reference["viewRevision"]
                    )
                    source, values, remapped = None, None, None
                    if action == "selection.remap":
                        original = self.store.validate_selection(
                            c, nid, args["selection"], valid=False
                        )
                        original_view = self.store.get(
                            c,
                            nid,
                            "view",
                            original["view"]["viewId"],
                            original["view"]["viewRevision"],
                        )
                        if (
                            original["count"] != args["expectedMemberCount"]
                            or original["membersSha256"]
                            != args["expectedMembersSha256"]
                            or original["dataset"] != view["dataset"]
                            or original["axis"]["keys"] != view["axis"]["keys"]
                            or original["axis"]["role"] != view["axis"]["role"]
                            or original_view["matrix"] != view["matrix"]
                        ):
                            wire.fail("selection_invalid", 409)
                        source, remapped = original["memberArtifact"], args["selection"]
                    elif args["members"]["kind"] == "idArtifact":
                        source = args["members"]["artifact"]
                        self.files.validate_artifact(
                            c, nid, source, selection_input=True
                        )
                    else:
                        values = args["members"]["values"]
                    members = self.files.members(
                        c, nid, view, values=values, source=source
                    )
                    entity_id = uuid.uuid4().hex
                    item = {
                        "selectionId": entity_id,
                        "selectionRevision": 1,
                        "object": view["object"],
                        "dataset": view["dataset"],
                        "view": reference,
                        "axis": view["axis"],
                        **members,
                        "valid": True,
                        "reason": "",
                        "remappedFrom": remapped,
                    }
                    self.store.put(c, nid, "selection", item)
                    self.store.advance(c, nid, active=entity_ref(item, "selection"))
                if not agent:
                    self.r.requests.takeover(c, nid)
                result = self.receipt(command, digest, entity_id, None)
                return self.remember(c, nid, command, digest, result, agent=agent)

            return bounded(self.files.transaction(write))

    @staticmethod
    def source(spec, directory):
        root = Path(__file__).resolve().parents[2] / "skillenv"
        request = {
            "schemaVersion": 1,
            **{
                key: spec[key]
                for key in (
                    "symbol",
                    "adapter",
                    "rowIdentity",
                    "columnIdentity",
                    "tableAxis",
                    "declaredMatrix",
                )
            },
        }
        if spec["kernel"]["language"] == "python":
            return (
                "(lambda ns: __import__('runpy').run_path("
                + repr(str(root / "notebook_inspect.py"))
                + ')["inspect_object"](ns, '
                + repr(request)
                + ", "
                + repr(str(directory))
                + ", __import__('runpy').run_path("
                + repr(str(root / "notebook_identity.py"))
                + ")))(globals())"
            )
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        return (
            "local({ inspector <- new.env(parent=baseenv()); sys.source("
            + json.dumps(str(root / "notebook_identity.R"), ensure_ascii=False)
            + ", envir=inspector); sys.source("
            + json.dumps(str(root / "notebook_inspect.R"), ensure_ascii=False)
            + ", envir=inspector); inspector$inspect_object(.GlobalEnv, jsonlite::fromJSON("
            + json.dumps(payload, ensure_ascii=False)
            + ", simplifyVector=FALSE), "
            + json.dumps(str(directory), ensure_ascii=False)
            + ") })"
        )

    async def inspect(self, nid, command, digest, *, agent=None):
        def prepare(c):
            self.r.fence(c)
            self.inspection_idle(c, nid, command["action"])
            if agent:
                self.check_agent(c, agent)
            self.expected(c, nid, command)
            if command["action"] == "object.register":
                spec = command["arguments"]
                for source in spec["sourceRefs"]:
                    self.files.validate_artifact(c, nid, source)
                active = self.store.context(c, nid)["guard"]["objects"]
                if len(active) >= 32:
                    wire.fail("limit", 413)
                oid = uuid.uuid4().hex
            else:
                oid = command["arguments"]["object"]["objectId"]
                spec = json.loads(
                    c.execute(
                        "SELECT document FROM notebook_analysis_specs WHERE notebook_id=? AND object_id=?",
                        (nid, oid),
                    ).fetchone()[0]
                )
            return oid, spec

        oid, spec = self.r.db.run(prepare)
        internal_command = hashlib.sha256(
            ("inspect:" + nid + ":" + command["commandId"]).encode()
        ).hexdigest()
        eid = hashlib.sha256(json.dumps([nid, internal_command]).encode()).hexdigest()
        directory = private_directory(self.files.staging / eid)
        semantic = {
            "version": 4 if agent else 3,
            "notebookId": nid,
            "cellId": internal_command,
            "commandId": internal_command,
            "source": self.source(spec, directory),
            "expectedRevision": 0,
            **spec["kernel"],
            "origin": "agent" if agent else "user",
            "approvalScope": "permission_gate" if agent else "explicit_user",
            "timeoutSeconds": 60,
            "preconditions": {
                "analysis": {
                    key: command["expected"][key]
                    for key in ("analysisRevision", "objects", "selections")
                }
            },
        }
        if agent:
            semantic["preconditions"].update(
                {
                    k: agent[k]
                    for k in ("sessionId", "requestId", "token", "toolPosition")
                }
            )
        operation = {
            "notebookId": nid,
            "objectId": oid,
            "command": command,
            "digest": digest,
            "spec": spec,
            "directory": str(directory),
            "agent": agent,
        }
        await self.r._admit(
            {"command": semantic}, agent=bool(agent), analysis_operation=operation
        )
        return self.r.db.run(lambda c: self.old(c, nid, command, digest))

    def claim(self, c, nid, eid, operation):
        self.expected(c, nid, operation["command"])
        self.inspection_idle(c, nid, operation["command"]["action"])
        if operation["agent"]:
            self.check_agent(c, operation["agent"])
        c.execute(
            "INSERT INTO notebook_analysis_operations VALUES(?,?,?,?,?,?,0)",
            (
                eid,
                nid,
                operation["objectId"],
                encoded(operation["command"]).decode(),
                encoded(operation["spec"]).decode(),
                operation["directory"],
            ),
        )
        result = self.receipt(
            operation["command"], operation["digest"], operation["objectId"], eid
        )
        self.store.advance(c, nid)
        self.remember(
            c,
            nid,
            operation["command"],
            operation["digest"],
            result,
            agent=operation["agent"],
        )

    def dispatch(self, c, nid, eid, document):
        operation = c.execute(
            "SELECT command_json FROM notebook_analysis_operations WHERE execution_id=?",
            (eid,),
        ).fetchone()
        if operation:
            command = json.loads(operation[0])
            command["expected"]["analysisRevision"] += 1
            self.expected(c, nid, command)
        else:
            if document.get("origin") != "agent":
                self.store.run_guard(c, nid, document)
            self.store.invalidate(
                c, nid, document["language"], "code_may_modify_object"
            )

    def check_agent(self, c, params):
        self.r.requests.verify(
            c,
            params["token"],
            params["sessionId"],
            params["requestId"],
            executable=False,
        )
        facts, binding, _, _ = self.r.requests.facts(
            c, params["sessionId"], params["requestId"]
        )
        if binding is None:
            wire.fail("unavailable", 503)
        if params.get("action") == "checkpoint.retry":
            self.r.checkpoints.decision_idle(c, facts["notebookId"])
        elif params.get("action") not in {"state", "page", "checkpoint.pin"}:
            self.inspection_idle(c, facts["notebookId"], params.get("action"))
        return facts["notebookId"]

    def inspection_idle(self, c, nid, action):
        if action == "object.inspect":
            self.r.checkpoints.inspection_idle(c, nid)
        else:
            self.r.idle(c, nid)

    def import_axis(self, c, directory, value):
        wire.fields(value, {"axis", "role", "count", "keys", "idsFile", "reason"})
        if value["axis"] not in {"row", "column"} or value["role"] not in {
            "obs",
            "var",
            "sample",
            "gene",
            "row",
            "column",
        }:
            wire.fail("invalid_reference")
        count = wire.number(value["count"])
        keys = codec.keys(value["keys"])
        result = {key: value[key] for key in ("axis", "role", "count", "keys")}
        result.update(
            unique=None, orderedIdsSha256=None, identityEncoding="omicsclaw-tuples-v1"
        )
        if value["idsFile"] is None:
            return result
        if value["idsFile"] != value["axis"] + ".ids":
            wire.fail("invalid_reference")
        c.execute(
            "CREATE TEMP TABLE IF NOT EXISTS notebook_axis_pending(axis TEXT,position INTEGER,identity BLOB,PRIMARY KEY(axis,position))"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS notebook_axis_pending_identity ON notebook_axis_pending(axis,identity)"
        )
        c.execute("DELETE FROM notebook_axis_pending WHERE axis=?", (value["axis"],))
        digest = hashlib.sha256(
            b"omicsclaw-axis-v1\n" + codec.tuple_bytes(keys) + b"\n"
        )
        actual = 0
        with self.files.reading(
            directory / value["idsFile"], codec.MAX_ID_BYTES
        ) as stream:
            for position, row in enumerate(codec.read_rows(stream, keys)):
                raw = codec.tuple_bytes(row)
                digest.update(raw + b"\n")
                c.execute(
                    "INSERT INTO notebook_axis_pending VALUES(?,?,?)",
                    (value["axis"], position, raw),
                )
                actual += 1
        if actual != count:
            wire.fail("invalid_reference")
        duplicate = c.execute(
            "SELECT 1 FROM notebook_axis_pending WHERE axis=? GROUP BY identity HAVING COUNT(*)>1 LIMIT 1",
            (value["axis"],),
        ).fetchone()
        result.update(unique=duplicate is None, orderedIdsSha256=digest.hexdigest())
        return result

    def import_inspection(self, c, operation):
        nid, oid = operation["notebook_id"], operation["object_id"]
        spec, directory = (
            json.loads(operation["spec_json"]),
            Path(operation["directory"]),
        )
        manifest = self.files.read_json(directory / "inspection.json")
        if (
            not isinstance(manifest, dict)
            or manifest.get("schemaVersion") != 1
            or manifest.get("status") != "ok"
        ):
            wire.fail("unavailable", 503)
        wire.fields(
            manifest,
            {"schemaVersion", "status", "type", "shape", "axes", "matrix", "table"},
            optional={"warnings"},
        )
        wire.text(manifest["type"], 256)
        shape = wire.sequence(manifest["shape"], 2, 2)
        for size in shape:
            wire.number(size)
        axes = [
            self.import_axis(c, directory, item)
            for item in wire.sequence(manifest["axes"], 2, 2)
        ]
        if [item["axis"] for item in axes] != ["row", "column"] or [
            item["count"] for item in axes
        ] != shape[:2]:
            wire.fail("invalid_reference")
        matrix = wire.fields(
            manifest["matrix"],
            {
                "assay",
                "layer",
                "exists",
                "axesAligned",
                "semantics",
                "semanticsEvidence",
                "reason",
            },
        )
        for key in ("assay", "layer"):
            if matrix[key] is not None:
                wire.text(matrix[key], 128)
        for key in ("exists", "axesAligned"):
            if matrix[key] is not None and type(matrix[key]) is not bool:
                wire.fail("invalid_reference")
        if (
            matrix["semanticsEvidence"] not in {"declared", "unknown"}
            or matrix["semantics"] != spec["declaredMatrix"]["semantics"]
        ):
            wire.fail("invalid_reference")
        wire.text(matrix["reason"])
        old = c.execute(
            "SELECT document FROM notebook_analysis_entities WHERE notebook_id=? AND kind='object' AND entity_id=? ORDER BY revision DESC LIMIT 1",
            (nid, oid),
        ).fetchone()
        old = json.loads(old[0]) if old else None
        dataset = (
            old["dataset"]
            if old
            else operation["dataset"]
            if "dataset" in operation.keys()
            else {"datasetId": uuid.uuid4().hex, "datasetRevision": 1}
        )
        if old is None and "dataset" not in operation.keys():
            self.store.put(
                c,
                nid,
                "dataset",
                {
                    **dataset,
                    "sourceRefs": spec["sourceRefs"],
                    "metadata": spec["declaredMetadata"],
                    "metadataEvidence": "declared"
                    if any(
                        value is not None for value in spec["declaredMetadata"].values()
                    )
                    else "unknown",
                    "producerExecutionId": operation["execution_id"],
                },
            )
        item = {
            "objectId": oid,
            "objectRevision": old["objectRevision"] + 1 if old else 1,
            "dataset": dataset,
            "kernel": spec["kernel"],
            "symbol": spec["symbol"],
            "adapter": spec["adapter"],
            "type": manifest["type"],
            "shape": shape,
            "axes": axes,
            "matrix": matrix,
            "availability": "verified",
            "reason": "",
            "producerExecutionId": operation["execution_id"],
            "defaultView": None,
        }
        selected_axis = next(
            value for value in axes if value["axis"] == spec["tableAxis"]
        )
        table = manifest["table"]
        if (
            selected_axis["unique"] is True
            and table is not None
            and matrix["exists"] is not False
            and matrix["axesAligned"] is not False
        ):
            wire.fields(table, {"axis", "columns", "rowsFile", "rowCount"})
            if (
                table["axis"] != spec["tableAxis"]
                or table["rowsFile"] != "table.rows.jsonl"
                or table["rowCount"] != selected_axis["count"]
            ):
                wire.fail("invalid_reference")
            columns = wire.sequence(table["columns"], 20, 1)
            for column in columns:
                wire.text(column, 128)
            if (
                len(set(columns)) != len(columns)
                or columns[: len(selected_axis["keys"])] != selected_axis["keys"]
            ):
                wire.fail("invalid_reference")
            vid = hashlib.sha256(("table:" + oid).encode()).hexdigest()
            version = c.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM notebook_analysis_entities WHERE notebook_id=? AND kind='view' AND entity_id=?",
                (nid, vid),
            ).fetchone()[0]
            count = 0
            with self.files.reading(
                directory / table["rowsFile"], 64 * 1024 * 1024
            ) as stream:
                while raw := stream.readline(wire.MAX_ITEM_BYTES + 1):
                    if len(raw) > wire.MAX_ITEM_BYTES:
                        wire.fail("limit", 413)
                    row = wire.fields(json.loads(raw), {"id", "values"})
                    identity = codec.tuple_bytes(row["id"])
                    actual = c.execute(
                        "SELECT identity FROM notebook_axis_pending WHERE axis=? AND position=?",
                        (selected_axis["axis"], count),
                    ).fetchone()
                    if (
                        actual is None
                        or actual[0] != identity
                        or not isinstance(row["values"], list)
                        or len(row["values"]) != len(columns)
                    ):
                        wire.fail("invalid_reference")
                    for value in row["values"]:
                        if isinstance(value, str):
                            wire.text(value)
                        elif value is not None and (
                            type(value) not in {bool, int, float}
                            or isinstance(value, float)
                            and not math.isfinite(value)
                        ):
                            wire.fail("invalid_reference")
                    c.execute(
                        "INSERT INTO notebook_analysis_view_rows VALUES(?,?,?,?,?,?)",
                        (nid, vid, version, count, identity, encoded(row).decode()),
                    )
                    count += 1
            if count != table["rowCount"]:
                wire.fail("invalid_reference")
            view = {
                "viewId": vid,
                "viewRevision": version,
                "kind": "table",
                "object": entity_ref(item, "object"),
                "dataset": dataset,
                "axis": selected_axis,
                "matrix": matrix,
                "columns": columns,
                "valid": True,
                "reason": "",
            }
            self.store.put(c, nid, "view", view)
            item["defaultView"] = entity_ref(view, "view")
        else:
            item.update(
                availability="needs_inspection", reason="axis_or_matrix_not_verifiable"
            )
        self.store.put(c, nid, "object", item)
        c.execute(
            "INSERT INTO notebook_analysis_specs VALUES(?,?,?) ON CONFLICT(notebook_id,object_id) DO UPDATE SET document=excluded.document",
            (nid, oid, operation["spec_json"]),
        )
        self.store.advance(c, nid)

    async def finish(self, nid, eid, status, output):
        operation = self.r.db.run(
            lambda c: c.execute(
                "SELECT * FROM notebook_analysis_operations WHERE execution_id=?",
                (eid,),
            ).fetchone()
        )
        if operation is None:
            if status == "completed":
                try:
                    consumed = await asyncio.to_thread(
                        self.files.transaction,
                        lambda c: self.selection_helpers.collect(c, nid, eid),
                    )
                    await asyncio.to_thread(self.files.clean, consumed)
                except (ValueError, OSError, KeyError, TypeError):
                    return "failed", {
                        **output,
                        "error": {
                            "ename": "NotebookSelectionInputFailed",
                            "evalue": "Selection input files were not registered.",
                        },
                    }
            return status, output
        if operation["finished"]:
            return status, output

        def complete(c):
            self.r.fence(c)
            if status == "completed":
                self.import_inspection(c, operation)
            c.execute(
                "UPDATE notebook_analysis_operations SET finished=1 WHERE execution_id=?",
                (eid,),
            )

        try:
            await asyncio.to_thread(self.r.service._transaction, complete)
        except (ValueError, OSError, KeyError, TypeError):

            def failed(c):
                self.r.fence(c)
                c.execute(
                    "UPDATE notebook_analysis_operations SET finished=1 WHERE execution_id=?",
                    (eid,),
                )

            await asyncio.to_thread(self.r.service._transaction, failed)
            return "failed", {
                **output,
                "error": {
                    "ename": "NotebookInspectionFailed",
                    "evalue": "Scientific object inspection did not establish valid metadata.",
                },
            }
        if status == "completed":
            directory = Path(operation["directory"])
            await asyncio.to_thread(
                self.files.clean,
                [
                    directory / name
                    for name in (
                        "inspection.json",
                        "row.ids",
                        "column.ids",
                        "table.rows.jsonl",
                    )
                ],
            )
        return status, output
