"""Run a controller scenario outside the test suite's accumulated process RSS."""

import subprocess
import sys
from pathlib import Path


def run_controller_scenario(module, function, workspace):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib,sys; from pathlib import Path; "
                "getattr(importlib.import_module(sys.argv[1]),sys.argv[2])(Path(sys.argv[3]))"
            ),
            module,
            function,
            str(workspace),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
