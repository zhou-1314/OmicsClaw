"""Enforce the Claude Science 4-part SKILL.md + sidecar_gate kernel.py contract across all skills."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from omicsclaw.skills import SkillDiscovery, load_skills
from omicsclaw.schema import Message, Role, ToolDefinition
from omicsclaw.skills import sidecar as _skill_creator
from omicsclaw.skills.sidecar import load_skill

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"


def all_skill_dirs() -> list[pathlib.Path]:
    return sorted(md.parent for md in SKILLS_ROOT.rglob("SKILL.md"))


def test_total_skills_include_88_domain_and_skill_creator():
    dirs = all_skill_dirs()
    assert len(dirs) == 89
    assert (SKILLS_ROOT / "skill-creator" / "SKILL.md").is_file()
    assert (SKILLS_ROOT / "skill-creator" / "kernel.py").is_file()


@pytest.mark.parametrize("skill_dir", all_skill_dirs(), ids=lambda p: p.name)
def test_every_skill_passes_claude_science_contract_and_sidecar_gate(skill_dir: pathlib.Path):
    errors = _skill_creator.validate_skill_dir(skill_dir, check_isolated=False)
    assert errors == [], f"{skill_dir.name} failed contract: {errors}"


def test_every_kernel_executes_cleanly_in_isolated_python_without_site_packages():
    """Claude Science requires kernel.py to execute under `python -I -S` (no third-party imports at top level)."""
    kernels = [str(d / "kernel.py") for d in all_skill_dirs()]
    code = (
        "import json, sys\n"
        "failed = {}\n"
        "for path in json.loads(sys.argv[1]):\n"
        "    ns = {'__name__': 'kernel'}\n"
        "    try:\n"
        "        with open(path, encoding='utf-8') as fh:\n"
        "            src = fh.read()\n"
        "        exec(compile(src, path, 'exec'), ns)\n"
        "        exported = ns.get('__all__', [])\n"
        "        assert exported, 'empty __all__'\n"
        "        for fn in exported:\n"
        "            assert callable(ns.get(fn)), f'{fn} not callable'\n"
        "    except Exception as exc:\n"
        "        failed[path] = str(exc)\n"
        "assert not failed, failed\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, json.dumps(kernels)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_every_skill_loads_via_load_skill_with_non_empty_exports():
    for sdir in all_skill_dirs():
        mod = load_skill(sdir.name, root=SKILLS_ROOT)
        assert hasattr(mod, "__all__") and len(mod.__all__) >= 1
        for fn_name in mod.__all__:
            assert callable(getattr(mod, fn_name))


def test_skill_creator_kernel_and_cli_workflow(tmp_path: pathlib.Path):
    creator = load_skill("skill-creator", root=SKILLS_ROOT)
    assert creator.validate_skill(str(SKILLS_ROOT / "spatial" / "spatial-de")) == []

    bad_errs = creator.validate_kernel_ast(
        "import scanpy as sc\n_PRIVATE = 1\ndef f(x=range(3)):\n    return x\n"
    )
    assert any("top-level import" in e for e in bad_errs)
    assert any("non-literal default" in e for e in bad_errs)
    assert any("starts with '_'" in e for e in bad_errs)

    created = _skill_creator.scaffold_skill("spatial", "spatial-demo-test", skills_root=tmp_path)
    assert _skill_creator.validate_skill_dir(created) == []

    runs_file = tmp_path / "runs.json"
    runs_file.write_text(
        json.dumps(
            {
                "runs": [
                    {"configuration": "with_skill", "pass_rate": 1.0, "time_seconds": 12.0, "tokens": 1800},
                    {"configuration": "without_skill", "pass_rate": 0.5, "time_seconds": 25.0, "tokens": 3400},
                ]
            }
        ),
        encoding="utf-8",
    )
    agg = _skill_creator.aggregate_benchmark(runs_file)
    assert agg["summary"]["delta"]["pass_rate"] == 0.5
    assert agg["summary"]["delta"]["time_seconds"] == -13.0


def test_skill_creator_is_discoverable_via_skill_discovery():
    import asyncio

    index = load_skills(SKILLS_ROOT)
    discovery = SkillDiscovery(index)
    tools = (ToolDefinition(name="use_skill", description="", input_schema={}),)
    block = asyncio.run(
        discovery.augment(
            [Message(role=Role.USER, content="Use skill-creator to validate and refactor a skill sidecar")],
            tools,
        )
    )
    assert any("skill-creator" in m.content for m in block)
