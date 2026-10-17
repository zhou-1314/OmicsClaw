"""Versioned scientific references and SQL-bounded views in the Notebook database."""

import base64
import json

from . import analysis_contracts as wire
from .contracts import bounded, encoded
from .store import OwnerViews, bump, current_kernel

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_analysis_receipts (
 command_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_analysis_agent_receipts (
 command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, notebook_id TEXT NOT NULL,
 invocation_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_analysis_collected (execution_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS notebook_analysis_state (
 notebook_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
 active_selection TEXT);
CREATE TABLE IF NOT EXISTS notebook_analysis_entities (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, notebook_id TEXT NOT NULL,
 kind TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL,
 document TEXT NOT NULL CHECK(length(CAST(document AS BLOB))<=16384),
 UNIQUE(notebook_id,kind,entity_id,revision));
CREATE INDEX IF NOT EXISTS notebook_analysis_entity_lookup
 ON notebook_analysis_entities(notebook_id,kind,entity_id,revision DESC);
CREATE TABLE IF NOT EXISTS notebook_analysis_specs (
 notebook_id TEXT NOT NULL, object_id TEXT NOT NULL, document TEXT NOT NULL,
 PRIMARY KEY(notebook_id,object_id));
CREATE TABLE IF NOT EXISTS notebook_analysis_operations (
 execution_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, object_id TEXT NOT NULL,
 command_json TEXT NOT NULL, spec_json TEXT NOT NULL, directory TEXT NOT NULL,
 finished INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notebook_analysis_view_rows (
 notebook_id TEXT NOT NULL, view_id TEXT NOT NULL, view_revision INTEGER NOT NULL,
 row_number INTEGER NOT NULL, identity BLOB NOT NULL, document TEXT NOT NULL
 CHECK(length(CAST(document AS BLOB))<=16384),
 PRIMARY KEY(notebook_id,view_id,view_revision,row_number),
 UNIQUE(notebook_id,view_id,view_revision,identity));
"""


def entity_ref(value, kind):
    return {
        kind + "Id": value[kind + "Id"],
        kind + "Revision": value[kind + "Revision"],
    }


def kernel_ref(row):
    return {
        "language": row["language"],
        "kernelInstanceId": row["instance_id"],
        "generation": row["generation"],
        "environmentId": row["environment_id"],
        "environmentRevision": row["environment_revision"],
    }


class AnalysisStore:
    def __init__(self, runtime):
        self.r = runtime

    @staticmethod
    def state(c, nid):
        row = c.execute(
            "SELECT revision,active_selection FROM notebook_analysis_state WHERE notebook_id=?",
            (nid,),
        ).fetchone()
        return {
            "analysisRevision": row[0] if row else 0,
            "activeSelection": json.loads(row[1]) if row and row[1] else None,
        }

    @staticmethod
    def advance(c, nid, *, active=...):
        c.execute(
            "INSERT INTO notebook_analysis_state(notebook_id,revision) VALUES(?,1) ON CONFLICT(notebook_id) DO UPDATE SET revision=revision+1",
            (nid,),
        )
        if active is not ...:
            c.execute(
                "UPDATE notebook_analysis_state SET active_selection=? WHERE notebook_id=?",
                (encoded(active).decode() if active else None, nid),
            )
        bump(c, nid)

    @staticmethod
    def put(c, nid, kind, value):
        prefix = wire.KINDS[kind]
        version = value[prefix + "Revision"] if kind != "selectionInput" else 1
        raw = encoded(value)
        if len(raw) > wire.MAX_ITEM_BYTES:
            wire.fail("limit", 413)
        c.execute(
            "INSERT INTO notebook_analysis_entities(notebook_id,kind,entity_id,revision,document) VALUES(?,?,?,?,?)",
            (nid, kind, value[prefix + "Id"], version, raw.decode()),
        )

    @staticmethod
    def raw(c, nid, kind, eid, version=None):
        clause = " AND revision=?" if version is not None else ""
        row = c.execute(
            "SELECT document FROM notebook_analysis_entities WHERE notebook_id=? AND kind=? AND entity_id=?"
            + clause
            + " ORDER BY revision DESC LIMIT 1",
            (nid, kind, eid, version) if version is not None else (nid, kind, eid),
        ).fetchone()
        if row is None:
            wire.fail("not_found", 404)
        return json.loads(row[0])

    def get(self, c, nid, kind, eid, version=None):
        value = self.raw(c, nid, kind, eid, version)
        if kind == "object":
            current = self.raw(c, nid, kind, eid)
            kernel = current_kernel(c, nid, value["kernel"]["language"])
            value["registrationState"] = (
                "removed"
                if current.get("registrationState") == "removed"
                else "historical"
                if kernel is None
                or kernel_ref(kernel) != value["kernel"]
                or kernel["state"] not in {"ready", "busy", "reserved"}
                else "active"
            )
            if value["registrationState"] != "active":
                value.update(
                    availability="unavailable",
                    reason="registration_" + value["registrationState"],
                )
            elif current["objectRevision"] != value["objectRevision"]:
                value.update(
                    availability="needs_inspection", reason="object_revision_changed"
                )
        elif kind in {"view", "selection"}:
            obj = self.get(c, nid, "object", value["object"]["objectId"])
            if (
                obj["availability"] != "verified"
                or entity_ref(obj, "object") != value["object"]
            ):
                value.update(valid=False, reason="object_requires_inspection")
        return value

    def validate_object(self, c, nid, reference):
        current = self.get(c, nid, "object", reference["objectId"])
        if (
            entity_ref(current, "object") != reference
            or current["availability"] != "verified"
            or current["registrationState"] != "active"
        ):
            wire.fail("reference_stale", 409)
        return current

    def validate_selection(self, c, nid, reference, *, valid=True):
        value = self.get(
            c,
            nid,
            "selection",
            reference["selectionId"],
            reference["selectionRevision"],
        )
        if valid and not value["valid"]:
            wire.fail("selection_invalid", 409)
        return value

    def active_objects(self, c, nid):
        rows = c.execute(
            "SELECT e.entity_id FROM notebook_analysis_entities e "
            "JOIN notebook_runtime_kernels k ON k.instance_id=json_extract(e.document,'$.kernel.kernelInstanceId') "
            "WHERE e.notebook_id=? AND e.kind='object' AND k.notebook_id=e.notebook_id "
            "AND k.state IN ('ready','busy','reserved') "
            "AND k.generation=(SELECT MAX(g.generation) FROM notebook_runtime_kernels g WHERE g.notebook_id=k.notebook_id AND g.language=k.language) "
            "AND COALESCE(json_extract(e.document,'$.registrationState'),'active')!='removed' "
            "AND e.revision=(SELECT MAX(x.revision) FROM notebook_analysis_entities x WHERE x.notebook_id=e.notebook_id AND x.kind=e.kind AND x.entity_id=e.entity_id) "
            "ORDER BY e.entity_id LIMIT 33",
            (nid,),
        ).fetchall()
        return [
            value
            for row in rows
            if (value := self.get(c, nid, "object", row[0]))["registrationState"]
            == "active"
        ]

    def validate_guard(self, c, nid, guard, *, language=None):
        wire.guard(guard)
        state = self.state(c, nid)
        if guard["analysisRevision"] != state["analysisRevision"]:
            wire.fail("reference_stale", 409)
        for item in guard["objects"]:
            self.validate_object(c, nid, item)
        for item in guard["selections"]:
            selected = self.validate_selection(c, nid, item)
            if selected["object"] not in guard["objects"]:
                wire.fail("invalid_reference")
        if state["activeSelection"]:
            selected = self.validate_selection(c, nid, state["activeSelection"])
            if (
                state["activeSelection"] not in guard["selections"]
                or selected["object"] not in guard["objects"]
            ):
                wire.fail("selection_invalid", 409)
        if language:
            for value in self.active_objects(c, nid):
                if value["kernel"]["language"] == language:
                    self.validate_object(c, nid, entity_ref(value, "object"))
                    if entity_ref(value, "object") not in guard["objects"]:
                        wire.fail("invalid_reference")

    def run_guard(self, c, nid, document):
        guard = document["preconditions"].get("analysis")
        if guard is None:
            if self.state(c, nid)["analysisRevision"]:
                wire.fail("owner_upgrade_required", 409)
            return
        self.validate_guard(c, nid, guard, language=document["language"])

    def context(self, c, nid):
        state = self.state(c, nid)
        values = self.active_objects(c, nid) if nid else []
        refs = [entity_ref(v, "object") for v in values]
        selected = (
            self.validate_selection(c, nid, state["activeSelection"], valid=False)
            if state["activeSelection"]
            else None
        )
        result = {
            **state,
            "objects": [
                {
                    key: item[key]
                    for key in (
                        "objectId",
                        "objectRevision",
                        "kernel",
                        "symbol",
                        "shape",
                        "matrix",
                        "registrationState",
                        "availability",
                        "reason",
                    )
                    if key in item
                }
                for item in values[:8]
            ],
            "objectsTruncated": len(values) > 32,
            "activeSelectionSummary": {
                key: selected[key]
                for key in (
                    "selectionId",
                    "selectionRevision",
                    "object",
                    "count",
                    "membersSha256",
                    "valid",
                    "reason",
                )
                if key in selected
            }
            if selected
            else None,
            "guard": {
                "analysisRevision": state["analysisRevision"],
                "objects": refs,
                "selections": [state["activeSelection"]] if selected else [],
            },
        }
        result["guards"] = {}
        for language in ("python", "r"):
            chosen = [
                entity_ref(item, "object")
                for item in values
                if item["kernel"]["language"] == language
            ]
            if selected and selected["object"] not in chosen:
                chosen.append(selected["object"])
            result["guards"][language] = {
                "analysisRevision": state["analysisRevision"],
                "objects": chosen,
                "selections": [state["activeSelection"]] if selected else [],
            }
        while len(encoded(result)) > 8192 and result["objects"]:
            result["objects"].pop()
        result["objectsTruncated"] = len(result["objects"]) < len(values)
        return result

    def invalidate(self, c, nid, language, reason):
        changed = False
        for value in self.active_objects(c, nid):
            if value["kernel"]["language"] != language:
                continue
            value.update(
                objectRevision=value["objectRevision"] + 1,
                availability="needs_inspection",
                reason=reason,
                defaultView=None,
            )
            self.put(c, nid, "object", value)
            changed = True
        if changed:
            self.advance(c, nid)

    def run_context(self, c, nid):
        """Read current execution references without loading historical catalogues."""

        def brief(value, keys):
            return {
                **{key: value[key] for key in keys},
                "reason": str(value.get("reason", ""))
                .encode()[:128]
                .decode(errors="ignore"),
            }

        object_keys = (
            "objectId",
            "objectRevision",
            "kernel",
            "registrationState",
            "availability",
        )
        objects = self.active_objects(c, nid)
        if len(objects) > 32:
            wire.fail("limit", 413)
        selected_ref = self.state(c, nid)["activeSelection"]
        selected = (
            self.validate_selection(c, nid, selected_ref, valid=False)
            if selected_ref
            else None
        )
        source = (
            self.get(
                c,
                nid,
                "object",
                selected["object"]["objectId"],
                selected["object"]["objectRevision"],
            )
            if selected
            else None
        )
        return {
            "kernels": [
                kernel_ref(kernel)
                for language in ("python", "r")
                if (kernel := current_kernel(c, nid, language)) is not None
                and kernel["state"] in {"ready", "busy", "reserved"}
            ],
            "objects": [brief(value, object_keys) for value in objects],
            "activeSelection": brief(
                selected, ("selectionId", "selectionRevision", "object", "valid")
            )
            if selected
            else None,
            "selectionObject": brief(source, object_keys) if source else None,
        }

    def state_response(self, params):
        if isinstance(params, dict) and isinstance(params.get("subject"), dict) and str(params["subject"].get("kind", "")).startswith("checkpoint"):
            return self.r.checkpoints.wire.state(params)
        wire.fields(params, {"notebookId"}, optional={"subject"})
        nid = wire.identity(params["notebookId"])
        subject = params.get("subject")
        is_run = subject == {"kind": "runContext"}
        if subject is not None and not is_run:
            wire.fields(subject, {"kind", "id"}, optional={"revision"})
            if subject["kind"] not in wire.KINDS:
                wire.fail("invalid_reference")
            wire.identity(subject["id"])
            if "revision" in subject:
                wire.number(subject["revision"], 1)

        def read(c):
            if is_run and not c.in_transaction:
                c.execute("BEGIN")
            self.r.fence(c)
            result = {
                "schemaVersion": 1,
                "scope": self.r.store_identity,
                "notebookId": nid,
                **self.state(c, nid),
                "notebookSeq": OwnerViews.sequence(c, nid),
            }
            if subject is not None:
                result.pop("activeSelection")
                result.update(
                    subject=subject,
                    item=self.run_context(c, nid)
                    if is_run
                    else self.get(
                        c, nid, subject["kind"], subject["id"], subject.get("revision")
                    ),
                )
            else:
                result["counts"] = {
                    collection: c.execute(
                        "SELECT COUNT(DISTINCT entity_id) FROM notebook_analysis_entities WHERE notebook_id=? AND kind=?",
                        (nid, kind),
                    ).fetchone()[0]
                    for collection, kind in wire.COLLECTIONS.items()
                }
            return bounded(result, 32 * 1024) if is_run else bounded(result)

        return self.r.db.run(read)

    def page(self, params):
        if isinstance(params, dict) and str(params.get("collection", "")).startswith("checkpoint"):
            return self.r.checkpoints.wire.page(params)
        wire.fields(
            params,
            {"notebookId", "collection"},
            optional={"parentId", "viewRevision", "cursor", "limit"},
        )
        nid, collection = wire.identity(params["notebookId"]), params["collection"]
        if collection not in {*wire.COLLECTIONS, "viewRows"}:
            wire.fail("invalid_page")
        is_rows = collection == "viewRows"
        if is_rows != ("parentId" in params and "viewRevision" in params) or (
            not is_rows and {"parentId", "viewRevision"} & set(params)
        ):
            wire.fail("invalid_page")
        parent, version = params.get("parentId"), params.get("viewRevision")
        if is_rows:
            wire.identity(parent)
            wire.number(version, 1)
        limit = params.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 40:
            wire.fail("invalid_page")
        cursor = params.get("cursor")
        scope = [nid, collection, parent, version]

        def read(c):
            high = c.execute(
                "SELECT COALESCE(MAX(seq),0) FROM notebook_analysis_entities WHERE notebook_id=?",
                (nid,),
            ).fetchone()[0]
            after = -1 if is_rows else ""
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
                    ):
                        raise ValueError
                    high, after = data[1:]
                    if is_rows and (type(after) is not int or after < -1):
                        raise ValueError
                    if not is_rows and not isinstance(after, str):
                        raise ValueError
                except (ValueError, TypeError, UnicodeError):
                    wire.fail("invalid_page")
            if is_rows:
                self.get(c, nid, "view", parent, version)
                rows = c.execute(
                    "SELECT row_number AS position,document FROM notebook_analysis_view_rows WHERE notebook_id=? AND view_id=? AND view_revision=? AND row_number>? ORDER BY row_number LIMIT ?",
                    (nid, parent, version, after, limit + 1),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT e.entity_id AS position,e.document FROM notebook_analysis_entities e WHERE notebook_id=? AND kind=? AND entity_id>? AND seq<=? AND revision=(SELECT MAX(x.revision) FROM notebook_analysis_entities x WHERE x.notebook_id=e.notebook_id AND x.kind=e.kind AND x.entity_id=e.entity_id AND x.seq<=?) ORDER BY entity_id LIMIT ?",
                    (nid, wire.COLLECTIONS[collection], after, high, high, limit + 1),
                ).fetchall()
            result = {
                "schemaVersion": 1,
                "scope": self.r.store_identity,
                "notebookId": nid,
                "collection": collection,
                "parentId": parent,
                "items": [],
                "nextCursor": None,
                "notebookSeq": OwnerViews.sequence(c, nid),
            }
            for row in rows:
                item = json.loads(row["document"])
                if not is_rows:
                    kind = wire.COLLECTIONS[collection]
                    item = self.get(
                        c,
                        nid,
                        kind,
                        item[wire.KINDS[kind] + "Id"],
                        item.get(wire.KINDS[kind] + "Revision"),
                    )
                if (
                    len(result["items"]) >= limit
                    or len(encoded(result)) + len(encoded(item)) + 1500
                    > wire.MAX_PAGE_BYTES
                ):
                    result["nextCursor"] = base64.urlsafe_b64encode(
                        encoded([scope, high, after])
                    ).decode()
                    break
                result["items"].append(item)
                after = row["position"]
            return bounded(result, wire.MAX_PAGE_BYTES)

        return self.r.db.run(read)
