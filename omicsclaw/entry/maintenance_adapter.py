"""Typed offline package plans and post-install verification in selected runtimes."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .environment_coordination import CoordinationError
from .environment_scopes import registered_read_roots, registered_write_roots
from .maintenance_artifacts import prepare_artifacts, verify_artifacts
from .maintenance_process import MaintenanceProcess

PYTHON_WORKER = Path(__file__).resolve().parents[1] / "skillenv/maintenance_probe.py"
R_PROBE = r"""
args <- commandArgs(trailingOnly=TRUE)
nlibs <- as.integer(args[[1]])
.libPaths(c(args[seq_len(nlibs) + 1L], .Library))
request <- jsonlite::fromJSON(args[[nlibs + 2L]], simplifyVector=FALSE)
installed <- installed.packages(lib.loc=.libPaths())
installed <- installed[!duplicated(installed[,"Package"]),,drop=FALSE]
versions <- as.list(setNames(installed[,"Version"], installed[,"Package"]))
if (length(versions) > 2048) stop("inventory_limit")
versions[["R"]] <- as.character(getRversion())
packages <- lapply(request$packages, function(p) list(name=p$name,
 beforeVersion=if (is.null(versions[[p$name]])) NULL else versions[[p$name]], afterVersion=p$version))
if (request$action == "inspect") {
 for (p in request$packages) versions[[p$name]] <- p$version
 for (p in request$packages) {
  for (dep in strsplit(p$requires, ",", fixed=TRUE)[[1]]) {
   dep <- trimws(dep)
   if (!nzchar(dep)) next
   match <- regmatches(dep, regexec("^([A-Za-z][A-Za-z0-9.]*)[[:space:]]*([(]([><=]+)[[:space:]]*([^)]*)[)])?$", dep))[[1]]
   if (length(match) == 0L || is.null(versions[[match[[2L]]]])) stop("missing_dependency")
   if (length(match) >= 5L && nzchar(match[[4L]])) {
    cmp <- utils::compareVersion(versions[[match[[2L]]]], trimws(match[[5L]]))
    ok <- switch(match[[4L]], ">="=cmp>=0, "<="=cmp<=0, "=="=cmp==0, ">"=cmp>0, "<"=cmp<0, FALSE)
    if (!ok) stop("dependency_version_mismatch")
   }
  }
 }
 checked <- list()
} else {
 checked <- lapply(request$checks, function(item) {
  package <- find.package(item$package)
  version <- as.character(packageVersion(item$package))
  if (!startsWith(normalizePath(package), paste0(normalizePath(request$libraryPath), "/")) || version != item$version) stop("package_verification_failed")
  if (!requireNamespace(item$package, quietly=TRUE)) stop("package_load_failed")
  list(package=item$package,version=version,modulePath=normalizePath(package))
 })
}
cat(jsonlite::toJSON(list(prefix=R.home(), executable=normalizePath(file.path(R.home("bin"),"R")),
 installerVersion=as.character(getRversion()),packages=packages,checks=checked,
 dependencyErrors=list(),baselineDependencyErrors=list()), auto_unbox=TRUE, null="null"))
