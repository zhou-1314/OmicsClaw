"""Failure decisions and stage barriers survive transactional interruptions."""

import copy
import json
from types import SimpleNamespace

import pytest

from omicsclaw.entry.notebook.checkpoint_store import SCHEMA, CheckpointStore, reference
from omicsclaw.entry.notebook.contracts import encoded
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_analysis_store import seed
from tests.entry.test_notebook_analysis_store import store as analysis_fixture


@pytest.fixture
def store(tmp_path):
    fixture = analysis_fixture.__wrapped__(tmp_path)
    analysis = next(fixture)
    runtime = analysis.r
    runtime.source_digest = "component"
    runtime.analysis = SimpleNamespace(
        store=analysis, files=SimpleNamespace(disk_usage=lambda: 100)
    )
    runtime.checkpoints = SimpleNamespace(
        limits={
            "maxObjects": 32,
            "maxCheckpointBytes": 1000,
            "maxStagingBytes": 1000,
            "maxWorkspaceBytes": 10000,
        }
    )
    runtime.db.run(lambda c: c.executescript(SCHEMA))
    obj, _, _ = seed(analysis)

    def setup(c):
        obj.update(symbol="data", objectRevision=2)
        analysis.put(c, "nb", "object", obj)
        c.execute(
            "INSERT INTO notebook_analysis_specs VALUES(?,?,?)",
            (
                "nb",
                "obj",
                encoded(
                    {
                        "symbol": "data",
                        "adapter": "anndata",
                        "kernel": obj["kernel"],
                        "declaredMatrix": {"layer": "counts"},
                    }
                ).decode(),
            ),
        )

    runtime.db.run(setup)
    result = CheckpointStore(runtime)
    result.source = obj["kernel"]
    yield result
    with pytest.raises(StopIteration):
        next(fixture)


def write(store, work):
    def transaction(c):
        c.execute("BEGIN IMMEDIATE")
        return work(c)

    return store.r.db.run(transaction)


def intent(store, **kwargs):
    return write(store, lambda c: store.intent(c, "nb", store.source, **kwargs))


def failed(store, **kwargs):
    _, oid = intent(store, **kwargs)
    ref = write(
        store, lambda c: store.failed(c, "nb", oid, "write_failed", stopped=True)
    )
    return oid, ref


def test_stage_success_and_intent_rollback_together(store):
    def crash(c):
        c.execute("INSERT INTO notebook_checkpoint_stages VALUES('source','nb','{}')")
        store.intent(
            c,
            "nb",
            store.source,
            stage={"executionId": "source", "stage": "qc", "ordinal": 1},
            automatic=True,
        )
        raise RuntimeError("commit aborted")

    with pytest.raises(RuntimeError):
        write(store, crash)
    assert (
        store.r.db.run(
            lambda c: c.execute(
                "SELECT count(*) FROM notebook_checkpoint_stages"
            ).fetchone()[0]
        )
        == 0
    )
    assert store.r.db.run(lambda c: store.blocked(c, "nb", "python")) == ([], [])
    _, oid = intent(
        store,
        stage={"executionId": "source", "stage": "qc", "ordinal": 1},
        automatic=True,
    )
    with pytest.raises(NotebookError, match="checkpoint_blocked"):
        store.r.db.run(lambda c: store.require_clear(c, "nb", "python"))
    assert (
        store.r.db.run(lambda c: store.get(c, "nb", "operation", oid))["state"]
        == "intent"
    )


def test_automatic_budget_failure_is_durable_but_manual_rejection_is_not(store):
    store.r.checkpoints.limits["maxWorkspaceBytes"] = 200
    with pytest.raises(NotebookError, match="checkpoint_limit"):
        intent(store)
    assert (
        store.r.db.run(
            lambda c: c.execute(
                "SELECT count(*) FROM notebook_checkpoint_intents"
            ).fetchone()[0]
        )
        == 0
    )
    _, oid = intent(store, automatic=True)
    op = store.r.db.run(lambda c: store.get(c, "nb", "operation", oid))
    failure = store.r.db.run(
        lambda c: store.get(c, "nb", "failure", op["failureRef"]["failureId"])
    )
    assert (
        failure["blocking"]
        and failure["stopConfirmed"]
        and failure["reasonCode"] == "checkpoint_byte_limit"
    )


