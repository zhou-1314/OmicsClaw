"""The remote tool family over a fake plane, inside a tool context.

Each tool's contract with the model: schema problems are
:class:`~omicsclaw.tools.function_tool.ToolArgumentError`, approvals
happen before anything runs and quote the bytes, a remote command's
non-zero exit is a result, and ``remote_fetch`` declines past the
threshold with a reference instead of an error.
"""

from __future__ import annotations

import pytest

from omicsclaw.tools import (
    AnswerStatus,
    ApprovalDenied,
    ApprovalRequest,
    QuestionAnswer,
    QuestionRequest,
    use_tool_context,
)
from omicsclaw.tools.builtin.remote import (
    AskAboutHostTool,
    ExecOutcome,
    FetchOutcome,
    RemoteCancelTool,
    RemoteExecTool,
    RemoteFetchTool,
    RemoteStatusTool,
    RemoteSubmitTool,
    StatusReport,
    SubmitOutcome,
    remote_tools,
)
from omicsclaw.tools.function_tool import ToolArgumentError

from tests.remote._fakes import FakePlane, run


class _Recorder:
    def __init__(self) -> None:
        self.approvals: list[ApprovalRequest] = []
        self.questions: list[QuestionRequest] = []


def _context(recorder: _Recorder, *, approve: bool = True):
    def approval(request: ApprovalRequest):
        recorder.approvals.append(request)
        return approve

    def question(request: QuestionRequest):
        recorder.questions.append(request)
        return QuestionAnswer(AnswerStatus.ANSWERED, reply="gpu-long")

    return use_tool_context(
        approval=approval,
        progress=lambda update: None,
        values={},
        question=question,
    )


def test_the_family_has_six_tools_with_stable_names():
    names = [tool.name for tool in remote_tools(FakePlane())]
    assert names == [
        "remote_exec",
        "remote_submit",
        "remote_status",
        "remote_cancel",
        "remote_fetch",
        "ask_about_host",
    ]


class TestRemoteExec:
    def test_output_comes_back_with_the_exit_status(self):
        plane = FakePlane()
        plane.exec_answer = ExecOutcome(output="queue is empty\n", exit_code=0, host="h")
        with _context(_Recorder()):
            result = run(RemoteExecTool(plane).execute(
                '{"host": "h", "command": "squeue -u me", "intent": "check queue"}'
            ))
        assert "queue is empty" in result
        assert plane.calls[0] == ("exec", ("h", "squeue -u me", 120.0))

    def test_nonzero_exit_is_a_result_not_an_error(self):
        plane = FakePlane()
        plane.exec_answer = ExecOutcome(output="slurm_load_jobs error", exit_code=1)
        with _context(_Recorder()):
            result = run(RemoteExecTool(plane).execute(
                '{"host": "h", "command": "x", "intent": "i"}'
            ))
        assert result.startswith("[exit status 1]")

    def test_timeout_gets_the_banner(self):
        plane = FakePlane()
        plane.exec_answer = ExecOutcome(output="partial", exit_code=137, timed_out=True)
        with _context(_Recorder()):
            result = run(RemoteExecTool(plane).execute(
                '{"host": "h", "command": "x", "intent": "i", "timeout_seconds": 30}'
            ))
        assert "[TIMEOUT 30s" in result
        assert "may still be running" in result

    def test_approval_quotes_the_whole_command_and_the_intent(self):
        plane = FakePlane()
        recorder = _Recorder()
        with _context(recorder):
            run(RemoteExecTool(plane).execute(
                '{"host": "h", "command": "rm -rf /data/junk; echo done",'
                ' "intent": "clean the junk folder"}'
            ))
        request = recorder.approvals[0]
        assert request.tool_name == "remote_exec"
        assert "clean the junk folder" in request.reason
        assert "rm -rf /data/junk; echo done" in request.reason
        assert "on h" in request.reason

    def test_a_declined_approval_runs_nothing(self):
        plane = FakePlane()
        recorder = _Recorder()
        with _context(recorder, approve=False):
            with pytest.raises(ApprovalDenied):
                run(RemoteExecTool(plane).execute(
                    '{"host": "h", "command": "x", "intent": "i"}'
                ))
        assert plane.calls == []

    def test_timeout_is_capped_at_600(self):
        plane = FakePlane()
        with _context(_Recorder()):
            run(RemoteExecTool(plane).execute(
                '{"host": "h", "command": "x", "intent": "i", "timeout_seconds": 99999}'
            ))
        assert plane.calls[0][1][2] == 600.0

    def test_missing_intent_is_a_correctable_error(self):
        with _context(_Recorder()):
            with pytest.raises(ToolArgumentError, match="schema"):
                run(RemoteExecTool(FakePlane()).execute(
                    '{"host": "h", "command": "x"}'
                ))

    def test_bad_host_is_refused_before_anything_runs(self):
        plane = FakePlane()
        with pytest.raises(ToolArgumentError, match="one word"):
            run(RemoteExecTool(plane).execute(
                '{"host": "h; rm -rf /", "command": "x", "intent": "i"}'
            ))
        assert plane.calls == []


