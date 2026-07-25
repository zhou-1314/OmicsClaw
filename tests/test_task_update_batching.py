"""One tool iteration should be able to close one task and open the next.

Diagnosis 2026-07-25: ``task_update`` took a single ``task_id``, so every status
flip cost its own tool call. In the trace behind this fix, plan bookkeeping was
6 of the 24 calls a task made — a quarter of the turn's tool-iteration budget
spent on status, not analysis — and the turn hit ``MAX_TOOL_ITERATIONS`` before
it could report its results.

The hand-off "step-1 done, step-2 starting" is one intent; it should be one call.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tests.test_engineering_tools import _build_executors  # type: ignore[import-not-found]

SESSION = dict(
    session_id="interactive:user:chat-batch",
    chat_id="chat-batch",
    surface="interactive",
)


def _call(executors, name: str, args: dict, tmp_path: Path):
    return json.loads(
        asyncio.run(
            executors[name](args, workspace=str(tmp_path / "workspace"), **SESSION)
        )
    )


def _create(executors, title: str, tmp_path: Path) -> str:
    return _call(executors, "task_create", {"title": title}, tmp_path)["task"]["id"]


def test_one_call_can_complete_one_task_and_start_the_next(tmp_path: Path):
    executors = _build_executors(tmp_path)
    first = _create(executors, "生成合成单细胞表达矩阵", tmp_path)
    second = _create(executors, "预处理：标准化 + HVG + PCA", tmp_path)

    payload = _call(
        executors,
        "task_update",
        {
            "updates": [
                {"task_id": first, "status": "completed", "summary": "500x2000 generated"},
                {"task_id": second, "status": "in_progress"},
            ]
        },
        tmp_path,
    )

    by_id = {t["id"]: t for t in payload["tasks"]}
    assert by_id[first]["status"] == "completed"
    assert by_id[first]["metadata"]["summary"] == "500x2000 generated"
    assert by_id[second]["status"] == "in_progress"


def test_single_task_form_still_works(tmp_path: Path):
    """The batch form is additive — the existing one-task call is untouched."""
    executors = _build_executors(tmp_path)
    task_id = _create(executors, "数据验证", tmp_path)

    payload = _call(
        executors, "task_update", {"task_id": task_id, "status": "completed"}, tmp_path
    )
    assert payload["task"]["status"] == "completed"


def test_a_bad_id_in_a_batch_is_reported_without_losing_the_good_updates(tmp_path: Path):
    executors = _build_executors(tmp_path)
    good = _create(executors, "输出结果", tmp_path)

    payload = _call(
        executors,
        "task_update",
        {
            "updates": [
                {"task_id": good, "status": "completed"},
                {"task_id": "does-not-exist", "status": "completed"},
            ]
        },
        tmp_path,
    )

    by_id = {t["id"]: t for t in payload["tasks"]}
    assert by_id[good]["status"] == "completed"
    assert any("does-not-exist" in str(e) for e in payload["errors"])
