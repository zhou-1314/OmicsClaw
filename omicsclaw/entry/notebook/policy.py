"""Versioned owner sandbox policy, without model or proxy credentials."""

import hashlib

from omicsclaw.sandbox.config import SandboxConfig, SandboxMode

from .contracts import encoded
from .service import NotebookError

_SEQUENCES = (
    "egress_groups",
    "egress_allow",
    "egress_endpoints",
    "allowed_domains",
    "extra_allowed_domains",
    "allow_private_cidr",
    "extra_read_paths",
    "extra_write_paths",
    "deny_read_paths",
    "deny_write_globs",
    "no_proxy",
)
_SCALARS = (
    "mode",
    "backend",
    "egress",
    "sandbox_kernels",
    "pids_limit",
    "nofile",
    "start_timeout_s",
    "stop_grace_s",
)


def encode_policy(config):
    if config is None or config.mode == SandboxMode.OFF:
        return {"version": 1, "config": None}
    if config.upstream_proxy or config.scrubbed_env or config.mounts:
        raise NotebookError("invalid_notebook_policy")
    document = {key: getattr(config, key) for key in _SCALARS}
    document.update(
        {
            key: sorted(str(value) for value in getattr(config, key))
            for key in _SEQUENCES
        }
    )
    return {"version": 1, "config": document}


def decode_policy(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "config"}
        or value["version"] != 1
    ):
        raise NotebookError("invalid_notebook_policy")
    document = value["config"]
    if document is None:
        return None
    if not isinstance(document, dict) or set(document) != set(_SCALARS + _SEQUENCES):
        raise NotebookError("invalid_notebook_policy")
    if any(
        not isinstance(document[key], list)
        or len(document[key]) > 256
        or any(not isinstance(part, str) or len(part) > 2048 for part in document[key])
        for key in _SEQUENCES
    ):
        raise NotebookError("invalid_notebook_policy")
    if type(document["sandbox_kernels"]) is not bool or any(
        type(document[key]) not in (int, float) or not 0 <= document[key] <= 10**7
        for key in ("pids_limit", "nofile", "start_timeout_s", "stop_grace_s")
    ):
        raise NotebookError("invalid_notebook_policy")
    from pathlib import Path

    values = {**document, **{key: tuple(document[key]) for key in _SEQUENCES}}
    for key in ("allowed_domains", "extra_allowed_domains"):
        values[key] = frozenset(values[key])
    for key in ("extra_read_paths", "extra_write_paths"):
        values[key] = tuple(Path(item) for item in values[key])
    try:
        return SandboxConfig(**values)
    except (TypeError, ValueError) as error:
        raise NotebookError("invalid_notebook_policy") from error


def policy_digest(config):
    return hashlib.sha256(encoded(encode_policy(config))).hexdigest()
