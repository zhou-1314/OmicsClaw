import asyncio
import json
from pathlib import Path

from omicsclaw.memory.database import Database
from omicsclaw.memory.notebook import ExecutionRecord, NotebookCell, NotebookStore
from omicsclaw.memory.notebook_io import export_ipynb, import_ipynb


def test_ipynb_export_and_safe_import(tmp_path: Path):
    async def scenario():
        source_store = NotebookStore(Database(tmp_path / "source.db"))
        await source_store.put_cell(
            NotebookCell(
                notebook_id="nb",
                cell_id="c1",
                source="print(1)",
                status="completed",
                output={"stdout": "1\n"},
                provenance={"source": "kernel"},
                trust_label="kernel_output_data",
            )
        )
        await source_store.record_execution(
            ExecutionRecord(
                execution_id="e1",
                notebook_id="nb",
                cell_id="c1",
                status="completed",
                output={"stdout": "1\n"},
                provenance={"source": "kernel"},
                trust_label="kernel_output_data",
            )
        )
        path = tmp_path / "analysis.ipynb"
        document = await export_ipynb(source_store, "nb", path)
        assert document["nbformat"] == 4
        assert document["cells"][0]["outputs"][0]["text"] == ["1\n"]

        imported_path = tmp_path / "with-output.ipynb"
        imported_path.write_text(
            json.dumps(
                {
                    "nbformat": 4,
                    "nbformat_minor": 5,
                    "cells": [
                        {"cell_type": "markdown", "source": "ignore me"},
                        {
                            "cell_type": "code",
                            "id": "c2",
                            "source": ["print('run')\n"],
                            "outputs": [
                                {
                                    "output_type": "stream",
                                    "name": "stdout",
                                    "text": ["MUST NEVER EXECUTE\n"],
                                }
                            ],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        target_store = NotebookStore(Database(tmp_path / "target.db"))
        cells = await import_ipynb(target_store, imported_path, "target")
        assert len(cells) == 1
        assert cells[0].source == "print('run')\n"
        assert cells[0].output is None
        assert cells[0].status == "idle"
        assert cells[0].trust_label == "imported_untrusted"
        assert cells[0].provenance["source"] == "ipynb_import"
        assert await target_store.list_executions("target") == []

    asyncio.run(scenario())


def test_ipynb_import_rejects_non_v4(tmp_path: Path):
    async def scenario():
        path = tmp_path / "bad.ipynb"
        path.write_text(json.dumps({"nbformat": 3, "cells": []}), encoding="utf-8")
        store = NotebookStore(Database(tmp_path / "db.sqlite"))
        try:
            await import_ipynb(store, path, "nb")
        except ValueError as exc:
            assert str(exc) == "invalid_ipynb_document"
        else:
            raise AssertionError("invalid notebook was accepted")

    asyncio.run(scenario())