def test_readonly_inspection_refresh_does_not_invalidate_failure_decision(store):
    _, ref = failed(store)

    def refresh(c):
        obj = store.r.analysis.store.raw(c, "nb", "object", "obj")
        obj.update(objectRevision=obj["objectRevision"] + 1, shape=[3, 2])
        store.r.analysis.store.put(c, "nb", "object", obj)
        store.r.analysis.store.advance(c, "nb")

    write(store, refresh)
    resolved = write(store, lambda c: store.continued(c, "nb", ref))
    assert not resolved["blocking"] and resolved["recoveryGapId"]
    assert resolved["objects"] == [{"objectId": "obj", "objectRevision": 2}]


@pytest.mark.parametrize("change", ["code", "spec"])
def test_actual_mutation_or_registration_change_invalidates_old_failure(store, change):
    _, ref = failed(store)

    def mutate(c):
        if change == "code":
            store.mutation(c, "nb", "python", advance=True)
        else:
            value = json.loads(
                c.execute("SELECT document FROM notebook_analysis_specs").fetchone()[0]
            )
            value["declaredMatrix"]["layer"] = "other"
            c.execute(
                "UPDATE notebook_analysis_specs SET document=?",
                (encoded(value).decode(),),
            )

    write(store, mutate)
    with pytest.raises(NotebookError, match="checkpoint_reference_stale"):
        write(store, lambda c: store.continued(c, "nb", ref))


def test_retry_failed_replaces_only_its_failure_chain(store):
    _, first = failed(store)
    _, other = failed(store)
    _, second = failed(store, retry=first)
    old, new, independent = store.r.db.run(
        lambda c: [
            store.get(c, "nb", "failure", ref["failureId"])
            for ref in (first, second, other)
        ]
    )
    assert not old["blocking"] and old["supersededByFailure"] == second
    assert new["blocking"] and new["rootFailureId"] == first["failureId"]
    assert independent["blocking"]
    with pytest.raises(NotebookError, match="checkpoint_reference_stale"):
        write(store, lambda c: store.continued(c, "nb", first))
    write(store, lambda c: store.continued(c, "nb", second))
    assert [
        reference(v, "failure")
        for v in store.r.db.run(lambda c: store.blocked(c, "nb", "python"))[1]
    ] == [other]


def test_unknown_inspector_keeps_reservation_and_rejects_continue(store):
    _, oid = intent(store)
    write(
        store,
        lambda c: store.transition(
            c, "nb", oid, "inspecting", expected={"intent"}, stopConfirmed=False
        ),
    )
    write(store, store.recover)
    assert (
        store.r.db.run(lambda c: store.get(c, "nb", "operation", oid))["state"]
        == "unknown"
    )
    assert (
        store.r.db.run(
            lambda c: c.execute(
                "SELECT released FROM notebook_checkpoint_intents"
            ).fetchone()[0]
        )
        == 0
    )
    ref = write(
        store, lambda c: store.failed(c, "nb", oid, "owner_lost", stopped=False)
    )
    with pytest.raises(NotebookError, match="checkpoint_stop_unconfirmed"):
        write(store, lambda c: store.continued(c, "nb", ref))


def test_complete_group_publication_and_resolution_are_atomic(store):
    _, ref = failed(store)
    cid, oid = intent(store, retry=ref)
    write(
        store,
        lambda c: store.transition(
            c, "nb", oid, "publishing", expected={"intent"}, bindingsDigest="f" * 64
        ),
    )
    manifest = {"bindings": [{"artifact": {"bytes": 22}}, {"artifact": {"bytes": 33}}]}

    def abort(c):
        store.published(c, "nb", oid, manifest)
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        write(store, abort)
    assert (
        store.r.db.run(lambda c: store.get(c, "nb", "checkpoint", cid))["state"]
        == "pending"
    )
    assert store.r.db.run(lambda c: store.get(c, "nb", "failure", ref["failureId"]))[
        "blocking"
    ]
    item = write(store, lambda c: store.published(c, "nb", oid, manifest))
    assert item["state"] == "ready" and item["totalBytes"] == 55
    assert store.r.db.run(lambda c: store.get(c, "nb", "failure", ref["failureId"]))[
        "resolvedByCheckpoint"
    ] == reference(item, "checkpoint")


def test_historical_failures_do_not_consume_current_blocker_limit(store):
    _oid, ref = failed(store)
    sample = store.r.db.run(lambda c: store.get(c, "nb", "failure", ref["failureId"]))

    def histories(c):
        for index in range(401):
            item = copy.deepcopy(sample)
            item.update(failureId=f"old{index}", blocking=False)
            store.put(c, "nb", "failure", item)

    write(store, histories)
    operations, failures = store.r.db.run(lambda c: store.blocked(c, "nb", "python"))
    assert operations == [] and [reference(v, "failure") for v in failures] == [ref]
