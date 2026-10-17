"""Owned, hashed data files; scientific paths never become command inputs directly."""

import hashlib
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from omicsclaw.memory.artifacts import ArtifactRecord
from omicsclaw.skillenv import notebook_identity as codec

from . import analysis_contracts as wire
from .artifacts import evidence, open_inside
from .contracts import encoded
from .security import private_directory
from .service import NotebookError


class AnalysisFiles:
    def __init__(self, runtime):
        self.r = runtime
        self.directory = private_directory(
            runtime.workspace / ".omicsclaw/notebook-analysis"
        )
        self.staging = private_directory(self.directory / "staging")
        self.owned = private_directory(self.directory / "objects")
        self.pending = ContextVar("analysis_files_pending", default=None)

    def disk_usage(self):
        """Count unregistered files as well as committed artifact bytes."""
        total = sum(
            p.stat().st_size
            for p in (self.r.workspace / ".omicsclaw").glob("notebook*.db*")
            if p.is_file()
        )
        artifact, registered = self.r.artifact_db.run(
            lambda c: c.execute(
                "SELECT COALESCE(SUM(size),0), COALESCE(SUM(CASE WHEN produced_by='notebook_analysis' THEN size ELSE 0 END),0) FROM artifacts"
            ).fetchone()
        )
        physical, entries = 0, 0
        if not stat.S_ISDIR(self.directory.lstat().st_mode):
            wire.fail("limit", 413)

        def unreadable(error):
            raise error

        for root, dirs, files in os.walk(
            self.directory, followlinks=False, onerror=unreadable
        ):
            for name in dirs + files:
                try:
                    info = (Path(root) / name).lstat()
                except FileNotFoundError:
                    continue
                entries += 1
                if entries > 100000 or stat.S_ISLNK(info.st_mode):
                    wire.fail("limit", 413)
                if stat.S_ISREG(info.st_mode):
                    physical += info.st_size
                elif not stat.S_ISDIR(info.st_mode):
                    wire.fail("limit", 413)
                if (
                    total + artifact - registered + max(registered, physical)
                    > self.r.limits["diskBytes"]
                ):
                    return total + artifact - registered + max(registered, physical)
        return total + artifact - registered + max(registered, physical)

    def discard(self, item):
        aid, path, birth = item
        # Only this attempt's exclusive file and artifact ID may be removed.
        try:
            info = path.lstat()
            if (info.st_dev, info.st_ino) != birth:
                return
            self.r.artifact_db.run(
                lambda c: c.execute(
                    "DELETE FROM artifacts WHERE id=? AND path=? AND produced_by='notebook_analysis'",
                    (aid, str(path)),
                )
            )
            path.unlink()
        except OSError:
            pass  # An unremoved file remains charged to the physical budget.

    @contextmanager
    def output(self, aid, path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        item = (aid, path, (info.st_dev, info.st_ino))
        pending = self.pending.get()
        if pending is not None:
            pending.append(item)
        try:
            with os.fdopen(fd, "wb") as stream:
                yield stream
        except BaseException:
            self.discard(item)
            raise

    def transaction(self, work):
        pending, rolled_back = [], False
        token = self.pending.set(pending)

        def write(c):
            nonlocal rolled_back
            try:
                return work(c)
            except BaseException:
                c.rollback()
                rolled_back = True
                raise

        try:
            return self.r.service._transaction(write)
        except BaseException:
            # A COMMIT failure is uncertain: keep files and account for them.
            if rolled_back:
                for item in pending:
                    self.discard(item)
            raise
        finally:
            self.pending.reset(token)

    @contextmanager
    def reading(self, path, maximum):
        fd = open_inside(self.r.workspace, path)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or not 0 <= before.st_size <= maximum
            ):
                wire.fail("limit", 413)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream
            if evidence(before) != evidence(os.fstat(fd)):
                wire.fail("reference_stale", 409)
        finally:
            os.close(fd)

    def read_json(self, path, maximum=16384):
        with self.reading(path, maximum) as stream:
            try:
                return json.load(stream)
            except (ValueError, UnicodeError):
                wire.fail("invalid_reference")

    def validate_artifact(self, c, nid, reference, *, selection_input=False):
        wire.artifact(reference)
        record = self.r.artifacts.get_artifact(reference["artifactId"])
        if (
            record is None
            or record.sha256 != reference["sha256"]
            or record.size != reference["bytes"]
        ):
            wire.fail("invalid_reference")
        execution = c.execute(
            "SELECT k.session_key FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE e.notebook_id=? AND e.execution_id=?",
            (nid, record.meta.get("executionId")),
        ).fetchone()
        if (
            not (execution and execution[0] == record.session_id)
            and record.meta.get("notebookId") != nid
        ):
            wire.fail("invalid_reference")
        if selection_input:
            row = c.execute(
                "SELECT 1 FROM notebook_analysis_entities WHERE notebook_id=? AND kind='selectionInput' AND entity_id=?",
                (nid, record.id),
            ).fetchone()
            if row is None:
                wire.fail("invalid_reference")
        digest, total = hashlib.sha256(), 0
        with self.reading(record.path, 512 * 1024 * 1024) as stream:
            while block := stream.read(128 * 1024):
                digest.update(block)
                total += len(block)
        if digest.hexdigest() != reference["sha256"] or total != reference["bytes"]:
            wire.fail("reference_stale", 409)
        return record

    def clean(self, paths):
        for path in paths or ():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if paths:
            try:
                paths[0].parent.rmdir()
            except OSError:
                pass

    def publish(self, nid, aid, path, *, eid=None, kind="selection_ids"):
        digest, size = hashlib.sha256(), 0
        with self.reading(path, codec.MAX_ID_BYTES) as stream:
            while block := stream.read(128 * 1024):
                digest.update(block)
                size += len(block)
        if self.disk_usage() > self.r.limits["diskBytes"]:
            wire.fail("limit", 413)
        self.r.artifacts.insert_artifact(
            ArtifactRecord(
                id=aid,
                kind="table",
                path=str(path),
                title=kind,
                mime="application/vnd.omicsclaw.id-tuples",
                sha256=digest.hexdigest(),
                size=size,
                produced_by="notebook_analysis",
                meta={
                    "notebookId": nid,
                    "executionId": eid,
                    "format": "omicsclaw-id-tuples-v1",
                },
                created_at=time.time(),
            )
        )
        return {"artifactId": aid, "sha256": digest.hexdigest(), "bytes": size}

    def members(self, c, nid, view, *, values=None, source=None):
        """SQL enforces uniqueness and membership without retaining the whole set."""
        keys = view["axis"]["keys"]
        c.execute(
            "CREATE TEMP TABLE IF NOT EXISTS notebook_selection_pending(identity BLOB PRIMARY KEY)"
        )
        c.execute("DELETE FROM notebook_selection_pending")
        count, total = 0, 0

        def consume(rows):
            nonlocal count, total
            for row in rows:
                raw = codec.tuple_bytes(row)
                if len(row) != len(keys):
                    wire.fail("invalid_reference")
                count, total = count + 1, total + len(raw) + 1
                if count > codec.MAX_IDS or total > codec.MAX_ID_BYTES:
                    wire.fail("limit", 413)
                match = c.execute(
                    "SELECT 1 FROM notebook_analysis_view_rows WHERE notebook_id=? AND view_id=? AND view_revision=? AND identity=?",
                    (nid, view["viewId"], view["viewRevision"], raw),
                ).fetchone()
                if match is None:
                    wire.fail("selection_invalid", 409)
                if not c.execute(
                    "INSERT OR IGNORE INTO notebook_selection_pending VALUES(?)", (raw,)
                ).rowcount:
                    wire.fail("invalid_reference")

        try:
            if source is None:
                consume(values)
            else:
                record = self.validate_artifact(c, nid, source)
                with self.reading(record.path, codec.MAX_ID_BYTES) as stream:
                    digest = hashlib.sha256()
                    # Verify and consume the same descriptor, never a checked path reopened for use.
                    while block := stream.read(128 * 1024):
                        digest.update(block)
                    if digest.hexdigest() != source["sha256"]:
                        wire.fail("reference_stale", 409)
                    stream.seek(0)
                    consume(codec.read_rows(stream, keys))
            if not count:
                wire.fail("invalid_reference")
            aid = uuid.uuid4().hex
            path = self.owned / (aid + ".ids")
            digest = hashlib.sha256(
                b"omicsclaw-selection-v1\n" + codec.tuple_bytes(keys) + b"\n"
            )
            samples = []
            with self.output(aid, path) as stream:
                stream.write(codec.header(keys))
                for row in c.execute(
                    "SELECT identity FROM notebook_selection_pending ORDER BY identity"
                ):
                    raw = bytes(row[0])
                    digest.update(raw + b"\n")
                    stream.write(raw + b"\n")
                    if len(samples) < 20 and len(encoded(samples)) + len(raw) < 4096:
                        samples.append(json.loads(raw))
                stream.flush()
                os.fsync(stream.fileno())
                directory_fd = os.open(self.owned, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return {
                    "memberArtifact": self.publish(nid, aid, path),
                    "count": count,
                    "membersSha256": digest.hexdigest(),
                    "sampleIds": samples,
                }
        except NotebookError:
            raise
        except (ValueError, UnicodeError):
            wire.fail("invalid_reference")
        finally:
            c.execute("DELETE FROM notebook_selection_pending")

    def copy_input(self, c, nid, eid, path, keys, expected_count):
        aid = uuid.uuid4().hex
        destination = self.owned / (aid + ".ids")
        with (
            self.output(aid, destination) as target,
            self.reading(path, codec.MAX_ID_BYTES) as source,
        ):
            c.execute(
                "CREATE TEMP TABLE IF NOT EXISTS notebook_input_pending(identity BLOB PRIMARY KEY)"
            )
            c.execute("DELETE FROM notebook_input_pending")

            def unique_rows():
                for row in codec.read_rows(source, keys):
                    if not c.execute(
                        "INSERT OR IGNORE INTO notebook_input_pending VALUES(?)",
                        (codec.tuple_bytes(row),),
                    ).rowcount:
                        wire.fail("invalid_reference")
                    yield row

            count = codec.write_rows(target, keys, unique_rows())
            c.execute("DELETE FROM notebook_input_pending")
            target.flush()
            os.fsync(target.fileno())
            directory_fd = os.open(self.owned, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if count != expected_count:
                wire.fail("invalid_reference")
            return {
                **self.publish(nid, aid, destination, eid=eid),
                "kind": "selection_ids",
                "format": "omicsclaw-id-tuples-v1",
                "keys": keys,
                "count": count,
                "producerExecutionId": eid,
            }
