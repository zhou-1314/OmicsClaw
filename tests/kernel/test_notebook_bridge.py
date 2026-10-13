import asyncio
from pathlib import Path

from omicsclaw.entry.assembly import WorkspaceKernelBinding
from omicsclaw.kernel.session import CellResult
from omicsclaw.memory.database import Database
from omicsclaw.memory.notebook import NotebookStore


class FakeManager:
    async def execute(self, session_id, code, **kwargs):
        callbacks = kwargs["callbacks"]
        callbacks.on_stream("stdout", "hello")
        return CellResult(status="ok", stdout="hello")


def test_workspace_binding_records_kernel_cell_lifecycle(tmp_path: Path):
    async def run():
        store = NotebookStore(Database(":memory:"))
        binding = WorkspaceKernelBinding(tmp_path, notebook_store=store)
        binding.manager = FakeManager()
        await binding.run_python(
            "print hello",
            description="demo",
            session_id="s",
            notebook_id="n",
            cell_id="c",
        )
        cell = await store.get_cell("n", "c")
        assert cell is not None
        assert cell.status == "completed"
        assert cell.output["stdout"] == "hello"
        executions = await store.list_executions("n")
        assert len(executions) == 1
        assert executions[0].status == "completed"
        assert executions[0].trust_label == "kernel_output_data"

    asyncio.run(run())
