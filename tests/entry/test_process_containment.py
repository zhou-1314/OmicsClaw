"""Actual namespace-init evidence, including a descendant that calls setsid."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from omicsclaw.entry.notebook.security import process_evidence, same_process
from omicsclaw.entry.process_containment import (
    capture_containment,
    contained_exit,
    namespace,
)
from omicsclaw.sandbox.netns import probe_netns


def test_actual_unshare_chain_contains_detached_descendant_on_root_death(tmp_path):
    if not probe_netns().available:
        pytest.skip("Native Linux unshare PID namespace is unavailable")

    async def run():
        marker = tmp_path / "namespace-child.json"
        code = (
            "import os,sys,time,json; p=os.fork();\n"
            "if not p:\n"
            " os.setsid(); p=os.fork()\n"
            " if p: os._exit(0)\n"
            " open(sys.argv[1]+'.tmp','w').write(json.dumps({'pid':os.getpid()})); os.replace(sys.argv[1]+'.tmp',sys.argv[1]); time.sleep(60); os._exit(0)\n"
            "time.sleep(60)"
        )
        process = await asyncio.create_subprocess_exec(
            "unshare",
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            "--kill-child=SIGKILL",
            "--",
            sys.executable,
            "-I",
            "-c",
            code,
            str(marker),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        root = process_evidence(process.pid)
        child = None
        try:
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            proof = capture_containment(root)
            assert proof is not None
            assert not contained_exit(proof, root)
            wanted = str(json.loads(marker.read_text())["pid"])
            for entry in Path("/proc").iterdir():
                if not entry.name.isdecimal():
                    continue
                try:
                    if namespace(entry / "ns/pid") != proof["namespace"]:
                        continue
                    status = (entry / "status").read_text().splitlines()
                    nspid = next(
                        line.split()[1:] for line in status if line.startswith("NSpid:")
                    )
                    if nspid[-1] == wanted and str(marker).encode() in (
                        entry / "cmdline"
                    ).read_bytes().split(b"\0"):
                        child = process_evidence(int(entry.name))
                        break
                except (OSError, StopIteration):
                    continue
            assert same_process(child)
            assert same_process(root)
            os.kill(root["pid"], signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 3)
            for _ in range(200):
                if contained_exit(proof, root):
                    break
                await asyncio.sleep(0.01)
            assert contained_exit(proof, root)
            assert not same_process(child)
            assert not contained_exit(
                {**proof, "root": {**root, "start": "different"}}, root
            )
        finally:
            if same_process(root):
                os.kill(root["pid"], signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 3)
            if child:
                for _ in range(200):
                    if not same_process(child):
                        break
                    await asyncio.sleep(0.01)
                assert not same_process(child)

    asyncio.run(run())


def test_current_process_and_configuration_name_are_not_containment_evidence():
    birth = process_evidence(os.getpid())
    assert capture_containment(birth) is None
    assert not contained_exit({"kind": "netns"}, birth)


@pytest.mark.parametrize("kill_supervisor", [False, True])
def test_host_subreaper_closure_requires_live_waiter(tmp_path, kill_supervisor):
    from omicsclaw.entry.process_containment import (
        SubreaperLifecycleWrapper,
        wait_containment,
    )

    if not sys.platform.startswith("linux"):
        pytest.skip("Linux subreaper required")

    async def run():
        marker = tmp_path / "detached.json"
        code = (
            "import os,sys,time,json,signal; p=os.fork();\n"
            "if not p:\n"
            " os.setsid(); p=os.fork()\n"
            " if p: os._exit(0)\n"
            " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            " open(sys.argv[1]+'.tmp','w').write(json.dumps({'pid':os.getpid()})); os.replace(sys.argv[1]+'.tmp',sys.argv[1]); time.sleep(60); os._exit(0)\n"
            "time.sleep(60)"
        )
        wrapper = SubreaperLifecycleWrapper(tmp_path / "lifecycles")
        argv, _ = wrapper.wrap(
            [sys.executable, "-I", "-c", code, str(marker)], cwd=tmp_path, env={}
        )
        process = await asyncio.create_subprocess_exec(*argv, start_new_session=True)
        root = process_evidence(process.pid)
        child = None
        try:
            proof = await wait_containment(root)
            assert proof and proof["kind"] == "linux_subreaper"
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            child = process_evidence(json.loads(marker.read_text())["pid"])
            assert same_process(child)
            if kill_supervisor:
                os.kill(root["pid"], signal.SIGKILL)
                await asyncio.wait_for(process.wait(), 3)
                assert same_process(child)
                assert not contained_exit(proof, root)
                assert not (Path(proof["directory"]) / "closed.json").exists()
            else:
                assert await asyncio.to_thread(
                    wrapper.terminate, root["pid"], lambda pid: same_process(root)
                )
                await asyncio.wait_for(process.wait(), 3)
                assert not same_process(child)
                assert contained_exit(proof, root)
                assert not contained_exit({**proof, "nonce": "wrong"}, root)
        finally:
            if same_process(root):
                await asyncio.to_thread(
                    wrapper.terminate, root["pid"], lambda pid: same_process(root)
                )
            await asyncio.wait_for(process.wait(), 3)
            # A killed supervisor cannot issue closure; this fixture owns these births.
            for evidence in (child, proof.get("child") if proof else None):
                if same_process(evidence):
                    os.kill(evidence["pid"], signal.SIGKILL)
            for _ in range(200):
                if not same_process(child):
                    break
                await asyncio.sleep(0.01)
            assert not same_process(child)

    asyncio.run(run())
