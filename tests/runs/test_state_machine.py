from omicsclaw.runs.models import transition
import pytest
def test_idempotent_and_terminal_transitions():
    assert transition("running","running") == "running"
    assert transition("running","succeeded") == "succeeded"
    with pytest.raises(ValueError):
        transition("succeeded","running")
