"""Evaluation Protocol digest (ADR 0074 §6.1).

Only a declared Evaluation Protocol can earn a validation level above
``smoke-only``. This module owns the protocol's version identity: a deterministic
digest that binds the executable protocol, its declared spec (id / kind / entry /
dataset reference / repeats), and the relevant pinned dependency versions. A
change to any of those — the entry bytes that encode the pass conditions, the
declared spec, or a tool version — produces a new digest, so evidence earned
under the old protocol stops applying to the current one (the freshness rule of
ADR 0074 §6.4).

The module is intentionally pure: the caller reads the entry bytes and resolves
dependency versions; this function only hashes them. The full multi-asset and
dataset-content binding is layered on as later slices wire real evaluation
execution; the spec + entry + deps binding here is the stable core.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

__all__ = ["protocol_digest"]

# The declared protocol fields that participate in the digest, in a fixed order.
_PROTOCOL_SPEC_KEYS = ("id", "kind", "entry", "dataset_ref", "repeats")


def protocol_digest(
    *,
    protocol: Mapping[str, object],
    entry_bytes: bytes,
    dependency_versions: Mapping[str, str] | None = None,
) -> str:
    """Deterministic ``sha256:`` digest of a declared protocol + executable + deps.

    ``protocol`` is the declared spec (id / kind / entry / dataset_ref /
    repeats); ``entry_bytes`` is the executable protocol's content (which encodes
    its pass conditions); ``dependency_versions`` pins the relevant tool
    versions. Sorted keys make the result stable across dict ordering and Python
    runs, so the same protocol always digests identically and two different
    protocols never collide on field ordering alone.
    """
    deps = dependency_versions or {}
    payload = {
        "spec": {key: protocol.get(key) for key in _PROTOCOL_SPEC_KEYS},
        "entry_sha256": hashlib.sha256(entry_bytes).hexdigest(),
        "dependency_versions": {key: str(deps[key]) for key in sorted(deps)},
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
