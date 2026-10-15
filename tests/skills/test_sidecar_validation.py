"""Sidecars load without executing annotations or losing failed injections."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from omicsclaw.entry.assembly import WorkspaceKernelBinding
from omicsclaw.kernel import CellResult
from omicsclaw.skills.sidecar import build_sidecar_bootstrap_code, validate_kernel_ast, load_skill


@pytest.mark.parametrize("validator", [validate_kernel_ast, load_skill("skill-creator").validate_kernel_ast])
@pytest.mark.parametrize("annotation", ["__import__('builtins').print('ran')", "[x for x in ()]"])
def test_executable_annotations_are_rejected(annotation, validator):
    source = f"__all__ = ['helper']\ndef helper(x: {annotation}):\n    '''Return x.'''\n    return x\n"
    assert validator(source)


@pytest.mark.parametrize("validator", [validate_kernel_ast, load_skill("skill-creator").validate_kernel_ast])
@pytest.mark.parametrize("exports", ["42", "-'bad'", "{'helper': 1}", "[42]", "['helper', 'helper']"])
def test_malformed_exports_are_validation_errors(exports, validator):
    source = f"__all__ = {exports}\ndef helper():\n    '''Return a number.'''\n    return 1\n"
    assert validator(source)


def test_first_sidecar_can_load_another_domain():
    import sys

    root = Path(__file__).resolve().parents[2] / "skills"
    path = root / "spatial" / "spatial-de" / "kernel.py"
    namespace = {}
    modules = ("skills", "skills._sdk", "skills._sdk.notebook")
    before = {name: sys.modules.get(name) for name in modules}
    exec(build_sidecar_bootstrap_code("spatial-de", path, path.read_text()), namespace)
    assert callable(namespace["load_skill"]("sc-qc").qc_summary)
    assert {name: sys.modules.get(name) for name in modules} == before


def test_failed_injection_can_be_retried(tmp_path):
    binding = WorkspaceKernelBinding(tmp_path)
    path = tmp_path / "kernel.py"
    path.write_text("__all__ = ['helper']\ndef helper():\n    '''Return one.'''\n    return 1\n")
    assert binding.register_skill_sidecar("chat", "demo", path)
    calls = []

    class Kernel:
        def execute(self, code, *, store_history=True):
            calls.append(store_history)
            return CellResult(status="error", error={"ename": "RuntimeError", "evalue": "bootstrap failed"})

    state = SimpleNamespace(language="python", kernel=Kernel(), session_id="chat")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="demo"):
            binding._inject_pending_sidecars(state)
    assert calls == [False, False]


@pytest.mark.parametrize("memory", [False, True])
def test_default_app_binds_python_cells_with_or_without_memory(tmp_path, memory):
    import asyncio
    from tests.entry.test_turn_runner import Scripted, make_app

    async def scenario():
        app = make_app(tmp_path, Scripted(), memory=memory)
        try:
            await app.kernels.run_python("print(42)", description="calculate", session_id="chat")
            assert app.kernels.notebooks.binding(session_id="chat") is not None
        finally:
            await app.aclose()

    asyncio.run(scenario())


def test_binding_uses_the_configured_skill_root(tmp_path):
    root = tmp_path / "catalogue"
    first = root / "one" / "first"
    second = root / "two" / "second"
    for directory in [first, second]:
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("instructions")
        (directory / "kernel.py").write_text("__all__ = ['helper']\ndef helper():\n    '''Return one.'''\n    return 1\n")
    binding = WorkspaceKernelBinding(tmp_path, skills_root=root)
    binding.register_skill_sidecar("chat", "first", first / "kernel.py")
    namespace = {}

    class Kernel:
        def execute(self, code, *, store_history=True):
            exec(code, namespace)
            return CellResult()

    binding._inject_pending_sidecars(SimpleNamespace(language="python", kernel=Kernel(), session_id="chat"))
    assert namespace["load_skill"]("second").helper() == 1


def test_local_notebook_binding_preserves_remote_chat_routing(tmp_path):
    import asyncio

    from omicsclaw.entry.desktop.notebooks_manager import NotebookError
    from omicsclaw.memory.database import Database
    from omicsclaw.memory.notebook import NotebookStore

    async def scenario():
        db = Database(tmp_path / "memory.db")
        store = NotebookStore(db)
        binding = WorkspaceKernelBinding(tmp_path, notebook_store=store, auto_bind_notebook=True)
        binding.notebooks.bind("chat", "chat")
        calls = []

        async def execute(session_id, code, **kwargs):
            calls.append((session_id, code, kwargs["location"]))
            return CellResult(stdout="remote output")

        binding.manager.execute = execute
        try:
            result = await binding.run_python(
                "print(42)", description="calculate", session_id="chat", location="remote:gpu"
            )
            assert "remote output" in result
            assert calls == [("chat", "print(42)", "remote:gpu")]
            assert len(await store.list_executions("chat")) == 1
            assert (await binding.notebooks.snapshot("chat"))["executions"] == []
            with pytest.raises(NotebookError, match="bound_notebook_location_mismatch"):
                await binding.run_python(
                    "print(42)", description="calculate", session_id="chat",
                    location="remote:gpu", notebook_id="chat",
                )
            assert len(calls) == 1
        finally:
            await binding.aclose()
            db.close()

    asyncio.run(scenario())
