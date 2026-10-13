from omicsclaw.runs.store import RunStore
from omicsclaw.runs.models import Run
def test_schema_and_events():
    store=RunStore(":memory:")
    assert store.schema_version == 1
    run=Run(); store.save(run)
    assert store.load(run.run_id).run_id == run.run_id
    assert store.events(run.run_id)[0]["event"] == "created"

def test_replay_rejects_reverse_and_filters_legacy_fields():
    store=RunStore(":memory:")
    run=Run(); store.save(run)
    run.status="running"; store.save(run)
    assert store.replay(run.run_id).status == "running"
    run.status="created"
    import pytest
    with pytest.raises(ValueError):
        store.save(run)
