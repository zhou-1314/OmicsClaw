"""Owner-issued request snapshots and durable Notebook control state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

from .contracts import bounded, encoded
from .service import NotebookError, identifier
from .store import (
    OwnerViews,
    bump,
    current_kernel,
    kernel_row,
    kernel_set,
    next_generation,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_request_sessions (
 session_id TEXT PRIMARY KEY, helper_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notebook_request_control (
 notebook_id TEXT PRIMARY KEY, state_seq INTEGER NOT NULL DEFAULT 0,
 user_watermark INTEGER NOT NULL DEFAULT 0, control_mode TEXT NOT NULL DEFAULT 'agent');
CREATE TABLE IF NOT EXISTS notebook_request_outcomes (
 execution_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_request_default (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), environment_id TEXT,
 environment_revision INTEGER, revision INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO notebook_request_default VALUES(1,NULL,NULL,0);
CREATE TABLE IF NOT EXISTS notebook_language_defaults (
 language TEXT PRIMARY KEY, environment_id TEXT, environment_revision INTEGER,
 revision INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO notebook_language_defaults SELECT 'python',environment_id,environment_revision,revision FROM notebook_request_default;
INSERT OR IGNORE INTO notebook_language_defaults VALUES('r',NULL,NULL,0);
CREATE TABLE IF NOT EXISTS notebook_component_provenance (
 kind TEXT NOT NULL, record_id TEXT NOT NULL, source_digest TEXT NOT NULL,
 PRIMARY KEY(kind,record_id));
CREATE TABLE IF NOT EXISTS notebook_request_resumes (
 command_id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, session_id TEXT NOT NULL,
 source_request_id TEXT NOT NULL, prepared_request TEXT);
"""


