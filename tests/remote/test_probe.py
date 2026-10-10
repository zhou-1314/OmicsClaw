"""The probe script and its tolerant parser.

The parser is where a broken fleet meets this feature, so the tests are
mostly about what it *survives*: markers in noise, a missing END, JSON
with absent or mistyped fields, no object at all.
"""

from __future__ import annotations

from omicsclaw.remote.probe import (
    PROBE_END,
    PROBE_SCRIPT,
    PROBE_START,
    SCRATCH_CANDIDATE_ORDER,
    HostProbe,
    parse_probe_output,
)


def _wrap(json_body: str, *, close: bool = True, noise: str = "") -> str:
    text = noise + PROBE_START + "\n" + json_body + "\n"
    if close:
        text += PROBE_END + "\n"
    return text


class TestScript:
    def test_script_writes_nothing(self):
        # The audit story: a host administrator decodes the blob and
        # finds no writes. Redirection, tee and mkdir are the shapes a
        # write takes in shell; the two >/dev/null forms discard rather
        # than create.
        for line in PROBE_SCRIPT.splitlines():
            code = (
                line.split("#", 1)[0]
                .replace("2>/dev/null", "")
                .replace(">/dev/null", "")
                .replace("2>&1", "")
            )
            assert ">" not in code, line
            assert "tee " not in code
            assert not code.strip().startswith("mkdir")

    def test_every_command_is_best_effort(self):
        # Anything that can fail carries 2>/dev/null or a command -v
        # guard; nproc/getconf/uname/hostname/sed/tr are the only bare
        # calls, and each is wrapped in $( ) whose failure yields "".
        assert PROBE_SCRIPT.count("2>/dev/null") >= 8

    def test_scratch_candidates_agree_with_the_plan_order(self):
        # The script's emission order and the Python preference order
        # name the same list; drift between them is a silent change of
        # which directory jobs land in.
        for marker in ("${SCRATCH:-}", "${WORK:-}", "/scratch/${USER:-}",
                       "$HOME/scratch", "$HOME/.omicsclaw-scratch"):
            assert marker in PROBE_SCRIPT, marker
        assert len(SCRATCH_CANDIDATE_ORDER) == 5


class TestParse:
    def test_full_probe_decodes_every_field(self):
        body = (
            '{"hostname":"n01","platform":"Linux x86_64","nproc":32,'
            '"memMb":257462,"gpus":["GPU 0: A800 (UUID)"],'
            '"commands":{"sbatch":"/usr/bin/sbatch","conda":null,'
            '"module":"/usr/bin/module","uv":null,"sinfo":"/usr/bin/sinfo"},'
            '"slurm":{"partitions":["gpu","cpu"]},'
            '"scratch":[{"path":"/scratch/u","exists":true},'
            '{"path":"/data/u","exists":false}]}'
        )
        probe = parse_probe_output("motd noise\n" + _wrap(body) + "trailing junk")
        assert probe.hostname == "n01"
        assert probe.platform == "Linux x86_64"
        assert probe.nproc == 32
        assert probe.mem_mb == 257462
        assert probe.gpus == ["GPU 0: A800 (UUID)"]
        assert probe.commands["sbatch"] == "/usr/bin/sbatch"
        assert probe.commands["conda"] is None
        assert probe.slurm_partitions == ["gpu", "cpu"]
        assert probe.scratch_root() == "/scratch/u"
        assert probe.parse_error == ""

    def test_markers_inside_noise_only(self):
        probe = parse_probe_output("===OMICSCLAW_PROBE_START=== garbage")
        assert probe.parse_error
        assert probe.nproc is None

    def test_missing_fields_do_not_crash(self):
        probe = parse_probe_output(_wrap("{}"))
        assert probe == HostProbe(raw={}, parse_error="")
        assert probe.scratch_root() == "~"

    def test_no_end_marker_still_parses(self):
        probe = parse_probe_output(_wrap('{"hostname":"n02"}', close=False))
        assert probe.hostname == "n02"
        assert "END marker missing" in probe.parse_error

    def test_broken_json_reports_the_error_and_keeps_defaults(self):
        # Braces present but the body invalid: the JSON error path.
        probe = parse_probe_output(_wrap('{"hostname":"n03",}'))
        assert probe.parse_error.startswith("probe JSON")
        assert probe.nproc is None

    def test_wrong_typed_fields_are_dropped_not_coerced(self):
        probe = parse_probe_output(_wrap('{"nproc":"lots","memMb":true}'))
        assert probe.nproc is None
        assert probe.mem_mb is None

    def test_last_start_marker_wins(self):
        text = _wrap('{"hostname":"first"}') + _wrap('{"hostname":"second"}')
        assert parse_probe_output(text).hostname == "second"

    def test_non_object_json_is_an_error(self):
        assert parse_probe_output(_wrap("[1,2,3]")).parse_error

    def test_scratch_root_prefers_first_existing(self):
        probe = HostProbe(
            scratch_candidates=[("/scratch/u", False), ("~/scratch", True)]
        )
        assert probe.scratch_root() == "~/scratch"

    def test_scratch_root_falls_back_to_the_absolute_home(self):
        # A literal "~" handed to mkdir/sftp means a different thing to
        # each of them; the probe's home is the safe fallback.
        assert HostProbe(home="/home/u").scratch_root() == "/home/u"
        assert HostProbe().scratch_root() == "~"

    def test_as_json_round_trips_through_the_parser(self):
        probe = parse_probe_output(
            _wrap('{"hostname":"n04","nproc":8,"scratch":null}')
        )
        import json

        again = parse_probe_output(_wrap(json.dumps(probe.as_json())))
        assert again.hostname == "n04"
        assert again.nproc == 8
