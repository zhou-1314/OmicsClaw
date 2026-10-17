"""Private IRkernel launch data supplied by the environment registry."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class RegisteredRLaunch:
    executable: str
    library_paths: tuple[str, ...]

    def argv(self):
        bootstrap = "local({a <- commandArgs(trailingOnly=TRUE); n <- as.integer(a[[1]]); .libPaths(c(a[seq_len(n)+1L],.Library)); IRkernel::main(connection_file=a[[n+2L]])})"
        return [
            self.executable,
            "--vanilla",
            "--slave",
            "-e",
            bootstrap,
            "--args",
            str(len(self.library_paths)),
            *self.library_paths,
            "{connection_file}",
        ]


def identity_code(packages):
    names = "c(" + ",".join(json.dumps(p) for p in sorted(packages)) + ")"
    return (
        """local({
      names <- """
        + names
        + """
      versions <- as.list(setNames(vapply(names, function(p) as.character(packageVersion(p)), character(1)), names))
      cat(jsonlite::toJSON(list(version=as.character(getRversion()), prefix=R.home(),
        executable=normalizePath(file.path(R.home("bin"), "R")), libraryPaths=I(.libPaths()),
        packages=versions), auto_unbox=TRUE))
    })"""
    )
