"""Checkpoint actions reject malformed references without mutating the owner."""

import asyncio

import pytest

from omicsclaw.entry.notebook.checkpoint_wire import validate
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_r import create


@pytest.mark.parametrize(
    "action,args",
    [
        ("checkpoint.save", {}),
        ("checkpoint.save", {"kernel": {}, "objects": [], "policyRevision": 1}),
        ("checkpoint.retry", {"failure": {"failureId": "x", "failureRevision": True}}),
        (
            "checkpoint.continue",
            {
                "failure": {"failureId": "x", "failureRevision": 1},
                "acknowledgement": "yes",
            },
        ),
        (
            "checkpoint.pin",
            {
                "checkpoint": {
                    "checkpointId": "x",
                    "manifestRevision": 1,
                    "manifestSha256": "a" * 64,
                },
                "expectedPinRevision": 0,
                "pinned": 1,
            },
        ),
        ("checkpoint.private", {}),
    ],
)
def test_exact_command_shape(action, args):
    with pytest.raises(NotebookError, match="^notebook_checkpoint_"):
        validate(
            "nb",
            {
                "schemaVersion": 1,
                "commandId": "fixed",
                "action": action,
                "arguments": args,
                "expected": {
                    "analysisRevision": 0,
                    "kernels": [],
                    "objects": [],
                    "selections": [],
                },
            },
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"limit": 0},
        {"limit": 41},
        {"limit": True},
        {"cursor": "界" * 400},
        {"cursor": {}},
        {"parentRevision": 1},
        {"parentId": "x"},
    ],
)
def test_invalid_pagination_keeps_empty_state(tmp_path, extra):
    async def run():
        owner = await create(tmp_path)
        try:
            with pytest.raises(NotebookError, match="^notebook_checkpoint_"):
                owner.analysis.store.page(
                    {"notebookId": "nb", "collection": "checkpoints", **extra}
                )
            assert (
                owner.analysis.store.page(
                    {"notebookId": "nb", "collection": "checkpoints"}
                )["items"]
                == []
            )
        finally:
            await owner.close()

    asyncio.run(run())
