"""Exact commands and bounded references for scientific Notebook state."""

import hashlib
import re

from .contracts import encoded
from .service import NotebookError

PREFIX = "notebook_analysis_"
KINDS = {
    "dataset": "dataset",
    "object": "object",
    "view": "view",
    "selection": "selection",
    "selectionInput": "artifact",
}
COLLECTIONS = {
    "datasets": "dataset",
    "objects": "object",
    "views": "view",
    "dataSelections": "selection",
    "selectionInputs": "selectionInput",
}
MAX_PAGE_BYTES = 192 * 1024
MAX_ITEM_BYTES = 16 * 1024


def fail(reason="invalid_command", status=422):
    raise NotebookError(PREFIX + reason, status)


def fields(value, expected, *, optional=()):
    if not isinstance(value, dict) or not set(expected) <= set(value) <= set(
        expected
    ) | set(optional):
        fail()
    return value


def text(value, maximum=1024, *, empty=True):
    if not isinstance(value, str) or (not empty and not value) or "\0" in value:
        fail()
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        fail()
    if size > maximum:
        fail("limit", 413)
    return value


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        fail("invalid_reference")
    return value


def number(value, minimum=0):
    if type(value) is not int or value < minimum:
        fail("invalid_reference")
    return value


def sequence(value, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        fail()
    return value


def ref(value, kind):
    fields(value, {kind + "Id", kind + "Revision"})
    identity(value[kind + "Id"])
    number(value[kind + "Revision"], 1)
    return value


def refs(values, kind, maximum=32):
    seen = set()
    for value in sequence(values, maximum):
        ref(value, kind)
        key = value[kind + "Id"]
        if key in seen:
            fail("invalid_reference")
        seen.add(key)
    return values


def kernel(value):
    fields(
        value,
        {
            "language",
            "kernelInstanceId",
            "generation",
            "environmentId",
            "environmentRevision",
        },
    )
    if value["language"] not in {"python", "r"}:
        fail("invalid_reference")
    for key in ("kernelInstanceId", "environmentId"):
        identity(value[key])
    for key in ("generation", "environmentRevision"):
        number(value[key], 1)
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail("invalid_reference")
    return value


def artifact(value):
    fields(value, {"artifactId", "sha256", "bytes"})
    identity(value["artifactId"])
    sha(value["sha256"])
    number(value["bytes"])
    return value


def guard(value):
    fields(value, {"analysisRevision", "objects", "selections"})
    number(value["analysisRevision"])
    refs(value["objects"], "object")
    refs(value["selections"], "selection")
    return value


def identity_spec(value):
    if value == {"kind": "index"}:
        return value
    fields(value, {"kind", "names"})
    if value["kind"] != "columns":
        fail()
    names = sequence(value["names"], 4, 1)
    for name in names:
        text(name, 128, empty=False)
    if len(set(names)) != len(names):
        fail()
    return value


def command(nid, value):
    identity(nid)
    fields(value, {"schemaVersion", "commandId", "expected", "action", "arguments"})
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        fail()
    identity(value["commandId"])
    expected = fields(
        value["expected"], {"analysisRevision", "kernels", "objects", "selections"}
    )
    guard({k: expected[k] for k in ("analysisRevision", "objects", "selections")})
    kernels = sequence(expected["kernels"], 2)
    for item in kernels:
        kernel(item)
    if len({item["language"] for item in kernels}) != len(kernels):
        fail("invalid_reference")
    action, args = value["action"], value["arguments"]
    if action == "object.register":
        fields(
            args,
            {
                "kernel",
                "symbol",
                "adapter",
                "sourceRefs",
                "declaredMetadata",
                "rowIdentity",
                "columnIdentity",
                "tableAxis",
                "declaredMatrix",
            },
        )
        kernel(args["kernel"])
        symbol = text(args["symbol"], 128, empty=False)
        language = args["kernel"]["language"]
        if language == "python":
            if not symbol.isidentifier():
                fail()
        elif not re.fullmatch(r"(?:[A-Za-z]|\.(?![0-9]))[A-Za-z0-9._]*", symbol):
            fail()
        if args["adapter"] not in (
            {"anndata", "python_table"} if language == "python" else {"r_rds"}
        ):
            fail("unsupported_adapter")
        for item in sequence(args["sourceRefs"], 8):
            artifact(item)
        metadata = fields(
            args["declaredMetadata"],
            {"organism", "sampleColumn", "conditionColumn", "donorColumn"},
        )
        for item in metadata.values():
            if item is not None:
                text(item, 128)
        for name in ("rowIdentity", "columnIdentity"):
            identity_spec(args[name])
        if args["tableAxis"] not in {"row", "column"}:
            fail()
        matrix = fields(args["declaredMatrix"], {"assay", "layer", "semantics"})
        for key in ("assay", "layer"):
            if matrix[key] is not None:
                text(matrix[key], 128, empty=False)
        if matrix["semantics"] not in {"raw_counts", "lognorm", "other", "unknown"}:
            fail()
    elif action in {"object.inspect", "object.remove"}:
        ref(fields(args, {"object"})["object"], "object")
    elif action == "selection.create":
        fields(args, {"view", "members"})
        ref(args["view"], "view")
        members = args["members"]
        if isinstance(members, dict) and members.get("kind") == "ids":
            fields(members, {"kind", "values"})
            for row in sequence(members["values"], 100, 1):
                for item in sequence(row, 4, 1):
                    text(item, 512)
        elif isinstance(members, dict) and members.get("kind") == "idArtifact":
            artifact(fields(members, {"kind", "artifact"})["artifact"])
        else:
            fail()
    elif action == "selection.remap":
        fields(
            args,
            {"selection", "targetView", "expectedMemberCount", "expectedMembersSha256"},
        )
        ref(args["selection"], "selection")
        ref(args["targetView"], "view")
        number(args["expectedMemberCount"], 1)
        sha(args["expectedMembersSha256"])
    elif action == "selection.clear":
        ref(fields(args, {"selection"})["selection"], "selection")
    else:
        fail()
    payload = encoded({"notebookId": nid, "command": value})
    if len(payload) > 64 * 1024:
        fail("limit", 413)
    return hashlib.sha256(payload).hexdigest()
