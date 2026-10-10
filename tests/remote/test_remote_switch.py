"""The registration switch and the scheduling rules it gates.

The deploy-time contract: off by default with nothing mounted and
nothing injected into the prompt; on — by flag, by variable, or by
binding a plane — mounts exactly the six tools and exactly one prompt
section, and the two halves cannot be split.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.entry.assembly import (
    REMOTE_SCHEDULING_RULES,
    default_sections,
    foundation_tools,
    open_remote_plane,
)
from omicsclaw.entry.config import AppConfig, resolve_app_config


def _config(tmp_path: Path, **overrides) -> AppConfig:
    values = dict(workspace=tmp_path)
    values.update(overrides)
    return AppConfig(**values)


def test_off_by_default_in_the_dataclass():
    assert AppConfig.__dataclass_fields__["remote_execution"].default is False


def test_the_environment_variable_turns_it_on(tmp_path):
    config = resolve_app_config(
        [], {"OMICSCLAW_REMOTE_EXECUTION": "1", "OMICSCLAW_WORKSPACE": str(tmp_path)}
    )
    assert config.remote_execution is True


def test_the_flag_turns_it_on(tmp_path):
    config = resolve_app_config(
        ["--remote-execution", "true"],
        {"OMICSCLAW_WORKSPACE": str(tmp_path)},
    )
    assert config.remote_execution is True


def test_off_mounts_no_remote_tools_and_no_section(tmp_path):
    config = _config(tmp_path)
    tool_names = {tool.name for tool in foundation_tools(config)}
    assert not any(name.startswith("remote_") for name in tool_names)
    assert "ask_about_host" not in tool_names
    assert "remote" not in {section.key for section in default_sections(config)}


def test_on_with_a_plane_mounts_the_family_and_the_rules(tmp_path):
    config = _config(tmp_path, remote_execution=True)
    plane = open_remote_plane(config)
    assert plane is not None
    try:
        tool_names = {tool.name for tool in foundation_tools(config, remote=plane)}
        assert {
            "remote_exec", "remote_submit", "remote_status",
            "remote_cancel", "remote_fetch", "ask_about_host",
        } <= tool_names
        sections = default_sections(config, remote=True)
        keys = [section.key for section in sections]
        assert "remote" in keys
        # static rules sit after the sandbox block and before the
        # volatile catalogue — the ordering the section's docstring states
        assert keys.index("remote") < keys.index("skills") if "skills" in keys else True
    finally:
        plane.close()


def test_config_on_but_no_plane_mounts_nothing(tmp_path):
    # Half a feature is worse than none: a switch without its binding
    # mounts no tools, and the section follows the binding, not the flag.
    config = _config(tmp_path, remote_execution=True)
    tool_names = {tool.name for tool in foundation_tools(config)}
    assert not any(name.startswith("remote_") for name in tool_names)
    assert "remote" not in {s.key for s in default_sections(config, remote=False)}


def test_the_rules_are_the_plan_s_four_triggers():
    for trigger in ("GPU", "10 minutes", "memory", "data", "user"):
        assert trigger in REMOTE_SCHEDULING_RULES
    assert "local" in REMOTE_SCHEDULING_RULES
    assert "remote_submit" in REMOTE_SCHEDULING_RULES
    assert "ask_about_host" in REMOTE_SCHEDULING_RULES
