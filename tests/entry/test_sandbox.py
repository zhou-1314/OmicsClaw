"""Native OS sandbox integration in ``omicsclaw.entry``: config, policy, tools, REPL, and loop."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path

import pytest

from omicsclaw.engine import AgentEngine
from omicsclaw.entry import (
    AppConfig,
    AppConfigError,
    SandboxMode,
    SkillsIndex,
    assembly,
    attach_sessions,
    build_app,
    open_app,
    resolve_app_config,
)
from omicsclaw.entry.desktop.doctor import doctor_report
from omicsclaw.entry.sandbox import (
    EGRESS_GRANTS_FILENAME,
    RequestNetworkAccessTool,
    SandboxBinding,
    bash_policy,
    load_persisted_egress_grants,
    open_sandbox,
    python_policy,
    sandbox_section,
    unstarted_sandbox,
)
from omicsclaw.entry.turn import run_turn
from omicsclaw.sandbox import (
    BackendName,
    CommandOutcome,
    EgressMode,
    Level,
    ProbeReport,
    SandboxConfig,
    SandboxError,
)
from omicsclaw.schema import Message, Role, ToolCall
from omicsclaw.tools import ApprovalMode, BashTool, ToolPolicy, Workspace
from tests.entry.test_cli_repl import repl_over
from tests.entry.test_turn import _Scripted


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path.resolve()


@pytest.fixture(autouse=True)
def scripted_provider(monkeypatch):
    provider = _Scripted()
    monkeypatch.setattr(assembly, "provider_from_env", lambda p, m: provider)
    return provider


class _FakeSandboxManager:
    """Deterministic in-memory SandboxManager double for cross-platform entry tests."""

    def __init__(
        self,
        workspace: Path,
        *,
        config: SandboxConfig | None = None,
        report: ProbeReport | None = None,
        env_provider=None,
    ) -> None:
        self.workspace = Path(workspace)
        self.config = config or SandboxConfig()
        self.report = report or ProbeReport(
            available=True,
            backend=BackendName.BWRAP,
            level=Level.ISOLATED,
            summary="Linux bubblewrap",
        )
        self.env_provider = env_provider
        self.started = False
        self.closed = False
        self.calls: list[tuple[str, str, float]] = []
        self.granted_domains: list[str] = []
        self.proxy = None

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    def allow_domain_for_session(self, domain: str) -> None:
        self.granted_domains.append(domain)

    def status(self):
        from omicsclaw.sandbox import SandboxStatus

        covers = ("bash", "python", "r") if self.config.sandbox_kernels else ("bash",)
        return SandboxStatus(
            active=self.started and not self.closed,
            backend=self.report.backend,
            level=self.report.level,
            egress=self.config.egress,
            summary=self.report.summary,
            covers=covers,
            proxy_url="http://127.0.0.1:51234",
            allowed_domains=self.config.effective_allowed_domains
            | frozenset(self.granted_domains),
            degraded_reason=self.report.reason,
            fix_hint=self.report.fix_hint,
        )

    def as_kernel_spawn_wrapper(self):
        if not self.config.sandbox_kernels:
            return None
        return self

    @property
    def kernel_transport(self) -> str:
        return "ipc"

    def wrap(self, argv, *, cwd, env, extra_read_paths=(), extra_write_paths=()):
        return ["bwrap", "--", *argv], dict(env)

    async def run_bash(self, command: str, cwd: str, timeout: float) -> CommandOutcome:
        self.calls.append((command, cwd, timeout))
        if "exit 3" in command:
            return CommandOutcome(output="sandboxed-out\n", exit_code=3)
        return CommandOutcome(output="from-inside\n", exit_code=0)


@pytest.fixture
def fake_sandbox(monkeypatch):
    instances: list[_FakeSandboxManager] = []
    state = {
        "available": True,
        "level": Level.ISOLATED,
        "backend": BackendName.BWRAP,
        "fail_start": False,
    }

    def fake_probe(preferred=BackendName.AUTO):
        if not state["available"]:
            return ProbeReport(
                available=False,
                backend=BackendName.NONE,
                level=Level.OFF,
                summary="sandbox backend unavailable",
                reason="kernel userns disabled",
                fix_hint="enable userns",
            )
        return ProbeReport(
            available=True,
            backend=state["backend"],
            level=state["level"],
            summary=f"{state['backend'].value} ({state['level'].value})",
        )

    def make_manager(workspace, *, config=None, report=None, env_provider=None):
        if state["fail_start"]:
            raise SandboxError("failed to initialize sandbox")
        mgr = _FakeSandboxManager(
            workspace, config=config, report=report, env_provider=env_provider
        )
        instances.append(mgr)
        return mgr

    import omicsclaw.entry.sandbox as entry_sandbox

    monkeypatch.setattr(entry_sandbox, "probe_sandbox_backend", fake_probe)
    monkeypatch.setattr(entry_sandbox, "SandboxManager", make_manager)
    return state, instances


def _config(workspace: Path, **overrides) -> AppConfig:
    values: dict = {"workspace": workspace, "skills_index": SkillsIndex.OFF}
    values.update(overrides)
    return AppConfig(**values)


def _run(coro, timeout: float = 60.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _bash_call(command: str = "echo hi") -> ToolCall:
    return ToolCall(id="b1", name="bash", arguments=json.dumps({"command": command}))


# ---- configuration -------------------------------------------------------


def test_the_sandbox_is_off_unless_asked_for(workspace):
    config = _config(workspace)

    assert config.sandbox is SandboxMode.OFF
    assert config.sandbox_config() is None
    assert not config.sandbox_required and not config.sandbox_auto_approve


def test_every_sandbox_setting_reaches_the_sandbox_config(workspace):
    config = resolve_app_config(
        argv=[
            "--workspace", str(workspace),
            "--sandbox", "auto",
            "--sandbox-backend", "bwrap",
            "--sandbox-egress", "proxy",
            "--sandbox-egress-groups", "pkg,bio",
            "--sandbox-egress-allow", "api.custom.org",
            "--sandbox-egress-endpoints", "internal.org:8443",
            "--sandbox-allow-domain", "extra.org",
            "--sandbox-allow-private-cidr", "10.0.0.0/8",
            "--sandbox-kernels", "false",
            "--sandbox-mount", "/ref/hg38",
            "--sandbox-mount", "/ref/mm10",
            "--sandbox-required", "true",
            "--sandbox-auto-approve", "yes",
        ],
        env={},
    )  # fmt: skip

    sandbox = config.sandbox_config()

    assert sandbox is not None
    assert sandbox.backend == BackendName.BWRAP
    assert sandbox.egress == EgressMode.PROXY
    assert sandbox.egress_groups == ("pkg", "bio")
    assert "api.custom.org" in sandbox.egress_allow
    assert "extra.org" in sandbox.egress_allow
    assert sandbox.egress_endpoints == ("internal.org:8443",)
    assert sandbox.allow_private_cidr == ("10.0.0.0/8",)
    assert sandbox.sandbox_kernels is False
    assert sandbox.read_only_mounts == (Path("/ref/hg38"), Path("/ref/mm10"))
    assert config.sandbox_required and config.sandbox_auto_approve


def test_legacy_docker_mode_maps_cleanly_to_native_sandbox(workspace):
    config = resolve_app_config(
        argv=["--workspace", str(workspace)],
        env={
            "OMICSCLAW_SANDBOX": "DOCKER",
            "OMICSCLAW_SANDBOX_MOUNTS": "/a:/b",
            "OMICSCLAW_SANDBOX_REQUIRED": "0",
            "OMICSCLAW_SANDBOX_AUTO_APPROVE": "on",
        },
    )

    assert config.sandbox is SandboxMode.AUTO
    assert config.sandbox_mounts == (Path("/a"), Path("/b"))
    assert not config.sandbox_required
    assert config.sandbox_auto_approve
    assert config.sandbox_config() is not None


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OMICSCLAW_SANDBOX", "podman-invalid"),
        ("OMICSCLAW_SANDBOX_BACKEND", "lxc"),
        ("OMICSCLAW_SANDBOX_EGRESS", "invalid-egress"),
        ("OMICSCLAW_SANDBOX_REQUIRED", "maybe"),
        ("OMICSCLAW_SANDBOX_AUTO_APPROVE", "2"),
        ("OMICSCLAW_SANDBOX_BOOTSTRAP_TIMEOUT_S", "ten minutes"),
    ],
)
def test_an_unreadable_setting_is_refused_rather_than_defaulted(
    workspace, variable, value
):
    with pytest.raises(AppConfigError, match=variable):
        resolve_app_config(argv=["--workspace", str(workspace)], env={variable: value})


# ---- starting it ---------------------------------------------------------


def test_open_sandbox_starts_native_backend_and_cleans_up(workspace, fake_sandbox):
    _state, instances = fake_sandbox

    async def main():
        binding = await open_sandbox(_config(workspace, sandbox=SandboxMode.AUTO))
        try:
            return binding
        finally:
            await binding.aclose()

    binding = _run(main())

    assert binding.active and not binding.degraded
    assert binding.isolates_network
    assert binding.backend == BackendName.BWRAP
    assert binding.effective_level == Level.ISOLATED
    assert len(instances) == 1 and instances[0].started and instances[0].closed


def test_a_sandbox_that_cannot_start_degrades_loudly(workspace, fake_sandbox, caplog):
    state, _instances = fake_sandbox
    state["available"] = False

    with caplog.at_level(logging.WARNING, logger="omicsclaw.entry"):
        binding = _run(open_sandbox(_config(workspace, sandbox=SandboxMode.AUTO)))

    assert binding.degraded and not binding.active
    assert "unavailable" in binding.unavailable
    assert binding.manager is None
    assert any("without isolation" in record.message for record in caplog.records)


def test_a_required_sandbox_that_cannot_start_refuses_to_run(workspace, fake_sandbox):
    state, _instances = fake_sandbox
    state["fail_start"] = True

    with pytest.raises(SandboxError, match="sandbox_required"):
        _run(
            open_sandbox(
                _config(workspace, sandbox=SandboxMode.AUTO, sandbox_required=True)
            )
        )


def test_off_is_an_inactive_binding_with_no_prompt_section(workspace):
    binding = _run(open_sandbox(_config(workspace)))

    assert binding == SandboxBinding()
    assert sandbox_section(binding) is None


# ---- build_app without open_app ------------------------------------------


def test_build_app_off_leaves_bash_and_the_prompt_as_they_were(workspace):
    app = build_app(_config(workspace))

    assert app.sandbox is not None and app.sandbox.mode is SandboxMode.OFF
    assert "Execution sandbox" not in app.prompt.render().system_prompt


def test_build_app_cannot_start_a_sandbox_and_says_so(workspace):
    app = build_app(_config(workspace, sandbox=SandboxMode.AUTO))
    rendered = app.prompt.render().system_prompt

    assert app.sandbox.degraded
    assert "requested but is not running" in rendered
    assert "use open_app" in rendered
    assert app.registry.policy_for("bash").approval_mode is ApprovalMode.ASK


def test_build_app_refuses_when_the_sandbox_is_required(workspace):
    with pytest.raises(SandboxError, match="sandbox_required"):
        build_app(_config(workspace, sandbox=SandboxMode.AUTO, sandbox_required=True))


# ---- the approval policy: resolution -------------------------------------


_ASK = ToolPolicy(approval_mode=ApprovalMode.ASK)


def _active(
    *,
    level: Level = Level.ISOLATED,
    egress: EgressMode = EgressMode.PROXY,
    sandbox_kernels: bool = True,
) -> SandboxBinding:
    return SandboxBinding(
        mode=SandboxMode.AUTO,
        environment=object(),
        config=SandboxConfig(egress=egress, sandbox_kernels=sandbox_kernels),
        backend=BackendName.BWRAP if level == Level.ISOLATED else BackendName.NETNS,
        level=level,
        egress=egress,
    )


@pytest.mark.parametrize(
    ("binding", "auto", "expected"),
    [
        (
            _active(level=Level.ISOLATED, egress=EgressMode.PROXY),
            True,
            ApprovalMode.AUTO,
        ),
        (
            _active(level=Level.ISOLATED, egress=EgressMode.NONE),
            True,
            ApprovalMode.AUTO,
        ),
        (
            _active(level=Level.ISOLATED, egress=EgressMode.PROXY),
            False,
            ApprovalMode.ASK,
        ),
        (
            _active(level=Level.NET_ONLY, egress=EgressMode.PROXY),
            True,
            ApprovalMode.ASK,
        ),
        (_active(level=Level.ISOLATED, egress=EgressMode.OPEN), True, ApprovalMode.ASK),
        (
            SandboxBinding(mode=SandboxMode.AUTO, unavailable="unavailable"),
            True,
            ApprovalMode.ASK,
        ),
        (SandboxBinding(), True, ApprovalMode.ASK),
    ],
    ids=[
        "isolated+proxy+auto",
        "isolated+none+auto",
        "not-asked",
        "net-only-never-auto-approves",
        "open-network",
        "degraded",
        "off",
    ],
)
def test_bash_and_python_are_relaxed_only_in_isolated_network_controlled_sandbox(
    binding, auto, expected
):
    assert bash_policy(binding, _ASK, auto_approve=auto).approval_mode is expected
    assert python_policy(binding, _ASK, auto_approve=auto).approval_mode is expected


def test_python_is_not_relaxed_when_sandbox_kernels_is_false():
    binding = _active(
        level=Level.ISOLATED, egress=EgressMode.PROXY, sandbox_kernels=False
    )
    assert (
        bash_policy(binding, _ASK, auto_approve=True).approval_mode is ApprovalMode.AUTO
    )
    assert (
        python_policy(binding, _ASK, auto_approve=True).approval_mode
        is ApprovalMode.ASK
    )


# ---- RequestNetworkAccessTool & /sandbox REPL & Doctor -------------------


def test_request_network_access_tool_grants_and_persists_and_blocks_denylist(
    workspace, fake_sandbox
):
    from omicsclaw.tools import use_tool_context

    _state, instances = fake_sandbox

    async def main():
        binding = await open_sandbox(_config(workspace, sandbox=SandboxMode.AUTO))
        tool = RequestNetworkAccessTool(binding)
        try:
            with use_tool_context(approval=lambda _req: True):
                # 1. Denylisted domain is rejected
                with pytest.raises(ValueError, match="denylist"):
                    await tool.execute(
                        {"domain": "pastebin.com", "reason": "exfil test"}
                    )

                # 2. Session grant succeeds
                granted = await tool.execute(
                    json.dumps(
                        {
                            "domain": "api.custom-biobank.org",
                            "reason": "fetch cohort metadata",
                        }
                    )
                )
                assert "Granted" in granted
                assert "api.custom-biobank.org" in instances[0].granted_domains

                # 3. Persistent grant saves to .omicsclaw/sandbox_egress_grants.json
                persisted = await tool.execute(
                    {
                        "domain": "portal.genomics.edu",
                        "reason": "reference panel",
                        "scope": "always",
                    }
                )
                assert "persisted in workspace" in persisted
        finally:
            await binding.aclose()

    _run(main())
    grants_file = workspace / ".omicsclaw" / EGRESS_GRANTS_FILENAME
    assert grants_file.is_file()
    assert "portal.genomics.edu" in load_persisted_egress_grants(grants_file)


def test_repl_sandbox_command_and_doctor_report(workspace, fake_sandbox):
    async def main():
        app = attach_sessions(
            await open_app(_config(workspace, sandbox=SandboxMode.AUTO)),
            abandon_grace_s=None,
        )
        try:
            doc = doctor_report(app)
            repl, _source, buffer = repl_over(
                app,
                [
                    "/sandbox status",
                    "/sandbox allow api.biobank.org --always",
                    "/sandbox allow pastebin.com",
                    "/exit",
                ],
            )
            await repl.run()
            return doc, buffer.getvalue()
        finally:
            await app.aclose()

    doc, printed = _run(main())
    sbx_check = next(c for c in doc["checks"] if c["name"] == "sandbox")
    assert sbx_check["status"] == "ok"
    assert "bwrap" in sbx_check["summary"]
    assert "Sandbox: active · backend=bwrap" in printed
    assert "Allowed outbound egress to 'api.biobank.org'" in printed
    assert "Refused: 'pastebin.com' is on the exfiltration denylist." in printed


# ---- through the main loop -----------------------------------------------


def test_the_react_loop_runs_bash_inside_the_sandbox(
    workspace, fake_sandbox, monkeypatch
):
    provider = _Scripted(
        Message(role=Role.ASSISTANT, tool_calls=(_bash_call("uname -n; exit 3"),)),
        Message(role=Role.ASSISTANT, content="ran it"),
    )
    monkeypatch.setattr(assembly, "provider_from_env", lambda p, m: provider)
    config = _config(workspace, sandbox=SandboxMode.AUTO, sandbox_auto_approve=True)

    async def main():
        app = await open_app(config)
        app = dataclasses.replace(
            app, engine=AgentEngine(provider, app.registry, config.engine_config())
        )
        try:
            return await run_turn(app, (), "run something")
        finally:
            await app.aclose()

    outcome = _run(main())

    assert outcome.reply == "ran it"
    observation = next(m for m in outcome.history if m.role is Role.TOOL)
    assert observation.content.startswith("[exit status 3]")
    system = provider.seen[0][0].content
    assert "## Execution sandbox" in system
    assert "request_network_access" in system


def test_the_prompt_section_sits_between_tool_guidance_and_the_environment(workspace):
    app = build_app(_config(workspace, sandbox=SandboxMode.AUTO))
    rendered = app.prompt.render().system_prompt

    assert (
        rendered.index("## Tool guidance")
        < rendered.index("## Execution sandbox")
        < rendered.index("## Environment")
    )


def test_a_failure_after_the_sandbox_started_closes_it(workspace, fake_sandbox):
    _state, instances = fake_sandbox
    twice = [BashTool(Workspace(workspace)), BashTool(Workspace(workspace))]

    with pytest.raises(Exception, match="already registered"):
        _run(open_app(_config(workspace, sandbox=SandboxMode.AUTO), tools=twice))

    assert len(instances) == 1 and instances[0].closed


def test_unstarted_sandbox_is_off_for_an_off_config(workspace):
    assert unstarted_sandbox(_config(workspace)) == SandboxBinding()


def test_private_mirror_warning_when_unlisted(
    workspace, fake_sandbox, monkeypatch, caplog
):
    monkeypatch.setenv("PIP_INDEX_URL", "http://10.0.1.50:8081/repository/pypi/simple")
    with caplog.at_level(logging.WARNING, logger="omicsclaw.entry"):
        binding = _run(open_sandbox(_config(workspace, sandbox=SandboxMode.AUTO)))
        _run(binding.aclose())
    assert any("10.0.1.50:8081" in r.message for r in caplog.records)


def test_private_mirror_diagnostic_uses_supplied_environment_without_logging_credentials(
    monkeypatch, caplog
):
    from types import SimpleNamespace
    from omicsclaw.entry.sandbox import _warn_unlisted_private_mirrors

    monkeypatch.setattr(Path, "is_file", lambda self: False)
    config = SimpleNamespace(egress_endpoints=(), allow_private_cidr=())
    _warn_unlisted_private_mirrors(
        config,
        environment={
            "PIP_INDEX_URL": "https://user-secret:password-secret@10.2.3.4:8443/simple"
        },
    )
    assert "10.2.3.4:8443" in caplog.text
    assert "user-secret" not in caplog.text and "password-secret" not in caplog.text