"""


def probe_argv(environment, request):
    spec = environment["spec"]
    encoded = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
    if spec["language"] == "python":
        return [spec["executable"], "-I", "-B", str(PYTHON_WORKER), encoded]
    return [
        spec["executable"],
        "--vanilla",
        "--slave",
        "-e",
        R_PROBE,
        "--args",
        str(len(spec["libraryPaths"])),
        *spec["libraryPaths"],
        encoded,
    ]


def decode_probe(result):
    if result["exitCode"]:
        raise CoordinationError("environment_maintenance_invalid_plan")
    try:
        value = json.loads(result["output"])
        if not isinstance(value, dict) or value.get("dependencyErrors") != []:
            raise ValueError("dependencies")
        return value
    except (ValueError, TypeError) as error:
        raise CoordinationError("environment_maintenance_invalid_plan") from error


async def prepare_plan(
    environment,
    operation,
    directory,
    *,
    config=None,
    on_process=None,
    before_probe=None,
    launch_intent=None,
):
    language = environment["spec"]["language"]
    artifacts = prepare_artifacts(operation, language, directory)
    roots = registered_write_roots(
        environment, library_path=operation.get("libraryPath")
    )
    permission = all(
        os.access(root["canonicalPath"], os.W_OK | os.X_OK) for root in roots
    )
    free = min(shutil.disk_usage(root["canonicalPath"]).free for root in roots)
    if not permission or free < max(
        64 * 1024 * 1024, sum(a["bytes"] for a in artifacts) * 4
    ):
        raise CoordinationError("environment_maintenance_resource_limit")
    async with MaintenanceProcess(directory, environment, config=config) as process:
        result = await process.run(
            probe_argv(
                environment,
                {"action": "inspect", "packages": [a["metadata"] for a in artifacts]},
            ),
            on_spawn=on_process,
            before_spawn=launch_intent,
            before_execute=before_probe,
        )
        observed = decode_probe(result)
        sandbox = process.evidence
    if observed["prefix"] != environment["observation"]["prefix"]:
        raise CoordinationError("environment_maintenance_scope_changed")
    executable = environment["spec"]["executable"]
    if language == "python":
        argv = [
            executable,
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--upgrade",
            *[a["path"] for a in artifacts],
        ]
    else:
        argv = [
            executable,
            "--vanilla",
            "CMD",
            "INSTALL",
            "--library=" + roots[0]["canonicalPath"],
            *[a["path"] for a in artifacts],
        ]
    packages = observed["packages"]
    if isinstance(packages, dict):
        packages = [packages]
    for package, artifact in zip(packages, artifacts, strict=True):
        package["artifactSha256"] = artifact["sha256"]
    return {
        "adapter": operation["adapter"],
        "packages": packages,
        "artifacts": [
            {k: a[k] for k in ("name", "sha256", "bytes")} for a in artifacts
        ],
        "installer": {
            "executable": executable,
            "version": observed["installerVersion"],
            "argv": argv,
        },
        "writeRoots": roots,
        "readRoots": registered_read_roots(environment),
        "network": "deny",
        "privileges": "current_user",
        "sandbox": sandbox,
        "budgets": {"timeoutSeconds": 300, "outputBytes": 1024 * 1024},
        "permission": {"writable": True, "freeBytes": free, "reason": ""},
    }, {
        "artifacts": artifacts,
        "environment": environment,
        "operation": operation,
        "baselineDependencyErrors": observed["baselineDependencyErrors"],
    }


async def install_plan(
    plan,
    private,
    directory,
    *,
    config=None,
    before_write,
    on_process=None,
    on_result=None,
):
    verify_artifacts(private["artifacts"])
    async with MaintenanceProcess(
        directory, private["environment"], config=config
    ) as process:
        # Commit the damage possibility before a child can enter an installer.
        result = await process.run(
            plan["installer"]["argv"],
            timeout=plan["budgets"]["timeoutSeconds"],
            maximum=plan["budgets"]["outputBytes"],
            write_roots=[r["canonicalPath"] for r in plan["writeRoots"]],
            on_spawn=on_process,
            before_execute=before_write,
            on_result=on_result,
        )
        if result["exitCode"]:
            raise CoordinationError("environment_maintenance_needs_repair")
        return result


async def verify_plan(
    private,
    directory,
    *,
    config=None,
    on_result=None,
    on_process=None,
    before_probe=None,
):
    environment, operation = private["environment"], private["operation"]
    request = {
        "action": "verify",
        "packages": [],
        "checks": operation["checks"],
        "prefix": environment["observation"]["prefix"],
        "libraryPath": operation.get("libraryPath"),
        "baselineDependencyErrors": private["baselineDependencyErrors"],
    }
    async with MaintenanceProcess(directory, environment, config=config) as process:
        result = await process.run(
            probe_argv(environment, request),
            on_result=on_result,
            on_spawn=on_process,
            before_execute=before_probe,
        )
        try:
            return decode_probe(result)
        except CoordinationError as error:
            raise CoordinationError("environment_maintenance_needs_repair") from error
