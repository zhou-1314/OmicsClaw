"""State references cannot outlive mutations, language switches or page fences."""

from types import SimpleNamespace

import pytest

from omicsclaw.entry.notebook import analysis_contracts as wire
from omicsclaw.entry.notebook.analysis_store import SCHEMA, AnalysisStore
from omicsclaw.entry.notebook.checkpoint_store import SCHEMA as CHECKPOINT_SCHEMA
from omicsclaw.entry.notebook.checkpoint_store import CheckpointStore
from omicsclaw.entry.notebook.contracts import encoded
from omicsclaw.entry.notebook.restores import OwnerRestores
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.entry.notebook.store import SCHEMA as OWNER_SCHEMA
from omicsclaw.entry.notebook.store import migrate
from omicsclaw.memory.database import Database


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "analysis.db")
    db.run(lambda c: c.executescript(OWNER_SCHEMA + SCHEMA + CHECKPOINT_SCHEMA))
    db.run(migrate)
    runtime = SimpleNamespace(
        db=db,
        store_identity={"authorityId": "a", "workspaceId": "w"},
        fence=lambda c: None,
    )
    runtime.checkpoints = SimpleNamespace(store=CheckpointStore(runtime))
    runtime.analysis = SimpleNamespace(files=SimpleNamespace(directory=tmp_path))
    runtime.restores = OwnerRestores(runtime)
    result = AnalysisStore(runtime)
    try:
        yield result
    finally:
        db.close()


def seed(store, language="python"):
    kernel = {
        "language": language,
        "kernelInstanceId": "instance",
        "generation": 1,
        "environmentId": "env",
        "environmentRevision": 1,
    }
    obj = {
        "objectId": "obj",
        "objectRevision": 1,
        "kernel": kernel,
        "availability": "verified",
        "reason": "",
        "defaultView": {"viewId": "view", "viewRevision": 1},
    }
    view = {
        "viewId": "view",
        "viewRevision": 1,
        "object": {"objectId": "obj", "objectRevision": 1},
        "valid": True,
        "reason": "",
    }
    selection = {
        "selectionId": "selection",
        "selectionRevision": 1,
        "object": view["object"],
        "valid": True,
        "reason": "",
    }

    def write(c):
        c.execute(
            "INSERT INTO notebook_runtime_kernels VALUES('instance','nb',1,'session','env',1,'{}',0,'ready',NULL,0,0,?)",
            (language,),
        )
        store.put(c, "nb", "object", obj)
        store.put(c, "nb", "view", view)
        store.put(c, "nb", "selection", selection)
        store.advance(
            c, "nb", active={"selectionId": "selection", "selectionRevision": 1}
        )

    store.r.db.run(write)
    return obj, view, selection


def test_cross_language_active_selection_requires_source_object_and_revision(store):
    seed(store, "r")
    valid = {
        "analysisRevision": 1,
        "objects": [{"objectId": "obj", "objectRevision": 1}],
        "selections": [{"selectionId": "selection", "selectionRevision": 1}],
    }
    store.r.db.run(lambda c: store.validate_guard(c, "nb", valid, language="python"))
    with pytest.raises(NotebookError, match="selection_invalid"):
        store.r.db.run(
            lambda c: store.validate_guard(
                c, "nb", {**valid, "objects": [], "selections": []}, language="python"
            )
        )
    store.r.db.run(lambda c: store.invalidate(c, "nb", "r", "code_may_modify"))
    with pytest.raises(NotebookError, match="reference_stale"):
        store.r.db.run(
            lambda c: store.validate_guard(c, "nb", valid, language="python")
        )
    historical = store.state_response(
        {
            "notebookId": "nb",
            "subject": {"kind": "selection", "id": "selection", "revision": 1},
        }
    )["item"]
    assert historical["valid"] is False
    assert (
        store.state_response({"notebookId": "nb"})["activeSelection"]
        == valid["selections"][0]
    )


