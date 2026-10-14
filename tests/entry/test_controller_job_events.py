from types import SimpleNamespace
import pytest
from omicsclaw.memory.database import Database
from omicsclaw.entry.desktop.jobs_manager import JobRecord, JobStore
from omicsclaw.entry.desktop.turn_submission import DesktopIngressError


def test_job_event_page_uses_store_terminal_and_detects_retention_gap():
    from omicsclaw.entry.desktop.job_service import job_event_page

    database = Database(":memory:")
    store = JobStore(database)
    store.insert_job(JobRecord(id="j", status="succeeded"))
    store.append_event("j", 1, "job.done", {"status": "succeeded"})
    jobs = SimpleNamespace(store=store, get_job=store.get_job)
    page = job_event_page(jobs, "j", 0)
    assert page["complete"] is True and page["nextCursor"] == 1
    assert page["job"]["status"] == "succeeded"
    database.run(
        lambda conn: conn.execute("UPDATE job_events SET seq=10 WHERE job_id='j'")
    )
    assert job_event_page(jobs, "j", 1)["gap"] is True
    assert job_event_page(jobs, "j", 10)["complete"] is True
    with pytest.raises(DesktopIngressError):
        job_event_page(jobs, "j", -1)
    database.close()
