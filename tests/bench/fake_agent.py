"""A stand-in agent process for the harness tests.

Run as ``python fake_agent.py <spec.json>`` with the workspace as the
working directory. The spec says what this process does; every key is
optional:

``pid``         a file to write this process's pid to, before anything else
``stdin``       read standard input to its end and print the byte count
``write``       ``{relative path: text}`` written under the working directory
``log``         a file to append ``start``/``end`` lines with a clock to
``stray``       a file to write the pid of a detached ``sleep`` child to
``ignore_term`` ignore ``SIGTERM``
``sleep``       seconds to sleep before exiting
``stdout``      text printed to standard output
``evidence``    printed to standard error as ``FAKE-EVIDENCE <json>``
``exit``        the exit status (default 0)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    label = spec.get("label", "")
    if spec.get("pid"):
        Path(spec["pid"]).write_text(str(os.getpid()), encoding="utf-8")
    if spec.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if spec.get("log"):
        with open(spec["log"], "a", encoding="utf-8") as log:
            log.write(f"start {label} {time.monotonic()}\n")
    if spec.get("stdin"):
        print(f"stdin-bytes={len(sys.stdin.buffer.read())}", flush=True)
    for relative, text in spec.get("write", {}).items():
        target = Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if spec.get("stray"):
        child = subprocess.Popen(["sleep", "300"], start_new_session=True)
        Path(spec["stray"]).write_text(str(child.pid), encoding="utf-8")
    if "evidence" in spec:
        line = "FAKE-EVIDENCE " + json.dumps(spec["evidence"])
        print(line, file=sys.stderr, flush=True)
    if spec.get("stdout"):
        print(spec["stdout"], flush=True)
    time.sleep(float(spec.get("sleep", 0)))
    if spec.get("log"):
        with open(spec["log"], "a", encoding="utf-8") as log:
            log.write(f"end {label} {time.monotonic()}\n")
    return int(spec.get("exit", 0))


if __name__ == "__main__":
    raise SystemExit(main())
