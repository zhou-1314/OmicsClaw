"""The ``manage_environments`` tool's own seams, with a fake supply.

The tool is a leaf (``tests/tools/test_tools_is_a_leaf_layer.py``
enforces the imports; these tests stay in its spirit): what it owns is
argument validation, the mirror-whitelist boundary, the approval gate
on exactly the install action, and the rendering of the seam's answers.
The supply itself belongs to ``tests/envsupply``; nothing here runs a
process.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from omicsclaw.tools.base import ApprovalMode, RiskLevel
from omicsclaw.tools.builtin.manage_env import (
    MANAGE_ENV_SCHEMA,
    TOOL_NAME,
    ManageEnvironmentsTool,
)
from omicsclaw.tools.context import (
    ApprovalDecision,
    ApprovalRequest,
    use_tool_context,
)
from omicsclaw.tools.function_tool import ToolArgumentError


class _RecordingSupply:
    """The EnvironmentSupply double: three methods, called with what the
    tool sent, answering small fixed reports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_environments(self):
        self.calls.append(("list", {}))
        return [
            {
                "name": "main",
                "env_dir": "~/.omicsclaw/envs/main",
                "interpreter": "~/.omicsclaw/envs/main/bin/python",
                "exists": True,
            }
        ]

    async def probe_environment(self, *, env="main", interpreter=None):
        self.calls.append(("probe", {"env": env, "interpreter": interpreter}))
        return {
            "env": env,
            "interpreter": interpreter or "~/.omicsclaw/envs/main/bin/python",
            "exists": True,
            "ready": False,
            "missing": ["ipykernel", "scanpy"],
        }

    async def install_environment(self, *, env="main", packages=(), on_output=None):
        self.calls.append(("install", {"env": env, "packages": tuple(packages)}))
        if on_output is not None:
            on_output("===OMICSCLAW_ENV_STEP:packages===")
        return {
            "env": env,
            "interpreter": "~/.omicsclaw/envs/main/bin/python",
            "packages": ["ipykernel", "scanpy"],
        }


def _approve(request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=True, reason="")


def _deny(request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=False, reason="not today")


def _explode_if_asked(request: ApprovalRequest) -> ApprovalDecision:
    raise AssertionError("the read actions must never trigger an approval card")


def _run(tool, arguments: str, approval=_approve) -> str:
    async def scenario() -> str:
        with use_tool_context(approval=approval):
            return await tool.execute(arguments)

    return asyncio.run(scenario())


# ---- the schema and the policy --------------------------------------------------


def test_the_schema_is_a_required_action_with_additive_optional_fields():
    assert MANAGE_ENV_SCHEMA["required"] == ["action"]
    assert MANAGE_ENV_SCHEMA["additionalProperties"] is False
    assert MANAGE_ENV_SCHEMA["properties"]["action"]["enum"] == [
        "list", "probe", "install",
    ]
    for optional in ("env", "interpreter", "packages"):
        assert optional in MANAGE_ENV_SCHEMA["properties"]


def test_the_policy_claims_the_worst_action():
    tool = ManageEnvironmentsTool(supply=_RecordingSupply())
    assert tool.policy.risk_level is RiskLevel.HIGH
    assert tool.policy.approval_mode is ApprovalMode.ASK
    assert tool.policy.read_only is False
    assert tool.name == TOOL_NAME


def test_an_unknown_action_is_a_schema_refusal():
    tool = ManageEnvironmentsTool(supply=_RecordingSupply())
    with pytest.raises(ToolArgumentError):
        _run(tool, json.dumps({"action": "destroy"}))


# ---- the read actions never ask -------------------------------------------------


def test_list_passes_through_without_an_approval_card():
    supply = _RecordingSupply()
    tool = ManageEnvironmentsTool(supply=supply)
    answer = _run(tool, json.dumps({"action": "list"}), approval=_explode_if_asked)
    assert supply.calls == [("list", {})]
    assert "main" in answer and "~/.omicsclaw/envs/main" in answer


def test_probe_names_the_environment_and_the_gaps():
    supply = _RecordingSupply()
    tool = ManageEnvironmentsTool(supply=supply)
    answer = _run(
        tool, json.dumps({"action": "probe"}), approval=_explode_if_asked
    )
    assert supply.calls == [
        ("probe", {"env": "main", "interpreter": None})
    ]
    assert "ipykernel" in answer and "scanpy" in answer


def test_probe_accepts_an_explicit_interpreter_instead():
    supply = _RecordingSupply()
    tool = ManageEnvironmentsTool(supply=supply)
    _run(
        tool,
        json.dumps({"action": "probe", "interpreter": "/opt/conda/envs/bio/bin/python"}),
    )
    assert supply.calls[0][1]["interpreter"] == "/opt/conda/envs/bio/bin/python"


def test_probe_refuses_a_url_or_relative_interpreter():
    tool = ManageEnvironmentsTool(supply=_RecordingSupply())
    for bad in ("https://evil.example/python", "bin/python", "../python"):
        with pytest.raises(ToolArgumentError):
            _run(tool, json.dumps({"action": "probe", "interpreter": bad}))


def test_interpreter_is_meaningless_outside_probe():
    tool = ManageEnvironmentsTool(supply=_RecordingSupply())
    with pytest.raises(ToolArgumentError):
        _run(
            tool,
            json.dumps({"action": "install", "interpreter": "/opt/bin/python"}),
        )


# ---- the install action asks, and the whitelist holds ---------------------------


def test_a_denied_install_never_reaches_the_supply():
    supply = _RecordingSupply()
    tool = ManageEnvironmentsTool(supply=supply)
    with pytest.raises(Exception) as raised:
        _run(
            tool,
            json.dumps({"action": "install", "packages": ["squidpy"]}),
            approval=_deny,
        )
    assert supply.calls == []
    assert "not today" in str(raised.value)


def test_an_approved_install_carries_deduplicated_bare_names():
    supply = _RecordingSupply()
    tool = ManageEnvironmentsTool(supply=supply)
    answer = _run(
        tool,
        json.dumps({"action": "install", "packages": ["squidpy", "squidpy", "anndata"]}),
    )
    assert supply.calls == [
        ("install", {"env": "main", "packages": ("squidpy", "anndata")})
    ]
    assert "environment ready" in answer


def test_package_names_never_carry_urls_specifiers_or_shell_text():
    tool = ManageEnvironmentsTool(supply=_RecordingSupply())
    for bad in (
        "https://evil.example/pkg.whl",
        "scanpy>=1.10",
        "scanpy[leiden]",
        "foo; rm -rf /",
        "-r requirements.txt",
        "foo bar",
    ):
        with pytest.raises(ToolArgumentError):
            _run(tool, json.dumps({"action": "install", "packages": [bad]}))


def test_packages_are_meaningless_outside_install():
    tool = ManageEnvironmentsTool(supply=_RecordingSupply())
    with pytest.raises(ToolArgumentError):
        _run(tool, json.dumps({"action": "probe", "packages": ["scanpy"]}))


def test_the_env_name_cannot_escape_the_root():
    tool = ManageEnvironmentsTool(supply=_RecordingSupply())
    for bad in ("../elsewhere", "a/b", "a b", ".hidden", ".."):
        with pytest.raises(ToolArgumentError):
            _run(tool, json.dumps({"action": "install", "env": bad}))


def test_a_missing_supply_refuses_at_execution_time():
    tool = ManageEnvironmentsTool()
    with pytest.raises(RuntimeError) as raised:
        _run(tool, json.dumps({"action": "list"}))
    assert "no environment supply bound" in str(raised.value)
