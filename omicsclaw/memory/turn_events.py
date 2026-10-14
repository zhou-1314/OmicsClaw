"""Durable turn event log used beneath the in-memory TurnStream cache."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .database import Database


@dataclass(frozen=True, slots=True)
class DurableTurnEvent:
    stream_id: str
    session_id: str
    turn_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: float
    owner_epoch: int = 0


@dataclass(frozen=True, slots=True)
class DurableAuthorityIdentity:
    authority_id: str
    workspace_id: str


class EventLogConflict(ValueError):
    """The same stream/sequence or owner was attempted with different data."""


class StreamOwnerConflict(EventLogConflict):
    """Another live process owns the stream lease."""


class DurableTurnEventLog:
    """SQLite-backed append-only event log; payloads remain inert JSON data."""

    def __init__(
        self,
        database: Database,
        *,
        retained: int | None = None,
        owner_id: str | None = None,
        lease_s: float = 3600.0,
    ) -> None:
        if retained is not None and (not isinstance(retained, int) or retained <= 0):
            raise ValueError("retained must be a positive integer or None")
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        self._db = database
        self._retained = retained
        self.owner_id = owner_id or uuid.uuid4().hex
        self.lease_s = float(lease_s)
        self.identity = self._identity()

    def _identity(self) -> DurableAuthorityIdentity:
        def work(conn):
            conn.execute(
                "INSERT OR IGNORE INTO turn_authority_identity VALUES(1,?,?)",
                (uuid.uuid4().hex, uuid.uuid4().hex),
            )
            row = conn.execute(
                "SELECT authority_id, workspace_id FROM turn_authority_identity WHERE singleton=1"
            ).fetchone()
            return DurableAuthorityIdentity(**dict(row))

        return self._db.run(work)

    @property
    def database(self) -> Database:
        return self._db

    def request_receipt(self, session_id: str, source_request_id: str) -> dict | None:
        def work(conn):
            row = conn.execute(
                "SELECT session_id,source_request_id,payload_hash,turn_id,created_at,payload_json "
                "FROM turn_ingress WHERE session_id=? AND source_request_id=?",
                (session_id, source_request_id),
            ).fetchone()
            return self._receipt(row) if row is not None else None

        return self._db.run(work)

    def turn_receipt(self, session_id: str, turn_id: str) -> dict | None:
        """Find the admitted source for a running turn; no second identity store."""

        def work(conn):
            row = conn.execute(
                "SELECT session_id,source_request_id,payload_hash,turn_id,created_at,payload_json FROM turn_ingress WHERE session_id=? AND turn_id=?",
                (session_id, turn_id),
            ).fetchone()
            return self._receipt(row) if row is not None else None

        return self._db.run(work)

    def claim_request(
        self,
        session_id: str,
        source_request_id: str,
        payload_hash: str,
        turn_id: str,
        payload: dict | None = None,
    ) -> dict:
        """Persist admission before scheduling. A receipt is never a retry permit."""

        def work(conn):
            conn.execute(
                "INSERT OR IGNORE INTO turn_ingress(session_id,source_request_id,payload_hash,turn_id,created_at,payload_json) VALUES(?,?,?,?,?,?)",
                (
                    session_id,
                    source_request_id,
                    payload_hash,
                    turn_id,
                    time.time(),
                    json.dumps(
                        payload or {},
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            row = conn.execute(
                "SELECT session_id,source_request_id,payload_hash,turn_id,created_at,payload_json "
                "FROM turn_ingress WHERE session_id=? AND source_request_id=?",
                (session_id, source_request_id),
            ).fetchone()
            if row["payload_hash"] != payload_hash:
                raise EventLogConflict(
                    "request identity reused with a different payload"
                )
            return self._receipt(row)

        return self._db.run(work)

    def _receipt(self, row):
        fields = dict(row)
        fields["payload"] = json.loads(fields.pop("payload_json", "{}"))
        return {**dataclasses.asdict(self.identity), **fields}

    def requests(self, session_id: str, *, offset: int = 0, limit: int = 50):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("invalid request page")

        def work(conn):
            result, size = [], 0
            for row in conn.execute(
                "SELECT * FROM turn_ingress WHERE session_id=? ORDER BY created_at,source_request_id LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ):
                cost = len(row["payload_json"].encode()) + 1024
                if cost > 524288:
                    raise ValueError("request_payload_requires_data_channel")
                if size + cost > 524288:
                    break
                result.append(self._receipt(row))
                size += cost
            return result

        return self._db.run(work)

    def admitted_sessions(self, *, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("invalid session limit")
        return self._db.run(
            lambda conn: [
                dict(row)
                for row in conn.execute(
                    "SELECT session_id,MAX(created_at) AS updated_at,MIN(created_at) AS created_at FROM turn_ingress GROUP BY session_id ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            ]
        )

    def acquire_owner(
        self, stream_id: str, *, owner_id: str | None = None, now: float | None = None
    ) -> int:
        owner = owner_id or self.owner_id
        current_time = time.time() if now is None else now

        def work(conn):
            row = conn.execute(
                "SELECT owner_id, epoch, lease_until FROM turn_stream_owners WHERE stream_id=?",
                (stream_id,),
            ).fetchone()
            if row is None:
                epoch = 1
                conn.execute(
                    "INSERT INTO turn_stream_owners(stream_id,owner_id,epoch,lease_until,updated_at) VALUES(?,?,?,?,?)",
                    (
                        stream_id,
                        owner,
                        epoch,
                        current_time + self.lease_s,
                        current_time,
                    ),
                )
                return epoch
            if row["owner_id"] != owner and row["lease_until"] >= current_time:
                raise StreamOwnerConflict(
                    f"stream {stream_id} owned by another live process"
                )
            epoch = (
                row["epoch"] + 1 if row["lease_until"] < current_time else row["epoch"]
            )
            conn.execute(
                "UPDATE turn_stream_owners SET owner_id=?, epoch=?, lease_until=?, updated_at=? WHERE stream_id=?",
                (owner, epoch, current_time + self.lease_s, current_time, stream_id),
            )
            return epoch

        try:
            return self._db.run(work)
        except sqlite3.IntegrityError as exc:
            raise StreamOwnerConflict(
                f"stream {stream_id} owner acquisition raced"
            ) from exc

    def renew_owner(
        self,
        stream_id: str,
        *,
        owner_id: str | None = None,
        epoch: int | None = None,
        now: float | None = None,
    ) -> int:
        owner = owner_id or self.owner_id
        current_time = time.time() if now is None else now

        def work(conn):
            row = conn.execute(
                "SELECT epoch, owner_id, lease_until FROM turn_stream_owners WHERE stream_id=?",
                (stream_id,),
            ).fetchone()
            if (
                row is None
                or row["owner_id"] != owner
                or (epoch is not None and row["epoch"] != epoch)
                or row["lease_until"] < current_time
            ):
                raise StreamOwnerConflict(
                    f"stream {stream_id} owner lease is not valid"
                )
            conn.execute(
                "UPDATE turn_stream_owners SET lease_until=?, updated_at=? WHERE stream_id=?",
                (current_time + self.lease_s, current_time, stream_id),
            )
            return row["epoch"]

        return self._db.run(work)

    def release_owner(
        self, stream_id: str, *, owner_id: str | None = None, epoch: int | None = None
    ) -> None:
        owner = owner_id or self.owner_id

        def work(conn):
            row = conn.execute(
                "SELECT owner_id, epoch FROM turn_stream_owners WHERE stream_id=?",
                (stream_id,),
            ).fetchone()
            if row is None:
                return
            if row["owner_id"] != owner or (
                epoch is not None and row["epoch"] != epoch
            ):
                raise StreamOwnerConflict(f"stream {stream_id} owner release conflict")
            conn.execute(
                "UPDATE turn_stream_owners SET owner_id=?, lease_until=0, updated_at=? WHERE stream_id=?",
                ("__released__" + owner, time.time(), stream_id),
            )

        self._db.run(work)

    def append(self, event: DurableTurnEvent) -> DurableTurnEvent:
        if not isinstance(event.seq, int) or event.seq <= 0:
            raise ValueError("event seq must be a positive integer")
        payload = json.dumps(
            event.payload, ensure_ascii=False, allow_nan=False, sort_keys=True
        )

        def work(conn):
            now = time.time()
            owner = conn.execute(
                "SELECT owner_id, epoch, lease_until FROM turn_stream_owners WHERE stream_id=?",
                (event.stream_id,),
            ).fetchone()
            if (
                owner is not None
                and event.owner_epoch
                and owner["epoch"] != event.owner_epoch
            ):
                raise StreamOwnerConflict(
                    f"stream {event.stream_id} owner epoch is stale"
                )
            if owner is None:
                conn.execute(
                    "INSERT INTO turn_stream_owners(stream_id,owner_id,epoch,lease_until,updated_at) VALUES(?,?,?,?,?)",
                    (event.stream_id, self.owner_id, 1, now + self.lease_s, now),
                )
            elif owner["owner_id"] != self.owner_id and owner["lease_until"] >= now:
                raise StreamOwnerConflict(
                    f"stream {event.stream_id} owner lease is not valid"
                )
            elif owner["owner_id"] != self.owner_id:
                raise StreamOwnerConflict(
                    f"stream {event.stream_id} requires acquire_owner before takeover"
                )
            elif owner["lease_until"] < now:
                raise StreamOwnerConflict(
                    f"stream {event.stream_id} owner lease expired"
                )
            else:
                conn.execute(
                    "UPDATE turn_stream_owners SET lease_until=?, updated_at=? WHERE stream_id=?",
                    (now + self.lease_s, now, event.stream_id),
                )
            row = conn.execute(
                "SELECT * FROM turn_events WHERE stream_id=? AND seq=?",
                (event.stream_id, event.seq),
            ).fetchone()
            if row is not None:
                if (
                    row["session_id"],
                    row["turn_id"],
                    row["type"],
                    row["payload_json"],
                ) != (event.session_id, event.turn_id, event.type, payload):
                    raise EventLogConflict(
                        f"event {event.stream_id}/{event.seq} conflicts"
                    )
                return self._row(row)
            latest = (
                conn.execute(
                    "SELECT MAX(seq) AS latest FROM turn_events WHERE stream_id=?",
                    (event.stream_id,),
                ).fetchone()["latest"]
                or 0
            )
            if event.seq != latest + 1:
                raise EventLogConflict(
                    f"event sequence for {event.stream_id} must follow {latest}, got {event.seq}"
                )
            conn.execute(
                "INSERT INTO turn_events(stream_id,session_id,turn_id,seq,type,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    event.stream_id,
                    event.session_id,
                    event.turn_id,
                    event.seq,
                    event.type,
                    payload,
                    event.created_at or time.time(),
                ),
            )
            if self._retained is not None:
                conn.execute(
                    "DELETE FROM turn_events WHERE stream_id=? AND seq < (SELECT COALESCE(MAX(seq),0)-?+1 FROM turn_events WHERE stream_id=?)",
                    (event.stream_id, self._retained, event.stream_id),
                )
            row = conn.execute(
                "SELECT * FROM turn_events WHERE stream_id=? AND seq=?",
                (event.stream_id, event.seq),
            ).fetchone()
            return self._row(row)

        try:
            return self._db.run(work)
        except sqlite3.IntegrityError as exc:
            raise StreamOwnerConflict(
                f"stream {event.stream_id} owner acquisition raced"
            ) from exc

    def replay(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        event_type: str | None = None,
    ) -> list[DurableTurnEvent]:
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError("invalid replay limit")

        def work(conn):
            sql = "SELECT * FROM turn_events WHERE stream_id=? AND seq>?"
            args = [stream_id, after_seq]
            if event_type is not None:
                sql += " AND type=?"
                args.append(event_type)
            sql += " ORDER BY seq"
            if limit is not None:
                sql += " LIMIT ?"
                args.append(limit)
            return [self._row(row) for row in conn.execute(sql, args).fetchall()]

        return self._db.run(work)

    def bounds(self, stream_id: str) -> tuple[int | None, int]:
        def work(conn):
            row = conn.execute(
                "SELECT MIN(seq) AS oldest, MAX(seq) AS latest FROM turn_events WHERE stream_id=?",
                (stream_id,),
            ).fetchone()
            return row["oldest"], row["latest"] or 0

        return self._db.run(work)

    @staticmethod
    def _row(row: Any) -> DurableTurnEvent:
        return DurableTurnEvent(
            row["stream_id"],
            row["session_id"],
            row["turn_id"],
            row["seq"],
            row["type"],
            json.loads(row["payload_json"]),
            row["created_at"],
            0,
        )
