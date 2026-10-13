from concurrent.futures import ThreadPoolExecutor

from omicsclaw.runs.models import Kernel
from omicsclaw.runs.projections import ensure_session_run
from omicsclaw.runs.store import RunStore


def test_legacy_projection_concurrent_open_is_idempotent(tmp_path):
    path = tmp_path / "runs.db"
    RunStore(path).db.close()

    def project(_):
        store = RunStore(path)
        try:
            return ensure_session_run(store, "stable").run_id
        finally:
            store.db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(project, range(8))) == {"legacy:stable"}
    reopened = RunStore(path)
    assert len(reopened.events()) == 1
    assert reopened.load("legacy:stable").status == "unknown"


def test_shared_kernel_links_merge_across_independent_connections(tmp_path):
    path = tmp_path / "runs.db"
    one, two = RunStore(path), RunStore(path)
    one.save(Kernel(kernel_id="real", status="ready", run_id="one", run_ids=["one"]))
    two.save(Kernel(kernel_id="real", status="ready", run_id="two", run_ids=["two"]))
    assert one.load("real").run_ids == ["one", "two"]
    assert one.load("real").run_id == "one"
