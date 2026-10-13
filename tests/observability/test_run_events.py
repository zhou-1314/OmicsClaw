import asyncio

from omicsclaw.observability import Telemetry
from omicsclaw.observability.run_events import RunEventObserver
from tests.observability._support import RecordingMeter, RecordingTracer


def test_durable_events_carry_ids_without_content():
    tracer, meter = RecordingTracer(), RecordingMeter()
    observer = RunEventObserver(tracer, meter)
    observer(
        {
            "seq": 7,
            "kind": "verification",
            "event": "created",
            "object_id": "v-1",
            "run_id": "r-1",
            "payload": {
                "verification_id": "v-1",
                "artifact_id": "a-1",
                "verdict": "fail",
                "artifact_version": 2,
                "check": "json_schema",
                "details": "private patient content",
                "evidence_ref": "/secret/input.json",
            },
        }
    )
    span = tracer.spans[0]
    assert span.attributes["run.id"] == "r-1"
    assert span.attributes["verification.id"] == "v-1"
    assert span.attributes["artifact.id"] == "a-1"
    assert span.attributes["artifact.version"] == 2
    assert span.attributes["event.name"] == "verification.finished"
    assert span.attributes["event.sequence"] == 7
    assert span.ended == 1
    assert "private" not in repr(span.attributes)
    assert "secret" not in repr(span.attributes)
    assert meter.counts[0][2] == {
        "event.name": "verification.finished",
        "object.kind": "verification",
    }


def test_inactive_telemetry_has_no_store_observer():
    assert Telemetry().run_event_sink() is None


def test_enabled_telemetry_provides_a_store_observer():
    tracer = RecordingTracer()
    observer = Telemetry(tracer=tracer).run_event_sink()
    observer(
        {"seq": 1, "kind": "run", "event": "created", "payload": {"run_id": "r-2"}}
    )
    assert tracer.spans[0].attributes["run.id"] == "r-2"


def test_run_scope_carries_the_domain_id():
    tracer = RecordingTracer()

    async def scenario():
        async with Telemetry(tracer=tracer).run(session_id="s-1", run_id="r-1"):
            pass

    asyncio.run(scenario())
    assert tracer.spans[0].attributes["run.id"] == "r-1"


def test_exporter_failure_does_not_break_execution(caplog):
    class BrokenTracer:
        def start_span(self, *args, **kwargs):
            raise OSError("telemetry unavailable")

    observer = RunEventObserver(BrokenTracer(), RecordingMeter())
    observer(
        {
            "kind": "job",
            "event": "updated",
            "payload": {"run_id": "r", "status": "failed"},
        }
    )
    assert "run event telemetry failed" in caplog.text


def test_event_vocabulary_is_bounded_for_export():
    tracer = RecordingTracer()
    observer = RunEventObserver(tracer, RecordingMeter())
    observer({"kind": "private cohort", "event": "private value", "payload": {}})
    assert tracer.spans == []


def test_malformed_event_vocabulary_does_not_escape():
    tracer = RecordingTracer()
    observer = RunEventObserver(tracer, RecordingMeter())
    observer({"kind": [], "event": [], "payload": {}})
    observer({"kind": "kernel", "event": "updated", "payload": {"status": []}})
    assert len(tracer.spans) == 1
    assert "kernel.status" not in tracer.spans[0].attributes
