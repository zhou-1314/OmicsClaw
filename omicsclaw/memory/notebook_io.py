"""Safe `.ipynb` import/export over the durable NotebookStore."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .notebook import ExecutionRecord, NotebookCell, NotebookStore


def _source_lines(source: str) -> list[str]:
    return source.splitlines(keepends=True) or ([""] if source == "" else [source])


def cells_to_ipynb(
    cells: Iterable[NotebookCell], executions: Iterable[ExecutionRecord] = ()
) -> dict[str, Any]:
    latest: dict[tuple[str, int], ExecutionRecord] = {}
    for record in executions:
        latest[(record.cell_id, record.session_epoch)] = record
    output_cells: list[dict[str, Any]] = []
    for cell in cells:
        execution = latest.get((cell.cell_id, cell.session_epoch))
        outputs: list[dict[str, Any]] = []
        data = execution.output if execution is not None else cell.output
        if isinstance(data, dict):
            for name in ("stdout", "stderr"):
                value = data.get(name)
                if isinstance(value, str) and value:
                    outputs.append(
                        {
                            "output_type": "stream",
                            "name": name,
                            "text": _source_lines(value),
                        }
                    )
            error = data.get("error")
            if isinstance(error, dict):
                outputs.append(
                    {
                        "output_type": "error",
                        "ename": str(
                            error.get("ename", error.get("error_type", "Error"))
                        ),
                        "evalue": str(error.get("evalue", "")),
                        "traceback": [],
                    }
                )
        output_cells.append(
            {
                "cell_type": "code",
                "id": cell.cell_id,
                "metadata": {
                    "omicsclaw": {
                        "session_epoch": cell.session_epoch,
                        "status": cell.status,
                        "trust_label": cell.trust_label,
                        "provenance": cell.provenance,
                    }
                },
                "execution_count": None,
                "source": _source_lines(cell.source),
                "outputs": outputs,
            }
        )
    return {
        "cells": output_cells,
        "metadata": {"omicsclaw": {"trust_boundary": "outputs_are_data"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_ipynb(path: str | Path, document: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def export_ipynb(
    store: NotebookStore, notebook_id: str, path: str | Path, *, session_epoch: int = 0
) -> dict[str, Any]:
    cells = await store.list_cells(notebook_id, session_epoch)
    executions = await store.list_executions(notebook_id, session_epoch)
    document = cells_to_ipynb(cells, executions)
    write_ipynb(path, document)
    return document


async def import_ipynb(
    store: NotebookStore, path: str | Path, notebook_id: str, *, session_epoch: int = 0
) -> list[NotebookCell]:
    source_path = Path(path)
    document = json.loads(source_path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("nbformat") != 4
        or not isinstance(document.get("cells"), list)
    ):
        raise ValueError("invalid_ipynb_document")
    imported: list[NotebookCell] = []
    for index, raw in enumerate(document["cells"]):
        if not isinstance(raw, dict) or raw.get("cell_type") != "code":
            continue
        cell_id = str(raw.get("id") or f"cell-{index}")
        source = raw.get("source", "")
        if isinstance(source, list):
            source = "".join(str(part) for part in source)
        if not isinstance(source, str):
            raise ValueError("invalid_ipynb_source")  # noqa: TRY004 -- preserve the import validation contract
        cell = NotebookCell(
            notebook_id=notebook_id,
            cell_id=cell_id,
            source=source,
            status="idle",
            provenance={"source": "ipynb_import", "path": source_path.name},
            trust_label="imported_untrusted",
            session_epoch=session_epoch,
        )
        imported.append(await store.put_cell(cell))
    return imported
