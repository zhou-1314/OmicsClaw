"""Fixture RSS isolation does not replace the deployed owner's resource check."""

import json
import os
import subprocess
import sys

import pytest

_QUOTA_PROCESS = """
import asyncio,json,sys,os
from pathlib import Path
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.security import rss_bytes
from omicsclaw.entry.notebook.service import NotebookError
async def run():
    pressure = bytearray(600 * 1024 * 1024)
    for index in range(0, len(pressure), 4096):
        pressure[index] = 1
    owner = OwnerRuntime(Path(sys.argv[1]), {'authorityId':'a'*32,'workspaceId':'b'*32},
        source_digest='actual-quota', env={})
    await owner.start()
    try:
        measured = rss_bytes(os.getpid())
        assert owner.limits['controlRssBytes'] == 512 * 1024 * 1024
        assert measured > owner.limits['controlRssBytes']
        try:
            owner._budget(new_kernel=True)
        except NotebookError as error:
            assert str(error) == 'notebook_resource_limit'
        else:
            raise AssertionError('actual owner quota did not reject')
        print(json.dumps({'rss': measured, 'limit': owner.limits['controlRssBytes'],
            'error': 'notebook_resource_limit'}))
    finally:
        await owner.close()
asyncio.run(run())
"""


@pytest.mark.usefixtures("notebook_control_rss")
def test_actual_owner_control_memory_limit_still_rejects(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-c", _QUOTA_PROCESS, str(tmp_path)],
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result["rss"] > result["limit"] == 512 * 1024 * 1024
    assert result["error"] == "notebook_resource_limit"


@pytest.mark.usefixtures("notebook_control_rss")
def test_control_fixture_leaves_scientific_tree_sampling_real():
    from omicsclaw.entry.notebook import runtime, security

    assert runtime.rss_bytes(os.getpid()) == 64 * 1024 * 1024
    assert runtime.tree_rss is security.tree_rss
    assert runtime.tree_rss.__globals__["rss_bytes"] is security.rss_bytes
