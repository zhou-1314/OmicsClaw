"""Persist exact skill sources and inject approved definitions before their cells."""

import hashlib
import json

from omicsclaw.skills.sidecar import extract_kernel_exports, validate_kernel_ast

from .contracts import encoded
from .service import NotebookError, identifier
from .store import bump

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_helpers (
 session_id TEXT NOT NULL, skill TEXT NOT NULL, source_hash TEXT NOT NULL,
 body_hash TEXT NOT NULL, source TEXT NOT NULL, body TEXT NOT NULL,
 kernel_path TEXT NOT NULL, body_path TEXT NOT NULL, component_digest TEXT NOT NULL,
 PRIMARY KEY(session_id,skill));
CREATE TABLE IF NOT EXISTS notebook_execution_helpers (
 execution_id TEXT PRIMARY KEY, bundle_json TEXT NOT NULL, bundle_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notebook_helper_injections (
 instance_id TEXT NOT NULL, bundle_hash TEXT NOT NULL, PRIMARY KEY(instance_id,bundle_hash));
CREATE TABLE IF NOT EXISTS notebook_helper_registrations (
 session_id TEXT NOT NULL, skill TEXT NOT NULL, source_hash TEXT NOT NULL,
 body_hash TEXT NOT NULL, registration_json TEXT NOT NULL,
 PRIMARY KEY(session_id,skill,source_hash,body_hash));
"""


def bootstrap(bundle):
    """The loader closes over fixed modules and never reads a mutable checkout."""
    payload = json.dumps(
        [
            {k: entry[k] for k in ("skill", "sourceHash", "source", "kernelPath")}
            for entry in bundle
        ],
        ensure_ascii=True,
    )
    return f"""def __omicsclaw_register_helpers():
    import json, types, re
    entries = json.loads({payload!r})
    g = globals()
    previous = g.get('__omicsclaw_registered_helpers', {{}})
    pending = {{}}
    updates = {{}}
    for entry in entries:
        name, source_hash = entry['skill'], entry['sourceHash']
        old = previous.get(name)
        if old is not None and old[0] == source_hash:
            pending[name] = old
            continue
        module = types.ModuleType('omicsclaw_skill_' + re.sub(r'[^A-Za-z0-9_]', '_', name))
        module.__file__ = entry['kernelPath']
        exec(compile(entry['source'], entry['kernelPath'], 'exec'), module.__dict__)
        for exported in module.__all__:
            value = getattr(module, exported)
            prefixed = re.sub(r'[^A-Za-z0-9_]', '_', name) + '__' + exported
            for key in (exported, prefixed):
                if key in g and not (old is not None and g[key] is getattr(old[1], exported, None)):
                    raise RuntimeError('helper name conflicts with existing namespace: ' + key)
                if key in updates:
                    raise RuntimeError('helper names conflict: ' + key)
                updates[key] = value
        pending[name] = (source_hash, module)
    library = {{**previous, **pending}}
    old_loader = g.get('__omicsclaw_fixed_helper_loader')
    if 'load_skill' in g and g['load_skill'] is not old_loader:
        raise RuntimeError('helper name conflicts with existing namespace: load_skill')
    def load_skill(name):
        if name not in library:
            raise LookupError('Call use_skill to register this fixed source first: ' + name)
        return library[name][1]
    g.update(updates)
    g['__omicsclaw_registered_helpers'] = library
    g['__omicsclaw_fixed_helper_loader'] = load_skill
    g['load_skill'] = load_skill
__omicsclaw_register_helpers()
del __omicsclaw_register_helpers
"""


class OwnerHelpers:
    def __init__(self, runtime):
        self.r = runtime

    async def register(self, params):
        fields = {
            "sessionId",
            "skill",
            "source",
            "body",
            "kernelPath",
            "bodyPath",
            "componentDigest",
        }
        if set(params) not in (fields, fields | {"registration"}) or any(
            not isinstance(params[k], str) for k in fields
        ):
            raise NotebookError("notebook_helper_invalid")
        registration = params.get("registration", {"kind": "direct"})
        if not isinstance(registration, dict) or len(encoded(registration)) > 2048:
            raise NotebookError("notebook_helper_invalid")
        sid, skill = identifier(params["sessionId"]), identifier(params["skill"])
        source, body = params["source"], params["body"]
        if (
            len(source.encode()) > 16384
            or len(body.encode()) > 128 * 1024
            or any(
                len(params[k]) > 2048
                for k in ("kernelPath", "bodyPath", "componentDigest")
            )
            or validate_kernel_ast(source)
        ):
            raise NotebookError("notebook_helper_invalid")
        exports = extract_kernel_exports(source)
        if not exports:
            raise NotebookError("notebook_helper_invalid")
        source_hash, body_hash = (
            hashlib.sha256(source.encode()).hexdigest(),
            hashlib.sha256(body.encode()).hexdigest(),
        )
        async with self.r.operation_lock:

            def write(c):
                self.r.authorize()
                self.r.fence(c)
                old = c.execute(
                    "SELECT source_hash,body_hash FROM notebook_helpers WHERE session_id=? AND skill=?",
                    (sid, skill),
                ).fetchone()
                changed = old is None or tuple(old) != (source_hash, body_hash)
                size = c.execute(
                    "SELECT COALESCE(SUM(length(CAST(source AS BLOB))+length(CAST(body AS BLOB))),0) FROM notebook_helpers WHERE session_id=? AND skill!=?",
                    (sid, skill),
                ).fetchone()[0]
                if size + len(source.encode()) + len(body.encode()) > 512 * 1024:
                    raise NotebookError("notebook_resource_limit", 409)
                if changed:
                    c.execute(
                        "INSERT OR IGNORE INTO notebook_helper_registrations VALUES(?,?,?,?,?)",
                        (
                            sid,
                            skill,
                            source_hash,
                            body_hash,
                            encoded(registration).decode(),
                        ),
                    )
                    c.execute(
                        "INSERT INTO notebook_helpers VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,skill) DO UPDATE SET "
                        "source_hash=excluded.source_hash,body_hash=excluded.body_hash,source=excluded.source,body=excluded.body,"
                        "kernel_path=excluded.kernel_path,body_path=excluded.body_path,component_digest=excluded.component_digest",
                        (
                            sid,
                            skill,
                            source_hash,
                            body_hash,
                            source,
                            body,
                            params["kernelPath"],
                            params["bodyPath"],
                            params["componentDigest"],
                        ),
                    )
                    c.execute(
                        "INSERT INTO notebook_request_sessions VALUES(?,1) ON CONFLICT(session_id) DO UPDATE SET helper_revision=helper_revision+1",
                        (sid,),
                    )
                    binding = c.execute(
                        "SELECT notebook_id FROM notebook_bindings WHERE session_id=?",
                        (sid,),
                    ).fetchone()
                    if binding:
                        bump(c, binding[0])
                revision = c.execute(
                    "SELECT helper_revision FROM notebook_request_sessions WHERE session_id=?",
                    (sid,),
                ).fetchone()[0]
                return {
                    "skill": skill,
                    "sourceHash": source_hash,
                    "bodyHash": body_hash,
                    "helperRevision": revision,
                    "exports": exports,
                }

            return self.r.service._transaction(write)

    def capture(self, c, eid, sid):
        rows = c.execute(
            "SELECT h.skill,h.source_hash,h.body_hash,h.source,h.kernel_path,h.body_path,h.component_digest,h.body,r.registration_json "
            "FROM notebook_helpers h LEFT JOIN notebook_helper_registrations r USING(session_id,skill,source_hash,body_hash) "
            "WHERE h.session_id=? ORDER BY h.skill",
            (sid,),
        ).fetchall()
        bundle = [
            {
                "skill": row[0],
                "sourceHash": row[1],
                "bodyHash": row[2],
                "source": row[3],
                "kernelPath": row[4],
                "bodyPath": row[5],
                "componentDigest": row[6],
                "body": row[7],
                "registration": json.loads(row[8]) if row[8] else {"kind": "unknown"},
                "exports": extract_kernel_exports(row[3]),
            }
            for row in rows
        ]
        payload = encoded(bundle)
        c.execute(
            "INSERT INTO notebook_execution_helpers VALUES(?,?,?)",
            (eid, payload.decode(), hashlib.sha256(payload).hexdigest()),
        )

    def inject(self, state):
        eid = state.current_execution_token

        def read(c):
            return c.execute(
                "SELECT h.bundle_json,h.bundle_hash,x.instance_id,x.semantic_json FROM notebook_execution_helpers h "
                "JOIN notebook_runtime_executions x USING(execution_id) WHERE h.execution_id=?",
                (eid,),
            ).fetchone()

        row = self.r.db.run(read)
        if row is None or row[0] == "[]":
            return
        # Manual cells can repair a conflicting name before the Agent retries injection.
        if (
            json.loads(row[3])["origin"] != "agent"
            or json.loads(row[3])["language"] != "python"
        ):
            return
        if self.r.db.run(
            lambda c: c.execute(
                "SELECT 1 FROM notebook_helper_injections WHERE instance_id=? AND bundle_hash=?",
                (row[2], row[1]),
            ).fetchone()
        ):
            return
        result = state.kernel.execute(
            bootstrap(json.loads(row[0])), store_history=False, timeout_s=15
        )
        if result.status != "ok":
            raise NotebookError("notebook_helper_failed", 409)

        def confirm(c):
            self.r.fence(c)
            c.execute(
                "INSERT OR IGNORE INTO notebook_helper_injections VALUES(?,?)",
                (row[2], row[1]),
            )

        self.r.service._transaction(confirm)
