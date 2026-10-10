"""Command construction for the SSH transport: pure argv, no process.

The construction half of ``omicsclaw/remote/ssh.py``. Everything here is
a pure function over strings, so every property of the wire — the fixed
option set, the ``--`` separator, the closed host alphabet, the quoting
of paths this package splices — is pinned without a network.
"""

from __future__ import annotations

import base64
import os

import pytest

from omicsclaw.remote.ssh import (
    RemoteHostRefused,
    SSH_OPTIONS,
    build_sftp_argv,
    build_sftp_batch,
    build_ssh_argv,
    encode_login_script,
    sftp_quote,
    shell_quote,
    validate_host,
)


class TestSshArgv:
    def test_options_come_first_and_match_the_plan(self):
        argv = build_ssh_argv("hpc1", "echo hi")
        assert argv[0] == "ssh"
        # The option block is exactly the plan's list, verbatim.
        assert argv[1 : 1 + len(SSH_OPTIONS)] == list(SSH_OPTIONS)

    def test_separator_precedes_the_destination(self):
        argv = build_ssh_argv("hpc1", "echo hi")
        assert argv.index("--") == 1 + len(SSH_OPTIONS)
        assert argv[argv.index("--") + 1] == "hpc1"

    def test_command_is_one_argv_element(self):
        argv = build_ssh_argv("hpc1", "rm -rf /; echo done")
        assert argv[-1] == "rm -rf /; echo done"

    def test_batchmode_and_forwardagent_are_pinned(self):
        flat = build_ssh_argv("h", "true")
        assert "BatchMode=yes" in flat
        assert "ForwardAgent=no" in flat

    def test_control_path_carries_the_local_user_token(self):
        # /tmp is shared: without the uid, two local users multiplexing
        # to one destination collide on a 0600 socket and the second
        # reads the collision as "connection failed".
        flat = build_ssh_argv("h", "true")
        control = next(
            item for item in flat if item.startswith("ControlPath=")
        )
        assert control == f"ControlPath=/tmp/omicsclaw-mux-u{os.getuid()}-%C"

    @pytest.mark.parametrize(
        "host",
        ["ao-server", "gpu.lab.uni.edu", "10.0.0.7", "a_b-c.d", "h1"],
    )
    def test_valid_hosts_pass(self, host):
        assert validate_host(host) == host

    @pytest.mark.parametrize(
        "host",
        [
            "",
            "two words",
            "host;rm",
            "$(reboot)",
            "`id`",
            "a|b",
            "a\nb",
            "a\tb",
            "-oProxyCommand=evil",
            "-host",
            "hpc\n",
            "привет",
            "h/w",
        ],
    )
    def test_malicious_or_illformed_hosts_are_refused(self, host):
        with pytest.raises(RemoteHostRefused):
            validate_host(host)
        with pytest.raises(RemoteHostRefused):
            build_ssh_argv(host, "true")
        with pytest.raises(RemoteHostRefused):
            build_sftp_argv(host)


class TestSftp:
    def test_argv_reads_batch_from_stdin(self):
        argv = build_sftp_argv("hpc1")
        assert argv[0] == "sftp"
        assert argv[-1] == "hpc1"
        assert argv[argv.index("-b") + 1] == "-"

    def test_batch_puts_gets_then_exit(self):
        batch = build_sftp_batch(
            puts=[("/tmp/a", "/scratch/w/a")],
            gets=[("/scratch/w/out.h5ad", "/tmp/out.h5ad")],
        )
        lines = batch.splitlines()
        assert lines[0] == 'put -- "/tmp/a" "/scratch/w/a"'
        assert lines[1] == 'get -- "/scratch/w/out.h5ad" "/tmp/out.h5ad"'
        assert lines[-1] == "exit"

    def test_paths_with_spaces_and_quotes_survive(self):
        batch = build_sftp_batch(puts=[("/tmp/my dir/f \"n\".txt", "/w/x")])
        assert 'put -- "/tmp/my dir/f \\"n\\".txt" "/w/x"' in batch

    def test_quotes_are_escaped_not_dropped(self):
        assert sftp_quote('a"b\\c') == '"a\\"b\\\\c"'

    def test_empty_batch_is_just_exit(self):
        assert build_sftp_batch().strip() == "exit"


class TestShellQuote:
    def test_plain_path_is_single_quoted(self):
        assert shell_quote("/scratch/w") == "'/scratch/w'"

    def test_single_quote_inside_is_escaped(self):
        # 'a'b' becomes '\'' — the only character that needs it.
        assert shell_quote("a'b") == "'a'\\''b'"


class TestLoginScript:
    def test_payload_is_base64_that_decodes_to_the_script(self):
        wrapped = encode_login_script("echo 'hello world'\n")
        assert wrapped.startswith("echo ")
        assert "| base64 -d | bash -l" in wrapped
        b64 = wrapped.split("echo ", 1)[1].split(" |", 1)[0]
        assert base64.b64decode(b64).decode() == "echo 'hello world'\n"
