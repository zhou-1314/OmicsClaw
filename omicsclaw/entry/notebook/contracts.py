"""Versioned owner commands and byte budgets for Notebook transport."""

import hashlib
import json
import math
import re

from .service import NotebookError, identifier, revision

OWNER_PROTOCOL = 1
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 64 * 1024
MAX_PAGE = 40
MAX_KERNELS = 8
MAX_PENDING = 16
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_DISK_BYTES = 1024 * 1024 * 1024
MAX_CONTROL_RSS_BYTES = 512 * 1024 * 1024
MAX_SCIENCE_RSS_BYTES = 32 * 1024 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 7 * 24 * 3600


def encoded(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def bounded(value, limit=MAX_RESPONSE_BYTES):
    if len(encoded(value)) > limit:
        raise NotebookError("notebook_response_limit", 413)
    return value


def command(document):
    fields = {
        "version",
        "notebookId",
        "cellId",
        "commandId",
        "source",
        "expectedRevision",
        "kernelInstanceId",
        "generation",
        "environmentId",
        "environmentRevision",
        "language",
        "origin",
        "approvalScope",
        "timeoutSeconds",
        "preconditions",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise NotebookError("invalid_notebook_command")
    for key in (
        "notebookId",
        "cellId",
        "commandId",
        "kernelInstanceId",
        "environmentId",
    ):
        identifier(document[key])
    for key in ("expectedRevision", "generation", "environmentRevision"):
        revision(document[key])
    user = (
        document["version"] in {1, 3}
        and document["origin"] == "user"
        and document["approvalScope"] == "explicit_user"
        and (
            document["preconditions"] == {}
            if document["version"] == 1
            else isinstance(document["preconditions"], dict)
            and set(document["preconditions"]) == {"analysis"}
        )
    )
    agent = (
        document["version"] in {2, 4}
        and document["origin"] == "agent"
        and document["approvalScope"] == "permission_gate"
    )
    if agent:
        pre = document["preconditions"]
        expected_pre = {
            "sessionId",
            "requestId",
            "token",
            "toolPosition",
        }
        if document["version"] == 4:
            expected_pre.add("analysis")
        if not isinstance(pre, dict) or set(pre) != expected_pre:
            raise NotebookError("invalid_notebook_command")
        identifier(pre["sessionId"])
        identifier(pre["requestId"])
        if (
            not isinstance(pre["token"], str)
            or len(pre["token"]) > 8192
            or type(pre["toolPosition"]) is not int
            or not 0 <= pre["toolPosition"] < 1024
        ):
            raise NotebookError("invalid_notebook_command")
    if document["version"] in {3, 4} and (user or agent):
        from .analysis_contracts import guard

        guard(document["preconditions"]["analysis"])
    if (
        type(document["version"]) is not int
        or document["language"] not in {"python", "r"}
        or not (user or agent)
    ):
        raise NotebookError("invalid_notebook_command")
    source, seconds = document["source"], document["timeoutSeconds"]
    if (
        not isinstance(source, str)
        or not source.strip()
        or len(source.encode()) > MAX_SOURCE_BYTES
        or len(encoded(source)) > MAX_SOURCE_BYTES
        or type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise NotebookError("invalid_notebook_command")
    normalized = {**document, "timeoutSeconds": float(seconds)}
    # Future model-watermark semantics require a versioned schema extension.
    # Reading a newer watermark while executing an old command is not a grant.
    return normalized, hashlib.sha256(encoded(normalized)).hexdigest()


def public_semantic(document):
    """Execution summaries never expose the controller's private request token."""
    return {
        key: (
            {k: v for k, v in value.items() if k != "token"}
            if key == "preconditions"
            else value
        )
        for key, value in document.items()
        if key != "source"
    }


def page_limit(value=20):
    if type(value) is not int or not 1 <= value <= MAX_PAGE:
        raise NotebookError("invalid_notebook_page")
    return value


def uuid_value(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{32}", value):
        raise NotebookError("invalid_notebook_identity")
    return value
