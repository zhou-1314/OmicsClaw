"""Authenticated user edits, continuation handoff and command receipt queries."""

import hashlib
import json
from dataclasses import asdict, replace

from omicsclaw.memory.notebook import NotebookCell

from .contracts import MAX_SOURCE_BYTES, bounded, encoded
from .service import NotebookError, identifier, revision
from .store import (
    OwnerViews,
    bump,
    current_kernel,
    kernel_row,
    kernel_set,
    require_kernel,
)


class OwnerActions:
    def __init__(self, runtime):
        self.r = runtime

    def binding(self, params):
        if set(params) != {"sessionId"}:
            raise NotebookError("invalid_notebook_command")
        sid = identifier(params["sessionId"])

        def read(c):
            facts, binding, kernel, selection = self.r.requests.facts(
                c, sid, "binding-read"
            )
            return {
                "binding": dict(binding) if binding else None,
                "kernel": kernel_row(kernel),
                "control": {
                    k: facts[k]
                    for k in (
                        "stateSeq",
                        "userWatermark",
                        "controlMode",
                        "helperRevision",
                    )
                },
                "notebookSeq": OwnerViews.sequence(c, binding["notebook_id"])
                if binding
                else 0,
                "selection": selection,
                "kernels": kernel_set(c, binding["notebook_id"])
                if binding
                else {"python": None, "r": None},
                "selections": {
                    language: self.r.requests.selection(
                        c,
                        current_kernel(c, binding["notebook_id"], language)
                        if binding
                        else None,
                        language,
                    )
                    for language in ("python", "r")
                },
                **(
                    self.r.requests.readiness(c, binding["notebook_id"])
                    if binding
                    else {"continuationReady": True, "continuationBlockers": []}
                ),
            }

        return bounded(self.r.db.run(read))

    def receipt(self, params):
        if not {"commandId"} <= set(params) <= {"commandId", "notebookId"}:
            raise NotebookError("invalid_notebook_command")
        command_id = identifier(params["commandId"])
        nid = identifier(params["notebookId"]) if "notebookId" in params else None

        def read(c):
            old = c.execute(
                "SELECT receipt_json FROM notebook_runtime_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if old:
                result = json.loads(old[0])
                if result.get("kind") == "analysis":
                    owner = c.execute(
                        "SELECT notebook_id FROM notebook_analysis_receipts WHERE command_id=?",
                        (command_id,),
                    ).fetchone()
                    if nid is not None and (owner is None or owner[0] != nid):
                        return {"found": False, "receipt": None}
                    return {"found": True, "receipt": result}
                if (
                    nid
                    and result.get(
                        "notebookId", (result.get("binding") or {}).get("notebook_id")
                    )
                    != nid
                ):
                    return {"found": False, "receipt": None}
                if result.get("kind") == "resume":
                    row = c.execute(
                        "SELECT prepared_request FROM notebook_request_resumes WHERE command_id=?",
                        (command_id,),
                    ).fetchone()
                    result["continuationPrepared"] = bool(row and row[0])
                    result.update(self.r.requests.readiness(c, result["notebookId"]))
                return {"found": True, "receipt": result}
            if nid is None:
                return {"found": False, "receipt": None}
            row = c.execute(
                self.r.views._execution_select()
                + "WHERE e.notebook_id=? AND e.client_command_id=?",
                (nid, command_id),
            ).fetchone()
            return {
                "found": bool(row),
                "receipt": self.r.views._record(row) if row else None,
            }

        return bounded(self.r.db.run(read))

    def changes(self, params):
        if set(params) != {"notebookId", "afterSeq"}:
            raise NotebookError("invalid_notebook_command")
        nid, after = identifier(params["notebookId"]), revision(params["afterSeq"])
        current = self.r.db.run(lambda c: OwnerViews.sequence(c, nid))
        return {
            "notebookSeq": current,
            "changed": after != current,
            "gap": after != current,
        }

    def cell(self, params):
        if set(params) != {"notebookId", "cellId"}:
            raise NotebookError("invalid_notebook_command")
        nid, cid = identifier(params["notebookId"]), identifier(params["cellId"])

        def read(c):
            row = c.execute(
                "SELECT notebook_id,cell_id,language,revision,status,trust_label,session_epoch,client_command_id,execution_id,updated_at,created_at,cell_index,"
                "CASE WHEN length(CAST(source AS BLOB))<=65536 THEN source ELSE '' END AS source,"
                "CASE WHEN length(CAST(source AS BLOB))>65536 THEN 1 ELSE 0 END AS sourceTruncated,"
                "CASE WHEN length(CAST(output_json AS BLOB))<=16384 THEN output_json ELSE '{\"truncated\":true}' END AS output_json,"
                "'{}' AS provenance_json FROM notebook_cells WHERE notebook_id=? AND cell_id=?",
                (nid, cid),
            ).fetchone()
            if row is None:
                raise NotebookError("execution_not_found", 404)
            return OwnerViews._cell(row)

        return bounded(self.r.db.run(read))

    async def save(self, params):
        if set(params) - {"language"} != {
            "notebookId",
            "cellId",
            "source",
            "expectedRevision",
            "kernelInstanceId",
            "generation",
            "commandId",
        }:
            raise NotebookError("invalid_notebook_command")
        language = params.get("language", "python")
        if language not in {"python", "r"}:
            raise NotebookError("invalid_notebook_command")
        for key in ("notebookId", "cellId", "kernelInstanceId", "commandId"):
            identifier(params[key])
        revision(params["expectedRevision"])
        revision(params["generation"])
        if (
            not isinstance(params["source"], str)
            or len(encoded(params["source"])) > MAX_SOURCE_BYTES
        ):
            raise NotebookError("invalid_notebook_command")
        async with self.r.operation_lock:

            def write(c):
                self.r.authorize()
                self.r.fence(c)
                digest, old = self.r._receipt(c, params["commandId"], params)
                if old:
                    return old
                nid, cid = params["notebookId"], params["cellId"]
                require_kernel(
                    c, nid, params["kernelInstanceId"], params["generation"], language
                )
                cell = self.r.service.store._get_cell(c, nid, cid) or NotebookCell(
                    nid, cid
                )
                if cell.revision != params["expectedRevision"]:
                    raise NotebookError("revision_conflict", 409)
                # Execution keeps its immutable code even when the editable cell changes.
                saved = self.r.service.store._put_cell(
                    c,
                    replace(cell, source=params["source"], language=language),
                    params["expectedRevision"],
                )
                self.r.requests.takeover(c, nid)
                result = {
                    "kind": "save",
                    "notebookId": nid,
                    "cell": asdict(saved),
                    "control": self.r.requests.control(c, nid),
                    "notebookSeq": OwnerViews.sequence(c, nid),
                }
                c.execute(
                    "INSERT INTO notebook_runtime_commands VALUES(?,?,?)",
                    (params["commandId"], digest, encoded(result).decode()),
                )
                return result

            return bounded(self.r.service._transaction(write))

    async def resume(self, params):
        base = {"notebookId", "commandId", "userWatermark"}
        if set(params) not in (
            base | {"kernelIdentities"},
            base | {"kernelInstanceId", "generation"},
        ):
            raise NotebookError("invalid_notebook_command")
        for key in ("notebookId", "commandId"):
            identifier(params[key])
        revision(params["userWatermark"])
        identities = params.get("kernelIdentities")
        if identities is None:
            identities = {
                "python": {
                    "kernelInstanceId": params["kernelInstanceId"],
                    "generation": params["generation"],
                }
            }
        if not isinstance(identities, dict) or not set(identities) <= {"python", "r"}:
            raise NotebookError("invalid_notebook_command")
        for identity in identities.values():
            if not isinstance(identity, dict) or set(identity) != {
                "kernelInstanceId",
                "generation",
            }:
                raise NotebookError("invalid_notebook_command")
            identifier(identity["kernelInstanceId"])
            revision(identity["generation"])
        async with self.r.operation_lock:

            def write(c):
                self.r.authorize()
                self.r.fence(c)
                digest, old = self.r._receipt(c, params["commandId"], params)
                if old:
                    return old
                nid = params["notebookId"]
                for language in ("python", "r"):
                    self.r.restores.require_clear(c, nid, language)
                    self.r.checkpoints.store.require_clear(c, nid, language)
                current = {
                    language: kernel
                    for language, kernel in kernel_set(c, nid).items()
                    if kernel and kernel["state"] not in {"released", "lost"}
                }
                if set(current) != set(identities):
                    raise NotebookError("notebook_generation_changed", 409)
                for language, identity in identities.items():
                    require_kernel(
                        c,
                        nid,
                        identity["kernelInstanceId"],
                        identity["generation"],
                        language,
                    )
                control = self.r.requests.control(c, nid)
                if control["userWatermark"] != params["userWatermark"]:
                    raise NotebookError("notebook_request_stale", 409)
                sid = c.execute(
                    "SELECT session_id FROM notebook_bindings WHERE notebook_id=?",
                    (nid,),
                ).fetchone()[0]
                source = hashlib.sha256(
                    ("resume:" + sid + ":" + params["commandId"]).encode()
                ).hexdigest()[:32]
                c.execute(
                    "INSERT INTO notebook_request_control(notebook_id) VALUES(?) ON CONFLICT(notebook_id) DO UPDATE SET control_mode='agent'",
                    (nid,),
                )
                c.execute(
                    "INSERT INTO notebook_request_resumes VALUES(?,?,?,?,NULL)",
                    (params["commandId"], nid, sid, source),
                )
                bump(c, nid)
                result = {
                    "kind": "resume",
                    "notebookId": nid,
                    "sessionId": sid,
                    "sourceRequestId": source,
                    "control": self.r.requests.control(c, nid),
                    "notebookSeq": OwnerViews.sequence(c, nid),
                }
                c.execute(
                    "INSERT INTO notebook_runtime_commands VALUES(?,?,?)",
                    (params["commandId"], digest, encoded(result).decode()),
                )
                return result

            self.r.service._transaction(write)
            return self.receipt(
                {"commandId": params["commandId"], "notebookId": params["notebookId"]}
            )["receipt"]
