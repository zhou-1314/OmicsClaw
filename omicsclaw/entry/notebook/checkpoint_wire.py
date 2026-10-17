"""Checkpoint projections and exact actions share the existing analysis protocol."""

import base64
import hashlib
import json
import time

from . import analysis_contracts as w
from .analysis_store import entity_ref, kernel_ref
from .checkpoint_store import fail, reference
from .contracts import bounded, encoded
from .service import NotebookError
from .store import OwnerViews, current_kernel

SUBJECTS = {
    "checkpoint": "checkpoint",
    "checkpointFailure": "failure",
    "checkpointOperation": "operation",
    "checkpointRestoreOperation": "restore",
}
COLLECTIONS = {
    "checkpoints": "checkpoint",
    "checkpointFailures": "failure",
    "checkpointOperations": "operation",
    "checkpointRestoreOperations": "restore",
}
ACTIONS = frozenset(
    {
        "checkpoint.save",
        "checkpoint.retry",
        "checkpoint.continue",
        "checkpoint.pin",
        "checkpoint.restore",
    }
)


def validate(nid, command):
    try:
        w.identity(nid)
        w.fields(
            command, {"schemaVersion", "commandId", "expected", "action", "arguments"}
        )
        if (
            type(command["schemaVersion"]) is not int
            or command["schemaVersion"] != 1
            or command["action"] not in ACTIONS
        ):
            fail()
        w.identity(command["commandId"])
        expected = w.fields(
            command["expected"],
            {"analysisRevision", "kernels", "objects", "selections"},
        )
        w.number(expected["analysisRevision"])
        for kernel in w.sequence(expected["kernels"], 1):
            w.kernel(kernel)
        w.refs(expected["objects"], "object")
        if expected["selections"] != []:
            fail("invalid_reference")
        args, action = command["arguments"], command["action"]
        if action == "checkpoint.restore":
            w.fields(args, {"checkpoint", "target"})
            w.kernel(args["target"])
            w.fields(
                args["checkpoint"],
                {"checkpointId", "manifestRevision", "manifestSha256"},
            )
            w.identity(args["checkpoint"]["checkpointId"])
            w.number(args["checkpoint"]["manifestRevision"], 1)
            w.sha(args["checkpoint"]["manifestSha256"])
        elif action == "checkpoint.save":
            w.fields(args, {"kernel", "objects", "policyRevision"})
            w.kernel(args["kernel"])
            w.refs(w.sequence(args["objects"], 32, 1), "object")
            w.number(args["policyRevision"], 1)
        elif action in {"checkpoint.retry", "checkpoint.continue"}:
            w.fields(
                args,
                {"failure"}
                | ({"acknowledgement"} if action.endswith("continue") else set()),
            )
            w.fields(args["failure"], {"failureId", "failureRevision"})
            w.identity(args["failure"]["failureId"])
            w.number(args["failure"]["failureRevision"], 1)
            if (
                action.endswith("continue")
                and args["acknowledgement"] != "continue_without_this_checkpoint"
            ):
                fail()
        else:
            w.fields(args, {"checkpoint", "expectedPinRevision", "pinned"})
            w.fields(
                args["checkpoint"],
                {"checkpointId", "manifestRevision", "manifestSha256"},
            )
            w.identity(args["checkpoint"]["checkpointId"])
            w.number(args["checkpoint"]["manifestRevision"], 1)
            w.sha(args["checkpoint"]["manifestSha256"])
            w.number(args["expectedPinRevision"])
            if type(args["pinned"]) is not bool:
                fail()
        if len(encoded(command)) > 64 * 1024:
            fail("limit", 413)
    except NotebookError as error:
        if error.code.startswith("notebook_analysis_"):
            fail("invalid_command")
        raise
    return hashlib.sha256(encoded([nid, command])).hexdigest()


