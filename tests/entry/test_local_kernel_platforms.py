"""Local kernel setup must not load Linux coordination on other platforms."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_local_kernel_binding_skips_linux_coordination(tmp_path, platform):
    script = """
import builtins
import sys
from omicsclaw.entry.assembly import WorkspaceKernelBinding

original_import = builtins.__import__

def platform_import(name, *args, **kwargs):
    if name == "fcntl" or name.endswith("environment_runtime"):
        raise ModuleNotFoundError("Linux coordination is unavailable")
    return original_import(name, *args, **kwargs)

builtins.__import__ = platform_import
sys.platform = sys.argv[2]
binding = WorkspaceKernelBinding(sys.argv[1])
assert binding.manager._environment_guard is None
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), platform],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
