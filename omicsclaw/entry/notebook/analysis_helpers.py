"""Fixed selection-file helpers; owner validates every published descriptor."""

import json
import re
from pathlib import Path

from omicsclaw.skillenv import notebook_identity as codec

from . import analysis_contracts as wire
from .security import private_directory
from .service import NotebookError


def bootstrap(language, directory, *, strict):
    root = Path(__file__).resolve().parents[2] / "skillenv"
    if language == "python":
        return f"""def __omicsclaw_selection_install():
    import runpy
    ns = globals()
    previous = ns.get('__omicsclaw_selection_function')
    if 'notebook_selection_ids' in ns and ns['notebook_selection_ids'] is not previous:
        if {strict!r}:
            raise RuntimeError('notebook_selection_ids conflicts with an existing name')
        return
    codec = runpy.run_path({str(root / "notebook_identity.py")!r})
    writer = runpy.run_path({str(root / "notebook_inspect.py")!r})['write_selection']
    def notebook_selection_ids(keys, rows):
        return writer(keys, rows, {str(directory)!r}, codec)
    ns['notebook_selection_ids'] = notebook_selection_ids
    ns['__omicsclaw_selection_function'] = notebook_selection_ids
try:
    __omicsclaw_selection_install()
finally:
    del __omicsclaw_selection_install
"""
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    return (
        "local({ g <- .GlobalEnv; name <- 'notebook_selection_ids'; marker <- '__omicsclaw_selection_function'; "
        "conflict <- exists(name,g,inherits=FALSE) && (bindingIsActive(name,g) || !exists(marker,g,inherits=FALSE) || bindingIsActive(marker,g) || !identical(get(name,g,inherits=FALSE),get(marker,g,inherits=FALSE))); "
        + (
            "if(conflict) stop('notebook_selection_ids conflicts with an existing name'); "
            if strict
            else ""
        )
        + "if(!conflict) { inspector <- new.env(parent=baseenv()); sys.source("
        + quote(root / "notebook_identity.R")
        + ",envir=inspector); sys.source("
        + quote(root / "notebook_inspect.R")
        + ",envir=inspector); output <- "
        + quote(directory)
        + "; fn <- function(keys,rows) inspector$write_selection(keys,rows,output); "
        "assign(name,fn,g); assign(marker,fn,g) } })"
    )


class SelectionHelpers:
    def __init__(self, analysis):
        self.a = analysis
        self.r = analysis.r

    def inject(self, state):
        eid = state.current_execution_token

        def read(c):
            operation = c.execute(
                "SELECT 1 FROM notebook_analysis_operations WHERE execution_id=?",
                (eid,),
            ).fetchone()
            row = c.execute(
                "SELECT semantic_json FROM notebook_runtime_executions WHERE execution_id=?",
                (eid,),
            ).fetchone()
            return None if operation or row is None else json.loads(row[0])

        command = self.r.db.run(read)
        if command is None:
            return
        directory = private_directory(self.a.files.staging / (eid + "-selections"))
        result = state.kernel.execute(
            bootstrap(
                command["language"], directory, strict=command["origin"] == "agent"
            ),
            store_history=False,
            timeout_s=15,
        )
        if result.status != "ok":
            raise NotebookError("notebook_helper_failed", 409)

    def collect(self, c, nid, eid):
        if c.execute(
            "SELECT 1 FROM notebook_analysis_collected WHERE execution_id=?", (eid,)
        ).fetchone():
            return
        directory = self.a.files.staging / (eid + "-selections")
        if not directory.is_dir():
            return
        count, total, consumed = 0, 0, []
        for path in directory.iterdir():
            if not re.fullmatch(r"selection-[0-9a-f]{1,64}\.json", path.name):
                continue
            count += 1
            if count > 32:
                wire.fail("limit", 413)
            value = self.a.files.read_json(path)
            wire.fields(value, {"schemaVersion", "file", "keys", "count"})
            if value["schemaVersion"] != 1 or value["file"] != path.stem + ".ids":
                wire.fail("invalid_reference")
            keys = codec.keys(value["keys"])
            expected = wire.number(value["count"], 1)
            result = self.a.files.copy_input(
                c, nid, eid, directory / value["file"], keys, expected
            )
            total += result["bytes"]
            if total > codec.MAX_ID_BYTES:
                wire.fail("limit", 413)
            self.a.store.put(c, nid, "selectionInput", result)
            consumed.extend((path, directory / value["file"]))
        c.execute("INSERT INTO notebook_analysis_collected VALUES(?)", (eid,))
        if count:
            self.a.store.advance(c, nid)
        return consumed
