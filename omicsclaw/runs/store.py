import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from threading import RLock

from .models import (
    KERNEL_STATES,
    STATES,
    Activity,
    Artifact,
    Job,
    Kernel,
    Run,
    Verification,
    normalize_status,
    transition,
)

_TYPES = {
    "run": Run,
    "activity": Activity,
    "job": Job,
    "kernel": Kernel,
    "artifact": Artifact,
    "verification": Verification,
}


class RunStore:
    SCHEMA_VERSION = 1

    def __init__(self, path=":memory:", *, on_event=None):
        self.on_event = on_event
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = RLock()
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS run_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS run_objects (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS run_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, object_id TEXT NOT NULL, kind TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO run_schema(key,value) VALUES ('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )
            self.db.commit()

    @property
    def schema_version(self):
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM run_schema WHERE key='schema_version'"
            ).fetchone()
        return int(row[0]) if row else 0

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def save(self, obj, *, if_absent=False, event=None):
        kind = next(k for k, v in _TYPES.items() if isinstance(obj, v))
        ident = getattr(obj, kind + "_id")
        data = asdict(obj)
        if "status" in data:
            data["status"] = normalize_status(data["status"])
            if data["status"] not in (KERNEL_STATES if kind == "kernel" else STATES):
                raise ValueError(f"unknown run status: {data['status']}")
            obj.status = data["status"]
        payload = json.dumps(data)
        with self.transaction() as db:
            old = db.execute(
                "SELECT payload FROM run_objects WHERE id=?", (ident,)
            ).fetchone()
            if old and if_absent:
                return ident
            if old and kind == "kernel":
                previous_data = json.loads(old[0])
                data["run_ids"] = sorted(
                    set(previous_data.get("run_ids", []) + data["run_ids"])
                )
                data["run_id"] = previous_data.get("run_id") or data["run_id"]
                obj.run_ids, obj.run_id = data["run_ids"], data["run_id"]
                payload = json.dumps(data)
            if old and json.loads(old[0]) == data and event is None:
                return ident
            if old and "status" in data and kind != "kernel":
                previous = json.loads(old[0]).get("status", "created")
                transition(normalize_status(previous), data["status"])
            db.execute(
                "INSERT OR REPLACE INTO run_objects VALUES (?,?,?)",
                (ident, kind, payload),
            )
            event = event or ("updated" if old else "created")
            cursor = db.execute(
                "INSERT INTO run_events(object_id,kind,event,payload) VALUES (?,?,?,?)",
                (ident, kind, event, payload),
            )
            seq = cursor.lastrowid
        if self.on_event is not None:
            try:
                self.on_event(
                    {
                        "seq": seq,
                        "object_id": ident,
                        "kind": kind,
                        "event": event,
                        "payload": data,
                    }
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "run event observer failed: %s", ident
                )
        return ident

    def load(self, ident):
        with self._lock:
            row = self.db.execute(
                "SELECT kind,payload FROM run_objects WHERE id=?", (ident,)
            ).fetchone()
        if not row:
            return None
        return self._from_payload(row[0], json.loads(row[1]))

    def list(self, kind=None):
        with self._lock:
            rows = self.db.execute("SELECT kind,payload FROM run_objects").fetchall()
        return [
            self._from_payload(k, json.loads(p))
            for k, p in rows
            if kind is None or k == kind
        ]

    @staticmethod
    def _from_payload(kind, payload):
        fields = _TYPES[kind].__dataclass_fields__
        clean = {k: v for k, v in payload.items() if k in fields}
        if "status" in clean:
            clean["status"] = normalize_status(clean["status"])
        return _TYPES[kind](**clean)

    def replay(self, object_id):
        events = self.events(object_id)
        if not events:
            return None
        return self._from_payload(events[-1]["kind"], events[-1]["payload"])

    def events(self, object_id=None, *, after_seq=0, run_id=None):
        with self._lock:
            rows = self.db.execute(
                "SELECT seq,object_id,kind,event,payload FROM run_events "
                "WHERE seq > ? AND (object_id=? OR ? IS NULL) "
                "AND (? IS NULL OR object_id=? OR json_extract(payload, '$.run_id')=? "
                "OR EXISTS (SELECT 1 FROM json_each(run_events.payload, '$.run_ids') WHERE value=?) "
                "OR json_extract(payload, '$.artifact_id') IN "
                "(SELECT id FROM run_objects WHERE kind='artifact' AND json_extract(payload, '$.run_id')=?)) "
                "ORDER BY seq",
                (
                    after_seq,
                    object_id,
                    object_id,
                    run_id,
                    run_id,
                    run_id,
                    run_id,
                    run_id,
                ),
            ).fetchall()
        return [
            {
                "seq": r[0],
                "object_id": r[1],
                "kind": r[2],
                "event": r[3],
                "payload": json.loads(r[4]),
            }
            for r in rows
        ]
