"""Run guards use a single bounded snapshot, independently of history size."""

import asyncio
import copy
import sqlite3

import pytest

from omicsclaw.entry.notebook.analysis_store import entity_ref
from omicsclaw.entry.notebook.contracts import encoded
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_analysis_owner import reference, settled
from tests.entry.test_notebook_analysis_store import seed
from tests.entry.test_notebook_analysis_store import store as store_fixture
from tests.entry.test_notebook_r import bind, command, create, environment


@pytest.fixture(name="store")
def analysis_store(tmp_path):
    yield from store_fixture.__wrapped__(tmp_path)


def snapshot(store):
    return store.state_response({"notebookId": "nb", "subject": {"kind": "runContext"}})


def guard(page, language="python"):
    item = page["item"]
    refs = [
        entity_ref(obj, "object")
        for obj in item["objects"]
        if obj["kernel"]["language"] == language
    ]
    selected = item["activeSelection"]
    if selected and selected["object"] not in refs:
        refs.append(selected["object"])
    return {
        "analysisRevision": page["analysisRevision"],
        "objects": refs,
        "selections": [entity_ref(selected, "selection")] if selected else [],
    }


def history(store, kernel, count=401):
    def write(c):
        for index in range(count):
            store.put(
                c,
                "nb",
                "object",
                {
                    "objectId": f"old{index:04}",
                    "objectRevision": 1,
                    "kernel": kernel,
                    "registrationState": "removed",
                    "availability": "verified",
                    "reason": "",
                    "metadata": "汉" * 4000,
                },
            )
        store.advance(c, "nb")

    store.r.db.run(write)


def test_actual_owner_accepts_run_after_401_removed_and_multigeneration_objects(
    tmp_path, notebook_control_rss
):
    async def run():
        owner = await create(tmp_path)
        try:
            env = await environment(owner, "python")
            for generation in range(3):
                kernel = (await bind(owner, "python", env, str(generation)))["kernel"]
                if generation < 2:
                    await owner.release(
                        {
                            "notebookId": "nb",
                            "commandId": f"release{generation}",
                            "kernelInstanceId": kernel["kernelInstanceId"],
                            "generation": kernel["generation"],
                        }
                    )
            history(owner.analysis.store, reference(kernel))

            def historical(c):
                for number in (1, 2):
                    row = c.execute(
                        "SELECT * FROM notebook_runtime_kernels WHERE generation=?",
                        (number,),
                    ).fetchone()
                    old = reference(kernel)
                    old.update(kernelInstanceId=row["instance_id"], generation=number)
                    owner.analysis.store.put(
                        c,
                        "nb",
                        "object",
                        {
                            "objectId": f"past{number}",
                            "objectRevision": 1,
                            "kernel": old,
                            "availability": "verified",
                            "reason": "",
                            "registrationState": "active",
                        },
                    )

            owner.db.run(historical)
            page = snapshot(owner.analysis.store)
            assert page["item"] == {
                "objects": [],
                "kernels": [reference(kernel)],
                "activeSelection": None,
                "selectionObject": None,
            }
            assert len(encoded(page)) < 1024
            assert (
                owner.analysis.store.state_response({"notebookId": "nb"})["counts"][
                    "objects"
                ]
                == 403
            )
            result = await owner.admit(
                {
                    "command": {
                        **command(kernel, "fresh", "answer=42"),
                        "version": 3,
                        "preconditions": {"analysis": guard(page)},
                    }
                }
            )
            assert (await settled(owner, result["execution_id"]))[
                "status"
            ] == "completed"
            assert "notebook_analysis_run_context_v1" in owner.health()["capabilities"]
        finally:
            await owner.close()

    asyncio.run(run())


def test_history_does_not_hide_current_invalid_object(store):
    obj, _, _ = seed(store)
    history(store, obj["kernel"])
    store.r.db.run(lambda c: store.advance(c, "nb", active=None))
    store.r.db.run(lambda c: store.invalidate(c, "nb", "python", "code_may_modify"))
    page = snapshot(store)
    assert len(page["item"]["objects"]) == 1
    assert page["item"]["objects"][0]["availability"] == "needs_inspection"
    with pytest.raises(NotebookError, match="reference_stale"):
        store.r.db.run(
            lambda c: store.validate_guard(c, "nb", guard(page), language="python")
        )


def test_historical_selection_source_remains_invalid_across_languages(store):
    obj, _, _ = seed(store, "r")
    history(store, obj["kernel"])
    store.r.db.run(
        lambda c: c.execute("UPDATE notebook_runtime_kernels SET state='released'")
    )
    page = snapshot(store)
    assert page["item"]["objects"] == []
    assert page["item"]["kernels"] == []
    assert page["item"]["activeSelection"]["valid"] is False
    assert page["item"]["selectionObject"]["registrationState"] == "historical"
    assert page["item"]["selectionObject"]["objectId"] == obj["objectId"]
    for language in ("python", "r"):
        with pytest.raises(NotebookError, match="reference_stale|selection_invalid"):
            store.r.db.run(
                lambda c, language=language: store.validate_guard(
                    c, "nb", guard(page, language), language=language
                )
            )


def test_run_context_transaction_keeps_watermark_and_refs_together(store, monkeypatch):
    seed(store)
    read = store.run_context

    def concurrent_write(c, nid):
        with sqlite3.connect(store.r.db.path) as other:
            obj = store.raw(other, nid, "object", "obj")
            obj.update(objectRevision=2, availability="needs_inspection")
            store.put(other, nid, "object", obj)
            store.advance(other, nid)
        return read(c, nid)

    monkeypatch.setattr(store, "run_context", concurrent_write)
    page = snapshot(store)
    assert page["analysisRevision"] == 1
    assert page["item"]["objects"][0]["objectRevision"] == 1
    assert page["item"]["activeSelection"]["valid"] is True
    monkeypatch.setattr(store, "run_context", read)
    current = snapshot(store)
    assert current["analysisRevision"] == 2
    assert current["item"]["objects"][0]["objectRevision"] == 2
    assert current["item"]["activeSelection"]["valid"] is False
    with pytest.raises(NotebookError, match="reference_stale"):
        store.r.db.run(
            lambda c: store.validate_guard(c, "nb", guard(page), language="python")
        )


def test_run_context_32_current_objects_is_bounded_and_does_not_include_metadata(store):
    obj, _, _ = seed(store)

    def write(c):
        for index in range(31):
            value = copy.deepcopy(obj)
            value.update(objectId=f"live{index}", metadata="汉" * 4000)
            store.put(c, "nb", "object", value)

    store.r.db.run(write)
    page = snapshot(store)
    assert len(page["item"]["objects"]) == 32
    assert len(encoded(page)) < 32 * 1024
    assert all(
        "metadata" not in item and "defaultView" not in item
        for item in page["item"]["objects"]
    )
    store.r.db.run(
        lambda c: store.validate_guard(c, "nb", guard(page), language="python")
    )
    for subject in (
        {"kind": "runContext", "id": "invented"},
        {"kind": "runContext", "revision": 1},
    ):
        with pytest.raises(NotebookError, match="invalid_reference|invalid_command"):
            store.state_response({"notebookId": "nb", "subject": subject})