class TestRemoteSubmit:
    def test_returns_the_handle_and_the_ref(self):
        plane = FakePlane()
        with _context(_Recorder()):
            result = run(RemoteSubmitTool(plane).execute(
                '{"host": "h", "command": "python train.py", "intent": "train",'
                ' "outputs": ["model.pt"]}'
            ))
        assert "job_ref 7" in result
        assert "/scratch/w" in result
        kind, (host, command, kwargs) = plane.calls[0]
        assert kind == "submit"
        assert host == "h"
        assert kwargs["outputs"] == ("model.pt",)

    def test_scheduler_choice_travels(self):
        plane = FakePlane()
        with _context(_Recorder()):
            run(RemoteSubmitTool(plane).execute(
                '{"host": "h", "command": "x", "intent": "i", "scheduler": "slurm"}'
            ))
        assert plane.calls[0][1][2]["scheduler"] == "slurm"

    def test_unknown_scheduler_is_correctable(self):
        with _context(_Recorder()):
            with pytest.raises(ToolArgumentError):
                run(RemoteSubmitTool(FakePlane()).execute(
                    '{"host": "h", "command": "x", "intent": "i",'
                    ' "scheduler": "pbs"}'
                ))

    def test_inputs_travel_as_src_dst_pairs(self):
        plane = FakePlane()
        with _context(_Recorder()):
            run(RemoteSubmitTool(plane).execute(
                '{"host": "h", "command": "python train.py", "intent": "i",'
                ' "inputs": [{"src": "data/counts.csv", "dst": "counts.csv"}]}'
            ))
        assert plane.calls[0][1][2]["inputs"] == (
            ("data/counts.csv", "counts.csv"),
        )

    @pytest.mark.parametrize(
        "inputs_json",
        [
            '["data/counts.csv"]',
            '[{"src": "data/counts.csv"}]',
            '[{"src": "", "dst": "counts.csv"}]',
            '[{"src": "a", "dst": "b", "extra": 1}]',
            '"data/counts.csv"',
        ],
    )
    def test_inputs_must_be_src_dst_objects(self, inputs_json):
        with _context(_Recorder()):
            with pytest.raises(ToolArgumentError):
                run(RemoteSubmitTool(FakePlane()).execute(
                    '{"host": "h", "command": "x", "intent": "i",'
                    f' "inputs": {inputs_json}}}'
                ))