class RequestState:
    """Tokens attest a particular observation; the permission gate still authorizes code."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.secret = secrets.token_bytes(32)

    @staticmethod
    def control(c, nid):
        row = c.execute(
            "SELECT * FROM notebook_request_control WHERE notebook_id=?",
            (nid,),
        ).fetchone()
        return {
            "stateSeq": row["state_seq"] if row else 0,
            "userWatermark": row["user_watermark"] if row else 0,
            "controlMode": row["control_mode"] if row else "agent",
        }

    @staticmethod
    def eligible(row, language="python"):
        return bool(
            row["spec"]["language"] == language
            and row["status"] == "ready"
            and row["revision"] > 0
            and row["acceptedRevision"] == row["revision"]
            and (row["observation"] or {})
            .get("packages", {})
            .get("ipykernel" if language == "python" else "IRkernel")
            and not (row["observation"] or {}).get("missing")
        )

    def selection(self, c, kernel, language="python"):
        default = c.execute(
            "SELECT * FROM notebook_language_defaults WHERE language=?", (language,)
        ).fetchone()
        fixed = kernel is not None or default["environment_id"] is not None
        revision = (
            kernel["environment_revision"]
            if kernel
            else default["environment_revision"]
        )
        environment_id = (
            kernel["environment_id"] if kernel else default["environment_id"]
        )
        rows = []
        if fixed:
            try:
                row = self.runtime.registry.get(environment_id)
            except ValueError:
                row = None
            if (
                row is not None
                and self.eligible(row, language)
                and row["revision"] == revision
            ):
                rows = [row]
            reason = "notebook_environment_invalid" if not rows else ""
        else:
            ids = self.runtime.registry_db.run(
                lambda registry: registry.execute(
                    "SELECT environment_id FROM registered_environments ORDER BY environment_id LIMIT 65"
                ).fetchall()
            )
            for item in ids:
                row = self.runtime.registry.get(item[0])
                if self.eligible(row, language):
                    rows.append(row)
            reason = (
                "notebook_environment_required"
                if not rows
                else "notebook_environment_ambiguous"
                if len(rows) > 1
                else ""
            )
        selected = rows[0] if not reason else None
        return {
            "reason": reason,
            "environmentId": selected["environmentId"] if selected else environment_id,
            "environmentRevision": selected["revision"] if selected else revision,
            "selectionRevision": default["revision"] if kernel is None else None,
            "candidates": [
                {
                    "environmentId": row["environmentId"],
                    "revision": row["revision"],
                    "executable": row["spec"]["executable"][:256],
                }
                for row in rows[:16]
            ],
        }

    def readiness(self, c, nid):
        blockers = []
        for language in ("python", "r"):
            if any(
                self.runtime.checkpoints.store.blocked(c, nid, language)
            ) or self.runtime.restores.active(c, nid, language):
                blockers.append(
                    {
                        "reason": "notebook_checkpoint_blocked",
                        "language": language,
                        "kernelInstanceId": None,
                    }
                )
        kernels = kernel_set(c, nid)
        for language, kernel in kernels.items():
            if kernel is None or kernel["state"] in {"released", "lost"}:
                continue
            if kernel["state"] not in {"ready", "reserved", "busy"}:
                blockers.append(
                    {
                        "reason": "notebook_kernel_unavailable",
                        "language": language,
                        "kernelInstanceId": kernel["kernelInstanceId"],
                    }
                )
            for row in c.execute(
                "SELECT e.execution_id,e.status FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) WHERE e.notebook_id=? AND x.instance_id=? AND (e.status IN ('queued','running','outcome_unknown') OR json_extract(e.output_json,'$.event_stream_pending')=1) LIMIT 17",
                (nid, kernel["kernelInstanceId"]),
            ):
                blockers.append(
                    {
                        "reason": row["status"],
                        "language": language,
                        "executionId": row["execution_id"],
                    }
                )
        if self.control(c, nid)["controlMode"] == "user":
            blockers.append({"reason": "notebook_user_control"})
        return {"continuationReady": not blockers, "continuationBlockers": blockers}

    def facts(self, c, sid, request_id):
        self.runtime.fence(c)
        binding = c.execute(
            "SELECT * FROM notebook_bindings WHERE session_id=?", (sid,)
        ).fetchone()
        kernel = current_kernel(c, binding["notebook_id"]) if binding else None
        helper = c.execute(
            "SELECT helper_revision FROM notebook_request_sessions WHERE session_id=?",
            (sid,),
        ).fetchone()
        selection = self.selection(c, kernel)
        nid = binding["notebook_id"] if binding else None
        facts = {
            "version": 1,
            "ownerId": self.runtime.owner_id,
            "ownerEpoch": self.runtime.epoch,
            "store": self.runtime.store_identity,
            "sessionId": sid,
            "requestId": request_id,
            "notebookId": nid,
            "kernelInstanceId": kernel["instance_id"] if kernel else None,
            "generation": kernel["generation"] if kernel else 0,
            "helperRevision": helper[0] if helper else 0,
            **self.control(c, nid),
            **{
                k: selection[k]
                for k in (
                    "reason",
                    "environmentId",
                    "environmentRevision",
                    "selectionRevision",
                )
            },
        }
        facts["nextGeneration"] = next_generation(c, nid) if nid else 1
        facts["analysisRevision"] = self.runtime.analysis.store.state(c, nid)[
            "analysisRevision"
        ]
        facts["languages"] = {}
        for language in ("python", "r"):
            chosen = current_kernel(c, nid, language) if nid else None
            selected = self.selection(c, chosen, language)
            facts["languages"][language] = {
                "kernelInstanceId": chosen["instance_id"] if chosen else None,
                "generation": chosen["generation"] if chosen else 0,
                "state": chosen["state"] if chosen else None,
                **{
                    k: selected[k]
                    for k in (
                        "reason",
                        "environmentId",
                        "environmentRevision",
                        "selectionRevision",
                    )
                },
            }
        return facts, binding, kernel, selection

    def snapshot(self, c, sid, request_id):
        identifier(sid)
        identifier(request_id)
        facts, binding, kernel, selection = self.facts(c, sid, request_id)
        nid = facts["notebookId"]
        recent = []
        active = None
        if nid:
            # SQL bounds precede decoding all user-controlled source/output.
            recent = [
                dict(row)
                for row in c.execute(
                    "SELECT cell_id,language,execution_id,status,revision,substr(source,1,1024) AS sourcePreview,"
                    "substr(json_extract(output_json,'$.stdout'),1,1024) AS stdoutPreview,"
                    "substr(json_extract(output_json,'$.error'),1,512) AS errorPreview "
                    "FROM notebook_cells WHERE notebook_id=? ORDER BY updated_at DESC,cell_id LIMIT 6",
                    (nid,),
                ).fetchall()
            ]
            active = c.execute(
                "SELECT e.execution_id,e.status FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE e.notebook_id=? AND k.state NOT IN ('released','lost') AND k.generation=(SELECT MAX(k2.generation) FROM notebook_runtime_kernels k2 WHERE k2.notebook_id=k.notebook_id AND k2.language=k.language) AND (e.status IN ('queued','running','outcome_unknown') OR json_extract(e.output_json,'$.event_stream_pending')=1) ORDER BY e.created_at LIMIT 1",
                (nid,),
            ).fetchone()
        summary = {
            "binding": dict(binding) if binding else None,
            "kernel": {
                k: v
                for k, v in (kernel_row(kernel) or {}).items()
                if k
                in {
                    "kernelInstanceId",
                    "generation",
                    "environmentId",
                    "environmentRevision",
                    "language",
                    "state",
                    "environmentFingerprint",
                }
            }
            if kernel
            else None,
            "selection": selection,
            "kernels": kernel_set(c, nid),
            "selections": {
                language: self.selection(c, current_kernel(c, nid, language), language)
                for language in ("python", "r")
            },
            "nextGeneration": facts["nextGeneration"],
            **self.readiness(c, nid),
            "state": {
                k: facts[k]
                for k in ("stateSeq", "userWatermark", "controlMode", "helperRevision")
            },
            "notebookSeq": OwnerViews.sequence(c, nid) if nid else 0,
            "activeExecution": dict(active) if active else None,
            "recentCells": recent,
            "previewsMayBeTruncated": True,
            "analysis": self.runtime.analysis.store.context(c, nid),
            "checkpoints": self.runtime.checkpoints.context(c, nid),
        }
        while len(encoded(summary)) > 16000 and summary["recentCells"]:
            summary["recentCells"].pop()
        if summary["continuationReady"]:
            c.execute(
                "UPDATE notebook_request_resumes SET prepared_request=? WHERE session_id=? AND prepared_request IS NULL",
                (request_id, sid),
            )
        # The token is returned only to the trusted controller, never in public snapshots.
        payload = encoded(facts)
        token = (
            base64.urlsafe_b64encode(payload).decode()
            + "."
            + hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        )
        return bounded({"summary": summary, "token": token}, 24000)

    def decode(self, token, sid, request_id):
        if not isinstance(token, str) or len(token) > 8192:
            raise NotebookError("notebook_request_invalid", 409)
        try:
            raw, signature = token.split(".")
            payload = base64.b64decode(raw, altchars=b"-_", validate=True)
            if not hmac.compare_digest(
                hmac.new(self.secret, payload, hashlib.sha256).hexdigest(), signature
            ):
                raise ValueError("signature")
            facts = json.loads(payload)
            if facts["sessionId"] != sid or facts["requestId"] != request_id:
                raise ValueError("identity")
        except (ValueError, KeyError, TypeError):
            raise NotebookError("notebook_request_invalid", 409) from None
        return facts

    def verify(self, c, token, sid, request_id, *, executable=True, language="python"):
        old = self.decode(token, sid, request_id)
        current, _, kernel, _ = self.facts(c, sid, request_id)
        if current["controlMode"] != "agent":
            raise NotebookError("notebook_user_control", 409)
        if current != old:
            raise NotebookError("notebook_request_stale", 409)
        selected = current["languages"][language]
        kernel = current_kernel(c, current["notebookId"], language)
        if executable:
            self.runtime.restores.require_clear(c, current["notebookId"], language)
            self.runtime.checkpoints.store.require_clear(
                c, current["notebookId"], language
            )
            analysis = self.runtime.analysis.store.context(c, current["notebookId"])
            self.runtime.analysis.store.validate_guard(
                c,
                current["notebookId"],
                analysis["guards"][language],
                language=language,
            )
            if selected["reason"]:
                raise NotebookError(selected["reason"], 409)
            if kernel and kernel["state"] not in {"reserved", "ready", "busy"}:
                raise NotebookError("notebook_kernel_unavailable", 409)
            if current["notebookId"]:
                self.runtime.idle(c, current["notebookId"])
        return {**current, **selected}

    @staticmethod
    def changed(c, nid, eid):
        fresh = c.execute(
            "INSERT OR IGNORE INTO notebook_request_outcomes VALUES(?,?)", (eid, nid)
        ).rowcount
        if fresh:
            c.execute(
                "INSERT INTO notebook_request_control(notebook_id,state_seq) VALUES(?,1) "
                "ON CONFLICT(notebook_id) DO UPDATE SET state_seq=state_seq+1",
                (nid,),
            )

    @staticmethod
    def takeover(c, nid):
        c.execute(
            "INSERT INTO notebook_request_control(notebook_id,user_watermark,control_mode) VALUES(?,1,'user') "
            "ON CONFLICT(notebook_id) DO UPDATE SET user_watermark=user_watermark+1,control_mode='user'",
            (nid,),
        )
        bump(c, nid)

    def provenance(self, c, kind, record_id):
        c.execute(
            "INSERT INTO notebook_component_provenance VALUES(?,?,?)",
            (kind, record_id, self.runtime.source_digest),
        )