class CheckpointWire:
    def __init__(self, checkpoints):
        self.cp, self.r, self.s = checkpoints, checkpoints.r, checkpoints.store

    def required(self, c, nid, action, args):
        if action == "checkpoint.restore":
            return {"kernels": [args["target"]], "objects": [], "selections": []}
        if action == "checkpoint.save":
            return {
                "kernels": [args["kernel"]],
                "objects": args["objects"],
                "selections": [],
            }
        if action in {"checkpoint.retry", "checkpoint.continue"}:
            failure = self.s.get(c, nid, "failure", args["failure"]["failureId"])
            return {
                "kernels": [failure["source"]],
                "objects": failure["objects"],
                "selections": [],
            }
        return {"kernels": [], "objects": [], "selections": []}

    async def command(self, params, *, agent=None):
        try:
            return await self._command(params, agent=agent)
        except NotebookError as error:
            self.translate(error)

    @staticmethod
    def translate(error):
        if error.code.startswith("notebook_analysis_"):
            suffix = error.code.removeprefix("notebook_analysis_")
            fail(
                suffix
                if suffix
                in {
                    "invalid_command",
                    "invalid_reference",
                    "invalid_page",
                    "reference_stale",
                    "limit",
                    "not_found",
                    "unavailable",
                }
                else "invalid_command",
                error.status,
            )
        raise error

    async def _command(self, params, *, agent=None):
        w.fields(params, {"notebookId", "command"})
        nid, command = params["notebookId"], params["command"]
        digest = validate(nid, command)
        if agent and command["action"] in {"checkpoint.continue", "checkpoint.restore"}:
            fail()
        self.r.authorize()
        async with self.r.operation_lock:

            def work(c):
                self.r.authorize()
                self.r.fence(c)
                old = self.r.analysis.old(c, nid, command, digest)
                if old:
                    return old
                if agent:
                    self.r.analysis.check_agent(c, agent)
                action, args, expected = (
                    command["action"],
                    command["arguments"],
                    command["expected"],
                )
                if (
                    expected["analysisRevision"]
                    != self.r.analysis.store.state(c, nid)["analysisRevision"]
                ):
                    fail("reference_stale", 409)
                if {
                    key: expected[key] for key in ("kernels", "objects", "selections")
                } != self.required(c, nid, action, args):
                    fail("invalid_reference")
                oid = None
                if action != "checkpoint.pin":
                    for language in ("python", "r"):
                        self.r.restores.require_clear(c, nid, language)
                if action == "checkpoint.restore":
                    entity = oid = self.r.restores.intent(c, nid, args)
                elif action == "checkpoint.save":
                    if args["policyRevision"] != 1:
                        fail("policy_changed", 409)
                    source = args["kernel"]
                    kernel = current_kernel(c, nid, source["language"])
                    if (
                        kernel is None
                        or kernel_ref(kernel) != source
                        or kernel["state"] not in {"ready", "reserved"}
                    ):
                        fail("reference_stale", 409)
                    self.r.idle(c, nid)
                    self.s.require_clear(c, nid, source["language"])
                    group, _ = self.s.registration(c, nid, source)
                    refs = [
                        entity_ref(
                            self.r.analysis.store.get(c, nid, "object", v["objectId"]),
                            "object",
                        )
                        for v in group
                    ]
                    if sorted(refs, key=lambda v: v["objectId"]) != sorted(
                        args["objects"], key=lambda v: v["objectId"]
                    ):
                        fail("invalid_reference")
                    if self.r.environment_guard:
                        guard = self.r.environment_guard
                        guard.coordinator.check(
                            guard.key("kernel", source["kernelInstanceId"]), guard.owner
                        )
                    entity, oid = self.s.intent(c, nid, source)
                elif action in {"checkpoint.retry", "checkpoint.continue"}:
                    self.cp.decision_idle(c, nid)
                    failure = self.s.decision(c, nid, args["failure"])
                    if action == "checkpoint.retry":
                        old_op = self.s.get(c, nid, "operation", failure["operationId"])
                        old_details = self.s.details(c, failure["operationId"])
                        entity, oid = self.s.intent(
                            c,
                            nid,
                            failure["source"],
                            retry=args["failure"],
                            stage=old_op["stage"],
                            metadata=old_details["metadata"],
                        )
                    else:
                        entity = self.s.continued(c, nid, args["failure"])["failureId"]
                else:
                    item = self.s.get(
                        c, nid, "checkpoint", args["checkpoint"]["checkpointId"]
                    )
                    if (
                        item["state"] != "ready"
                        or reference(item, "checkpoint") != args["checkpoint"]
                        or item["pinRevision"] != args["expectedPinRevision"]
                    ):
                        fail("reference_stale", 409)
                    entity = item["checkpointId"]
                    item.update(
                        pinned=args["pinned"], pinRevision=item["pinRevision"] + 1
                    )
                    self.s.put(c, nid, "checkpoint", item)
                    hold = "pin-" + entity
                    if args["pinned"]:
                        c.execute(
                            "INSERT OR IGNORE INTO notebook_checkpoint_holds(checkpoint_id,hold_id,kind,reference_id,created_at) VALUES(?,?,?,?,?)",
                            (entity, hold, "pin", entity, time.time()),
                        )
                    else:
                        c.execute(
                            "DELETE FROM notebook_checkpoint_holds WHERE hold_id=? AND kind=?",
                            (hold, "pin"),
                        )
                    self.r.analysis.store.advance(c, nid)
                if not agent:
                    self.r.requests.takeover(c, nid)
                receipt = self.r.analysis.receipt(command, digest, entity, oid)
                receipt["result"]["entityKind"] = (
                    "checkpointRestore"
                    if action == "checkpoint.restore"
                    else "checkpointFailure"
                    if action == "checkpoint.continue"
                    else "checkpoint"
                )
                return self.r.analysis.remember(
                    c, nid, command, digest, receipt, agent=agent
                )

            result = self.r.service._transaction(work)
            self.r.wake()
            return result

    def overview(self, c, nid):
        languages = {}
        for language in ("python", "r"):
            kernel = current_kernel(c, nid, language)
            ops, failures = self.s.blocked(c, nid, language)
            clear = not ops and not failures
            languages[language] = {
                "kernel": kernel_ref(kernel)
                if kernel is not None and kernel["state"] not in {"lost", "released"}
                else None,
                "mutationSeq": self.s.mutation(c, nid, language),
                "activeOperationId": ops[0]["operationId"] if ops else None,
                "blockedFailures": [reference(v, "failure") for v in failures],
                "canRun": clear,
                "canInspect": not ops and all(v["stopConfirmed"] for v in failures),
                "canRelease": clear,
                "reason": "" if clear else "notebook_checkpoint_blocked",
            }
        ready = all(value["canRun"] for value in languages.values())
        return {
            "policyRevision": 1,
            "policy": {
                "automatic": True,
                "stages": [
                    "qc",
                    "normalization",
                    "integration",
                    "clustering",
                    "annotation",
                    "differential_expression",
                ],
                "everyStages": 1,
                "retentionCount": 5,
            },
            "limits": self.cp.limits,
            "usage": self.cp.usage(c),
            "languages": languages,
            "continuationReady": ready,
            "reason": "" if ready else "notebook_checkpoint_blocked",
        }

    def state(self, params):
        try:
            return self._state(params)
        except NotebookError as error:
            self.translate(error)

    def _state(self, params):
        w.fields(params, {"notebookId", "subject"})
        nid, subject = w.identity(params["notebookId"]), params["subject"]
        if subject not in (
            {"kind": "checkpointOverview"},
            {"kind": "checkpointRestoreOverview"},
        ):
            w.fields(subject, {"kind", "id"}, optional={"revision"})
            if subject["kind"] not in SUBJECTS:
                fail("invalid_reference")
            w.identity(subject["id"])
            if "revision" in subject:
                w.number(
                    subject["revision"],
                    1 if subject["kind"] == "checkpointRestoreOperation" else 0,
                )

        def read(c):
            if not c.in_transaction:
                c.execute("BEGIN")
            self.r.fence(c)
            item = (
                self.r.restores.overview(c, nid)
                if subject["kind"] == "checkpointRestoreOverview"
                else self.overview(c, nid)
                if subject["kind"] == "checkpointOverview"
                else self.s.get(
                    c,
                    nid,
                    SUBJECTS[subject["kind"]],
                    subject["id"],
                    subject.get("revision"),
                )
            )
            return bounded(
                {
                    "schemaVersion": 1,
                    "scope": self.r.store_identity,
                    "notebookId": nid,
                    "analysisRevision": self.r.analysis.store.state(c, nid)[
                        "analysisRevision"
                    ],
                    "notebookSeq": OwnerViews.sequence(c, nid),
                    "subject": subject,
                    "item": item,
                }
            )

        return self.r.db.run(read)

    def page(self, params):
        try:
            return self._page(params)
        except NotebookError as error:
            self.translate(error)

    def _page(self, params):
        w.fields(
            params,
            {"notebookId", "collection"},
            optional={"parentId", "parentRevision", "cursor", "limit"},
        )
        nid, collection = w.identity(params["notebookId"]), params["collection"]
        nested = collection in {"checkpointBindings", "checkpointHolds"}
        if collection not in COLLECTIONS and not nested:
            fail("invalid_page")
        if nested != ("parentId" in params and "parentRevision" in params) or (
            not nested and {"parentId", "parentRevision"} & set(params)
        ):
            fail("invalid_page")
        parent, version = params.get("parentId"), params.get("parentRevision")
        if nested:
            w.identity(parent)
            w.number(version, 1)
        limit = params.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 40:
            fail("invalid_page")
        scope = [nid, collection, parent, version]

        def read(c):
            if not c.in_transaction:
                c.execute("BEGIN")
            self.r.fence(c)
            high = c.execute(
                "SELECT COALESCE(MAX(seq),0) FROM notebook_checkpoint_records WHERE notebook_id=?",
                (nid,),
            ).fetchone()[0]
            if collection == "checkpointHolds":
                high = c.execute(
                    "SELECT COALESCE(seq,0) FROM sqlite_sequence WHERE name='notebook_checkpoint_holds'"
                ).fetchone()
                high = high[0] if high else 0
            after = ""
            cursor = params.get("cursor")
            if cursor is not None:
                try:
                    if not isinstance(cursor, str) or len(cursor.encode()) > 1024:
                        raise ValueError
                    data = json.loads(
                        base64.b64decode(cursor, altchars=b"-_", validate=True)
                    )
                    if (
                        not isinstance(data, list)
                        or len(data) != 3
                        or data[0] != scope
                        or type(data[1]) is not int
                        or not 0 <= data[1] <= high
                        or not isinstance(data[2], str)
                    ):
                        raise ValueError
                    high, after = data[1:]
                except (ValueError, TypeError, UnicodeError):
                    fail("invalid_page")
            if nested:
                checkpoint = self.s.get(c, nid, "checkpoint", parent, version)
                if checkpoint["state"] != "ready":
                    fail("invalid_reference")
                if collection == "checkpointBindings":
                    rows = [
                        (v["logicalKey"], v)
                        for v in self.s.details(c, checkpoint["operationId"])[
                            "manifest"
                        ]["bindings"]
                        if v["logicalKey"] > after
                    ]
                    rows.sort(key=lambda v: v[0])
                    rows = rows[: limit + 1]
                else:
                    rows = [
                        (
                            v["hold_id"],
                            {
                                "holdId": v["hold_id"],
                                "kind": v["kind"],
                                "referenceId": v["reference_id"],
                                "createdAt": v["created_at"],
                            },
                        )
                        for v in c.execute(
                            "SELECT * FROM notebook_checkpoint_holds WHERE checkpoint_id=? AND hold_id>? AND seq<=? ORDER BY hold_id LIMIT ?",
                            (parent, after, high, limit + 1),
                        )
                    ]
            else:
                rows = [
                    (v[0], json.loads(v[1]))
                    for v in c.execute(
                        "SELECT entity_id,document FROM notebook_checkpoint_records r WHERE notebook_id=? AND kind=? AND entity_id>? AND seq<=? AND NOT EXISTS(SELECT 1 FROM notebook_checkpoint_gc g WHERE r.kind='checkpoint' AND g.checkpoint_id=r.entity_id) AND revision=(SELECT MAX(x.revision) FROM notebook_checkpoint_records x WHERE x.notebook_id=r.notebook_id AND x.kind=r.kind AND x.entity_id=r.entity_id AND x.seq<=?) ORDER BY entity_id LIMIT ?",
                        (nid, COLLECTIONS[collection], after, high, high, limit + 1),
                    )
                ]
            result = {
                "schemaVersion": 1,
                "scope": self.r.store_identity,
                "notebookId": nid,
                "collection": collection,
                "parentId": parent,
                "parentRevision": version,
                "notebookSeq": OwnerViews.sequence(c, nid),
                "items": [],
                "nextCursor": None,
            }
            for position, item in rows:
                if len(encoded(item)) > 16 * 1024:
                    fail("limit", 413)
                if (
                    len(result["items"]) >= limit
                    or len(encoded(result)) + len(encoded(item)) + 1500 > 192 * 1024
                ):
                    result["nextCursor"] = base64.urlsafe_b64encode(
                        encoded([scope, high, after])
                    ).decode()
                    break
                result["items"].append(item)
                after = position
            return bounded(result, 192 * 1024)

        return self.r.db.run(read)