class TestStatusAndCancel:
    def test_status_renders_state_and_exit_code(self):
        plane = FakePlane()
        plane.status_answer = StatusReport(state="failed", exit_code=2, detail="oom")
        with _context(_Recorder()):
            result = run(RemoteStatusTool(plane).execute('{"job_ref": "7"}'))
        assert "failed" in result
        assert "2" in result
        assert "oom" in result

    def test_cancel_reports_what_the_host_said(self):
        plane = FakePlane()
        plane.cancel_answer = StatusReport(state="canceled")
        with _context(_Recorder()):
            result = run(RemoteCancelTool(plane).execute('{"job_ref": "7"}'))
        assert "canceled" in result

    @pytest.mark.parametrize("tool_call", ["{}", '{"job_ref": ""}'])
    def test_a_ref_is_required(self, tool_call):
        with _context(_Recorder()):
            with pytest.raises(ToolArgumentError):
                run(RemoteStatusTool(FakePlane()).execute(tool_call))


class TestRemoteFetch:
    def test_downloaded_paths_are_listed(self):
        plane = FakePlane()
        plane.fetch_answer = FetchOutcome(downloaded=("/w/artifacts/remote/out.h5ad",))
        with _context(_Recorder()):
            result = run(RemoteFetchTool(plane).execute(
                '{"source": "7", "dest": "artifacts/remote"}'
            ))
        assert "1 file(s)" in result
        assert "/w/artifacts/remote/out.h5ad" in result

    def test_over_threshold_says_where_the_data_stayed(self):
        plane = FakePlane()
        plane.fetch_answer = FetchOutcome(
            remote_ref="remote://h/scratch/w/big.h5ad",
            skipped_reason="123456789 bytes exceeds the 100 MB threshold",
        )
        with _context(_Recorder()):
            result = run(RemoteFetchTool(plane).execute('{"source": "7"}'))
        assert "remote://h/scratch/w/big.h5ad" in result
        assert "has not travelled" in result

    def test_max_mb_travels_to_the_plane(self):
        plane = FakePlane()
        with _context(_Recorder()):
            run(RemoteFetchTool(plane).execute(
                '{"source": "7", "max_mb": 5}'
            ))
        assert plane.calls[0] == ("fetch", ("7", "artifacts/remote", 5.0))


class TestAskAboutHost:
    def test_the_question_is_asked_and_the_answer_recorded(self):
        plane = FakePlane()
        plane.card = None
        recorder = _Recorder()
        with _context(recorder):
            result = run(AskAboutHostTool(plane).execute(
                '{"host_alias": "hpc1", "question": "which partition for A100s?"}'
            ))
        assert len(recorder.questions) == 1
        assert "hpc1" in recorder.questions[0].question
        assert "which partition" in recorder.questions[0].question
        assert plane.notes == [("hpc1", "which partition for A100s?", "gpu-long")]
        assert "gpu-long" in result

    def test_known_host_context_is_in_the_question(self):
        from omicsclaw.tools.builtin.remote import HostCard

        plane = FakePlane()
        plane.card = HostCard(
            alias="hpc1", hostname="n01", slurm_partitions=("gpu", "cpu")
        )
        recorder = _Recorder()
        with _context(recorder):
            run(AskAboutHostTool(plane).execute(
                '{"host_alias": "hpc1", "question": "account?"}'
            ))
        assert "n01" in recorder.questions[0].question
        assert "gpu, cpu" in recorder.questions[0].question

    def test_no_answer_is_reported_not_recorded(self):
        plane = FakePlane()

        def no_answer(request: QuestionRequest):
            return QuestionAnswer(AnswerStatus.NO_ANSWER, reason="nobody")

        with use_tool_context(
            approval=lambda r: True,
            progress=lambda u: None,
            values={},
            question=no_answer,
        ):
            result = run(AskAboutHostTool(plane).execute(
                '{"host_alias": "hpc1", "question": "q"}'
            ))
        assert "No answer" in result
        assert plane.notes == []

    def test_a_blank_question_is_correctable(self):
        with _context(_Recorder()):
            with pytest.raises(ToolArgumentError):
                run(AskAboutHostTool(FakePlane()).execute(
                    '{"host_alias": "hpc1", "question": "  "}'
                ))