def test_pages_pin_high_water_and_reject_cross_scope_cursor(store):
    def put(c, name):
        store.put(c, "nb", "dataset", {"datasetId": name, "datasetRevision": 1})

    store.r.db.run(lambda c: [put(c, name) for name in ("a", "c", "e")])
    first = store.page({"notebookId": "nb", "collection": "datasets", "limit": 1})
    store.r.db.run(lambda c: put(c, "b"))
    second = store.page(
        {
            "notebookId": "nb",
            "collection": "datasets",
            "limit": 1,
            "cursor": first["nextCursor"],
        }
    )
    assert [v["datasetId"] for v in second["items"]] == ["c"]
    with pytest.raises(NotebookError, match="invalid_page"):
        store.page(
            {
                "notebookId": "other",
                "collection": "datasets",
                "cursor": first["nextCursor"],
            }
        )
    with pytest.raises(NotebookError, match="invalid_page"):
        store.page(
            {"notebookId": "nb", "collection": "objects", "cursor": first["nextCursor"]}
        )


def test_page_budget_is_utf8_bytes_before_decoding_large_histories(store):
    def write(c):
        for index in range(40):
            store.put(
                c,
                "nb",
                "dataset",
                {
                    "datasetId": f"d{index:02}",
                    "datasetRevision": 1,
                    "metadata": "汉" * 5000,
                },
            )

    store.r.db.run(write)
    result = store.page({"notebookId": "nb", "collection": "datasets", "limit": 40})
    assert len(encoded(result)) <= wire.MAX_PAGE_BYTES
    assert 1 < len(result["items"]) < 40 and result["nextCursor"]


def test_nul_and_surrogate_rejected_before_any_scientific_json_conversion():
    for value in ("a\0b", "a\ud800b"):
        with pytest.raises(NotebookError, match="invalid_command"):
            wire.text(value)


def test_old_run_cannot_ignore_registered_state(store):
    seed(store)
    with pytest.raises(NotebookError, match="owner_upgrade_required"):
        store.r.db.run(
            lambda c: store.run_guard(
                c, "nb", {"preconditions": {}, "language": "python"}
            )
        )


def test_clear_can_remove_invalid_selection_after_source_kernel_release(store):
    from omicsclaw.entry.notebook.analysis import OwnerAnalysis

    seed(store, "r")
    store.r.db.run(
        lambda c: c.execute("UPDATE notebook_runtime_kernels SET state='released'")
    )
    command = {
        "action": "selection.clear",
        "arguments": {
            "selection": {"selectionId": "selection", "selectionRevision": 1}
        },
        "expected": {
            "analysisRevision": 1,
            "kernels": [],
            "objects": [],
            "selections": [{"selectionId": "selection", "selectionRevision": 1}],
        },
    }
    analysis = object.__new__(OwnerAnalysis)
    analysis.store = store
    analysis.r = store.r
    store.r.db.run(lambda c: analysis.expected(c, "nb", command))
    blocked = {
        "operationId": "saving",
        "statusSeq": 1,
        "source": {"language": "r"},
        "state": "saving",
    }
    store.r.db.run(
        lambda c: store.r.checkpoints.store.put(c, "nb", "operation", blocked)
    )
    with pytest.raises(NotebookError, match="checkpoint_blocked"):
        store.r.db.run(lambda c: analysis.expected(c, "nb", command))
    store.r.db.run(
        lambda c: store.r.checkpoints.store.put(
            c, "nb", "operation", {**blocked, "statusSeq": 2, "state": "ready"}
        )
    )
    store.r.db.run(lambda c: analysis.expected(c, "nb", command))
    # A newer active choice is never removed by this old clear request.
    store.r.db.run(lambda c: store.advance(c, "nb", active=None))
    with pytest.raises(NotebookError, match="reference_stale"):
        store.r.db.run(lambda c: analysis.expected(c, "nb", command))


def test_other_language_uninspected_objects_do_not_block_unrelated_run_without_selection(
    store,
):
    seed(store, "r")
    store.r.db.run(lambda c: store.advance(c, "nb", active=None))
    store.r.db.run(lambda c: store.invalidate(c, "nb", "r", "changed"))
    context = store.r.db.run(lambda c: store.context(c, "nb"))
    assert context["guards"]["python"]["objects"] == []
    store.r.db.run(
        lambda c: store.validate_guard(
            c, "nb", context["guards"]["python"], language="python"
        )
    )
    with pytest.raises(NotebookError, match="reference_stale"):
        store.r.db.run(
            lambda c: store.validate_guard(
                c, "nb", context["guards"]["r"], language="r"
            )
        )
