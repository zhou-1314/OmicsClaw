"""Language identity, explicit R launch, and shared Notebook control."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from omicsclaw.entry.notebook.binding import SSHNotebookBinding
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.schema import ToolCall
from omicsclaw.tools.builtin.r_kernel import RKernelTool
from omicsclaw.tools.context import use_tool_context
from omicsclaw.tools.registry import ToolRegistry
from tests.entry.test_notebook_owner import Manager, settled
from tests.entry.test_ssh_notebook_binding import Client


async def observation(spec, workspace, **kwargs):
    return {
        "version": "fixture",
        "prefix": "/fixture/" + spec["language"],
        "executable": spec["executable"],
        "libraryPaths": spec["libraryPaths"],
        "packages": {"ipykernel": "1"}
        if spec["language"] == "python"
        else {"IRkernel": "1", "jsonlite": "1"},
        "missing": [],
        "maintenanceRoots": ["/fixture/" + spec["language"]],
    }


async def create(tmp_path, *, real=False):
    runtime = OwnerRuntime(
        tmp_path,
        {"authorityId": "a" * 32, "workspaceId": "b" * 32},
        source_digest="r-fixture",
        env={},
        **({} if real else {"manager": Manager(), "registry_probe": observation}),
    )
    await runtime.start()
    return runtime


async def environment(runtime, language, *, real=False, libraries=None):
    executable = (
        str(Path(sys.executable).with_name("R")) if language == "r" else sys.executable
    )
    return await runtime.registry.register(
        {
            "language": language,
            "executable": executable if real else "/fixture/" + language,
            "libraryPaths": (
                libraries
                or (
                    [str(Path(sys.executable).parents[1] / "lib/R/library")]
                    if real
                    else ["/fixture/r/library"]
                )
            )
            if language == "r"
            else [],
            "requiredPackages": [],
        }
    )


async def bind(runtime, language, env, suffix="", **kwargs):
    return await runtime.bind(
        {
            "notebookId": "nb",
            "sessionId": "chat",
            "commandId": "bind-" + language + suffix,
            "language": language,
            "environmentId": env["environmentId"],
            "environmentRevision": env["revision"],
            **kwargs,
        }
    )


def command(kernel, cid, source):
    return {
        "version": 1,
        "notebookId": "nb",
        "cellId": cid,
        "commandId": cid,
        "source": source,
        "expectedRevision": 0,
        "origin": "user",
        "approvalScope": "explicit_user",
        "timeoutSeconds": 20,
        "preconditions": {},
        **{
            key: kernel[key]
            for key in (
                "kernelInstanceId",
                "generation",
                "environmentId",
                "environmentRevision",
                "language",
            )
        },
    }


def identity(kernel):
    return {key: kernel[key] for key in ("kernelInstanceId", "generation")}


@pytest.mark.usefixtures("notebook_control_rss")
def test_two_language_reservations_keep_guards_and_empty_resume(tmp_path):
    async def run():
        r = await create(tmp_path)
        try:
            py_env, r_env = await environment(r, "python"), await environment(r, "r")
            py = (await bind(r, "python", py_env))["kernel"]
            rk = (await bind(r, "r", r_env))["kernel"]
            assert (py["generation"], rk["generation"]) == (1, 2)
            state = r.actions.binding({"sessionId": "chat"})
            assert state["kernel"] == py and state["kernels"]["r"] == rk
            prepared = await r.context({"sessionId": "chat", "requestId": "before"})
            await r.check_request(
                {
                    "sessionId": "chat",
                    "requestId": "before",
                    "token": prepared["token"],
                    "language": "python",
                }
            )
            await r.release(
                {"notebookId": "nb", "commandId": "release-r", **identity(rk)}
            )
            with pytest.raises(NotebookError, match="notebook_request_stale"):
                await r.check_request(
                    {
                        "sessionId": "chat",
                        "requestId": "before",
                        "token": prepared["token"],
                        "language": "python",
                    }
                )
            record = await r.admit(
                {"command": command(py, "python-after-r-release", "answer = 42")}
            )
            assert (await settled(r, record["execution_id"]))["status"] == "completed"
            await r.release(
                {"notebookId": "nb", "commandId": "release-py", **identity(py)}
            )
            control = r.actions.binding({"sessionId": "chat"})["control"]
            resume = await r.resume(
                {
                    "notebookId": "nb",
                    "commandId": "resume-empty",
                    "userWatermark": control["userWatermark"],
                    "kernelIdentities": {},
                }
            )
            assert (
                resume["continuationReady"]
                and resume["control"]["controlMode"] == "agent"
            )
            rb = (await bind(r, "r", r_env, "-new", takeControl=True))["kernel"]
            assert rb["generation"] == 3
            saved = await r.save(
                {
                    "notebookId": "nb",
                    "cellId": "draft",
                    "commandId": "save-r",
                    "source": "x <- 1",
                    "language": "r",
                    "expectedRevision": 0,
                    **identity(rb),
                }
            )
            assert saved["cell"]["language"] == "r"
            assert (
                r.actions.cell({"notebookId": "nb", "cellId": "draft"})["language"]
                == "r"
            )
            with pytest.raises(NotebookError, match="notebook_generation_changed"):
                await r.release(
                    {"notebookId": "nb", "commandId": "old-r", **identity(rk)}
                )
            assert r.manager.codes == ["answer = 42"]
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_r_agent_tool_uses_its_own_snapshot_and_no_python_helper(tmp_path):
    async def run():
        r = await create(tmp_path)
        try:
            await environment(r, "r")
            binding = SSHNotebookBinding(Client(r))
            tools = ToolRegistry(before_execute=binding.before_tool)
            tools.register(RKernelTool(runner=binding))
            call = ToolCall(
                id="r-call", name="r", arguments=json.dumps({"code": "counts <- 42L"})
            )
            approvals = []

            async def approve(request):
                approvals.append(request)
                return True

            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                request = await binding.notebook_augmentor("chat").prepare_request(
                    (), (), "r-request"
                )
                with tools.use_request_context(request.context, 0, call):
                    result = await tools.execute(call)
            assert not result.is_error, result.output
            assert len(approvals) == 1 and r.manager.codes == ["counts <- 42L"]
            state = r.actions.binding({"sessionId": "chat"})
            assert state["kernel"] is None
            assert state["kernels"]["r"]["language"] == "r"
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_r_registered_library_and_python_objects_remain_independent(tmp_path):
    pytest.importorskip("ipykernel")
    executable = Path(sys.executable).with_name("R")
    if not executable.is_file():
        pytest.skip("R is not installed beside the selected test Python")
    libraries = [
        p for p in os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep) if p
    ]

    async def run():
        r = await create(tmp_path, real=True)
        try:
            re = await environment(r, "r", real=True, libraries=libraries)
            if re["status"] != "ready":
                pytest.skip(
                    "Selected R lacks IRkernel/jsonlite; set OMICSCLAW_TEST_R_LIBRARY"
                )
            pe = await environment(r, "python", real=True)
            py = (await bind(r, "python", pe))["kernel"]
            rk = (await bind(r, "r", re))["kernel"]
            assert not r.manager.states

            async def cell(k, cid, source):
                receipt = await r.admit({"command": command(k, cid, source)})
                for _ in range(600):
                    row = r.views.execution("nb", receipt["execution_id"])
                    if (
                        row["status"] in {"completed", "failed", "interrupted"}
                        and row["durability"] != "pending"
                    ):
                        assert row["status"] == "completed", row
                        return row
                    await asyncio.sleep(0.05)
                raise AssertionError("real cell did not finish")

            await cell(py, "py-first", "same_name = 9001")
            await cell(
                rk,
                "r-first",
                "same_name <- new.env(); same_name$counts <- matrix(1:12, nrow=3); cat('R-created')",
            )
            changed = await cell(
                rk, "r-user", "same_name$counts[1,1] <- 42L; cat(sum(same_name$counts))"
            )
            assert "119" in changed["output"]["stdout"]
            checked = await cell(py, "py-next", "print(same_name)")
            assert "9001" in checked["output"]["stdout"]
            again = await cell(
                rk,
                "r-next",
                "stopifnot(same_name$counts[1,1] == 42L); cat('R-persisted')",
            )
            assert "R-persisted" in again["output"]["stdout"]
            marker = tmp_path / "r-cancel-started"
            running = await r.admit(
                {
                    "command": command(
                        rk,
                        "r-cancel",
                        "same_name$before_cancel <- TRUE; writeLines('started', "
                        + json.dumps(str(marker))
                        + "); Sys.sleep(60)",
                    )
                }
            )
            for _ in range(200):
                if marker.is_file():
                    break
                await asyncio.sleep(0.02)
            assert marker.is_file(), "R cell did not reach its execution barrier"
            await r.cancel(
                {
                    "notebookId": "nb",
                    "executionId": running["execution_id"],
                    "commandId": "cancel-r",
                    "language": "r",
                    **identity(rk),
                }
            )
            interrupted = await settled(r, running["execution_id"])
            assert interrupted["status"] == "interrupted"
            await cell(
                rk,
                "r-after-cancel",
                "stopifnot(same_name$counts[1,1] == 42L); cat('still-alive')",
            )
            rich = await cell(
                rk,
                "r-rich",
                "data.frame(gene=c('A','B'),count=c(2L,5L)); plot(1:4); message('R-message'); warning('R-warning')",
            )
            types = {item["type"] for item in rich["output"]["outputs"]}
            assert {"table", "image", "stream"} <= types, rich
            assert (
                "R-message" in rich["output"]["stderr"]
                and "R-warning" in rich["output"]["stderr"]
            )
            state = r.views.snapshot("nb")
            assert (
                state["kernels"]["python"]["kernelInstanceId"] == py["kernelInstanceId"]
            )
            assert state["kernels"]["r"]["kernelInstanceId"] == rk["kernelInstanceId"]
        finally:
            await r.close()

    asyncio.run(run())


def test_registered_r_ignores_global_spec_profiles_and_wrapper_credentials(
    tmp_path, monkeypatch
):
    from omicsclaw.kernel.r_launch import RegisteredRLaunch
    from omicsclaw.kernel.session import SessionKernel
    from omicsclaw.tools.builtin.bash import without_control_credentials

    executable = Path(sys.executable).with_name("R")
    libraries = [
        p for p in os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep) if p
    ]
    if not executable.is_file() or not libraries:
        pytest.skip("Explicit R/IRkernel library fixture is unavailable")
    unicode_library = tmp_path / 'library-😀-"-空'
    unicode_library.mkdir()
    profile = tmp_path / "injected-profile.R"
    marker = tmp_path / "profile-ran"
    profile.write_text('writeLines("unexpected", ' + json.dumps(str(marker)) + ")")
    (tmp_path / ".Rprofile").write_text(profile.read_text())
    monkeypatch.setattr(
        "omicsclaw.kernel.session._r_spec_manager",
        lambda: pytest.fail("Registered R consulted a global kernelspec"),
    )

    class Wrapper:
        kernel_transport = "tcp"

        def wrap(self, argv, *, env, **kwargs):
            return list(argv), {
                **env,
                "OPENAI_API_KEY": "injected-control-canary",
                "R_PROFILE_USER": str(profile),
                "R_LIBS_USER": "/wrong/library",
                "R_HOME": "/wrong/R",
            }

    kernel = SessionKernel(
        "explicit-r",
        workspace=tmp_path,
        language="r",
        r_launch=RegisteredRLaunch(str(executable), (str(unicode_library), *libraries)),
        spawn_wrapper=Wrapper(),
        env_scrubber=without_control_credentials,
    )
    try:
        kernel.start()
        result = kernel.execute(
            'cat(jsonlite::toJSON(list(libraries=I(.libPaths()), key=Sys.getenv("OPENAI_API_KEY"), profile=Sys.getenv("R_PROFILE_USER")), auto_unbox=TRUE))',
            timeout_s=15,
        )
        assert result.status == "ok", result
        values = json.loads(result.stdout)
        assert values["libraries"][0] == str(unicode_library)
        assert values["key"] == values["profile"] == ""
        assert not marker.exists()
    finally:
        kernel.shutdown()


@pytest.mark.usefixtures("notebook_control_rss")
def test_legacy_python_records_migrate_without_becoming_r(tmp_path):
    import sqlite3

    async def run():
        r = await create(tmp_path)
        py_env = await environment(r, "python")
        py = (await bind(r, "python", py_env))["kernel"]
        await r.save(
            {
                "notebookId": "nb",
                "cellId": "legacy",
                "commandId": "legacy-save",
                "source": "x = 42",
                "expectedRevision": 0,
                **identity(py),
            }
        )
        await r.close()
        with sqlite3.connect(tmp_path / ".omicsclaw/notebooks.db") as c:
            c.execute("DROP INDEX idx_notebook_kernel_language")
            c.execute("ALTER TABLE notebook_runtime_kernels DROP COLUMN language")
            c.execute("ALTER TABLE notebook_cells DROP COLUMN language")
        r = await create(tmp_path)
        try:
            snapshot = r.views.snapshot("nb")
            assert (
                snapshot["kernels"]["python"]["kernelInstanceId"]
                == py["kernelInstanceId"]
            )
            assert snapshot["kernels"]["r"] is None
            assert snapshot["cells"][0]["source"] == "x = 42"
            assert snapshot["cells"][0]["language"] == "python"
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_reserved_r_save_resume_and_wrong_language_refusal(tmp_path):
    async def run():
        r = await create(tmp_path)
        try:
            py_env, r_env = await environment(r, "python"), await environment(r, "r")
            with pytest.raises(NotebookError, match="notebook_language_mismatch"):
                await bind(r, "r", py_env)
            rk = (await bind(r, "r", r_env, takeControl=True))["kernel"]
            await r.save(
                {
                    "notebookId": "nb",
                    "cellId": "draft",
                    "commandId": "save",
                    "source": "answer <- 42L",
                    "language": "r",
                    "expectedRevision": 0,
                    **identity(rk),
                }
            )
            assert not r.manager.codes and not r.manager.states
            control = r.actions.binding({"sessionId": "chat"})["control"]
            resume = await r.resume(
                {
                    "notebookId": "nb",
                    "commandId": "resume",
                    "userWatermark": control["userWatermark"],
                    "kernelIdentities": {"r": identity(rk)},
                }
            )
            assert resume["continuationReady"]
            request = await r.context({"sessionId": "chat", "requestId": "fresh"})
            await r.check_request(
                {
                    "sessionId": "chat",
                    "requestId": "fresh",
                    "token": request["token"],
                    "language": "r",
                }
            )
            assert (
                request["summary"]["kernels"]["r"]["kernelInstanceId"]
                == rk["kernelInstanceId"]
            )
            with pytest.raises(NotebookError, match="notebook_generation_changed"):
                await r.save(
                    {
                        "notebookId": "nb",
                        "cellId": "bad",
                        "commandId": "wrong-language",
                        "source": "x = 1",
                        "language": "python",
                        "expectedRevision": 0,
                        **identity(rk),
                    }
                )
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_r_identity_mismatch_refuses_user_source(tmp_path, monkeypatch):
    libraries = [
        p for p in os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep) if p
    ]
    if not Path(sys.executable).with_name("R").is_file() or not libraries:
        pytest.skip("Explicit R/IRkernel library fixture is unavailable")
    monkeypatch.setattr(
        "omicsclaw.entry.notebook.runtime.identity_code", lambda packages: 'cat("{}")'
    )

    async def run():
        r = await create(tmp_path, real=True)
        try:
            re = await environment(r, "r", real=True, libraries=libraries)
            assert re["status"] == "ready"
            rk = (await bind(r, "r", re))["kernel"]
            marker = tmp_path / "must-not-run"
            record = await r.admit(
                {
                    "command": command(
                        rk,
                        "wrong-identity",
                        "writeLines('executed', " + json.dumps(str(marker)) + ")",
                    )
                }
            )
            for _ in range(400):
                result = r.views.execution("nb", record["execution_id"])
                if result["status"] == "failed" and result["durability"] != "pending":
                    break
                await asyncio.sleep(0.025)
            assert result["status"] == "failed", result
            assert result["output"]["error"]["evalue"] == "notebook_r_identity_mismatch"
            assert not marker.exists()
            assert r.views.snapshot("nb")["kernels"]["r"]["state"] == "lost"
        finally:
            await r.close()

    asyncio.run(run())
