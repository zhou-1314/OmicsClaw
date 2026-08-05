"""Portable replay evidence for completed standard Skill Runs.

The shared Skill runner owns this Module.  A standard Skill Run is reproduced
by invoking the same versioned Skill through OmicsClaw again; copying Skill
implementation code into a generated notebook would create a second, drifting
execution surface.  This Module therefore emits a small data contract instead:

* ``replay.json`` freezes the Skill/source/environment identities, normalized
  input intent, effective parameters, allow-listed invocation, and deterministic
  scientific evidence;
* ``environment.json`` makes the current bounded producer evidence explicit
  without pretending it is a complete cross-machine lockfile; and
* ``replay.sh`` delegates to the stable ``oc replay`` Interface.

Fresh-run verification compares two capsules.  Integrity of a canonical Run's
captured files remains the responsibility of the Run Manifest.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence

from omicsclaw.common.output_claim import (
    atomic_write_owned_output_text,
    first_filesystem_alias_component,
    is_scientific_output_file,
)
from omicsclaw.skill.result import SkillRunAuditIdentity


REPLAY_CAPSULE_FILENAME = "replay.json"
ENVIRONMENT_EVIDENCE_FILENAME = "environment.json"
REPLAY_SCRIPT_FILENAME = "replay.sh"
REPLAY_SCHEMA_VERSION = 1
_MAX_REPLAY_BYTES = 1024 * 1024
_CANONICAL_SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256_OR_UNKNOWN = re.compile(r"(?:sha256:[0-9a-f]{64}|unknown)\Z")


class SkillReplayCapsuleError(RuntimeError):
    """A replay capsule is unsafe, malformed, or cannot be verified."""


def write_skill_replay_capsule(
    output_dir: str | Path,
    *,
    skill_alias: str,
    skill_info: Mapping[str, Any],
    result_payload: Mapping[str, Any] | None,
    audit_identity: SkillRunAuditIdentity,
    runtime_source: str,
    demo: bool,
    input_paths: Sequence[str] = (),
    forwarded_args: Sequence[str] = (),
) -> Path:
    """Emit the runner-owned replay capsule and return ``replay.json``.

    Input paths are represented by content evidence rather than absolute host
    paths.  A later replay supplies local input mappings explicitly.
    """

    root = _require_plain_output_directory(Path(output_dir))
    repro = _require_plain_reproducibility_directory(root)
    normalized_args = _normalized_forwarded_args(forwarded_args)
    input_document = _input_document(demo=demo, input_paths=input_paths)
    invocation_argv = _invocation_argv(
        skill_alias,
        input_document=input_document,
        forwarded_args=normalized_args,
    )
    environment = _environment_document(
        skill_info,
        audit_identity=audit_identity,
        runtime_source=runtime_source,
    )
    payload = dict(result_payload or {})
    capsule = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "kind": "omicsclaw.skill-replay",
        "skill_revision": {
            "skill_id": audit_identity.skill_id,
            "skill_version": audit_identity.skill_version,
            "manifest_hash": audit_identity.skill_hash,
            "source_hash": audit_identity.source_hash,
        },
        "input": input_document,
        "parameters": _effective_parameters(payload),
        "invocation": {
            "argv": invocation_argv,
            "forwarded_args": list(normalized_args),
        },
        "environment": {
            "environment_id": audit_identity.environment_id,
            "runtime_source": runtime_source,
            "evidence": f"reproducibility/{ENVIRONMENT_EVIDENCE_FILENAME}",
        },
        "verification": {
            "mode": "fresh-run-contract",
            "result_semantic_sha256": _semantic_result_sha256(payload),
            "artifacts": _declared_artifact_evidence(root, skill_info),
        },
    }
    replay_path = repro / REPLAY_CAPSULE_FILENAME
    atomic_write_owned_output_text(
        replay_path,
        output_root=root,
        text=_render_json(capsule),
        label="Skill replay capsule",
    )
    atomic_write_owned_output_text(
        repro / ENVIRONMENT_EVIDENCE_FILENAME,
        output_root=root,
        text=_render_json(environment),
        label="Skill environment evidence",
    )
    script = "\n".join(
        (
            "#!/bin/sh",
            "set -eu",
            'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
            'exec oc replay "$script_dir/replay.json" "$@"',
            "",
        )
    )
    atomic_write_owned_output_text(
        repro / REPLAY_SCRIPT_FILENAME,
        output_root=root,
        text=script,
        label="Skill replay launcher",
    )
    return replay_path


def load_skill_replay_capsule(path: str | Path) -> dict[str, Any]:
    """Read and validate one bounded, ordinary replay capsule file."""

    candidate = Path(path).expanduser()
    if first_filesystem_alias_component(candidate) is not None:
        raise SkillReplayCapsuleError("replay capsule path contains a filesystem alias")
    try:
        details = os.lstat(candidate)
    except OSError as exc:
        raise SkillReplayCapsuleError("replay capsule is unavailable") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise SkillReplayCapsuleError(
            "replay capsule must be a regular single-link file"
        )
    if details.st_size > _MAX_REPLAY_BYTES:
        raise SkillReplayCapsuleError("replay capsule exceeds the size limit")
    try:
        raw = candidate.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillReplayCapsuleError("replay capsule is not valid UTF-8 JSON") from exc
    if len(raw) > _MAX_REPLAY_BYTES or not isinstance(document, dict):
        raise SkillReplayCapsuleError("replay capsule is invalid")
    _validate_capsule_shape(document)
    return document


def verify_skill_replay_capsules(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return stable mismatch codes for one fresh replay result."""

    _validate_capsule_shape(expected)
    _validate_capsule_shape(observed)
    mismatches: list[str] = []
    for key, code in (
        ("skill_revision", "skill_revision_mismatch"),
        ("parameters", "effective_parameters_mismatch"),
    ):
        if expected[key] != observed[key]:
            mismatches.append(code)
    if _comparable_input_document(expected["input"]) != _comparable_input_document(
        observed["input"]
    ):
        mismatches.append("input_evidence_mismatch")
    if (
        expected["environment"]["environment_id"]
        != observed["environment"]["environment_id"]
    ):
        mismatches.append("environment_mismatch")
    expected_verification = expected["verification"]
    observed_verification = observed["verification"]
    if (
        expected_verification["result_semantic_sha256"]
        != observed_verification["result_semantic_sha256"]
    ):
        mismatches.append("result_semantics_mismatch")
    if expected_verification["artifacts"] != observed_verification["artifacts"]:
        mismatches.append("scientific_artifacts_mismatch")
    return tuple(mismatches)


def _require_plain_output_directory(path: Path) -> Path:
    if first_filesystem_alias_component(path) is not None:
        raise SkillReplayCapsuleError("output directory contains a filesystem alias")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillReplayCapsuleError("output directory is unavailable") from exc
    if not resolved.is_dir():
        raise SkillReplayCapsuleError("output directory is unavailable")
    return resolved


def _require_plain_reproducibility_directory(root: Path) -> Path:
    repro = root / "reproducibility"
    if first_filesystem_alias_component(repro) is not None:
        raise SkillReplayCapsuleError(
            "reproducibility directory contains a filesystem alias"
        )
    repro.mkdir(parents=False, exist_ok=True)
    if first_filesystem_alias_component(repro) is not None or not repro.is_dir():
        raise SkillReplayCapsuleError("reproducibility directory is unavailable")
    return repro


def _normalized_forwarded_args(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(value) for value in values)
    if any(
        not value
        or "\x00" in value
        or value in {"--input", "--output", "--demo"}
        or value.startswith(("--input=", "--output=", "--demo="))
        for value in normalized
    ):
        raise SkillReplayCapsuleError("replay arguments contain a runner-owned flag")
    return normalized


def _input_document(
    *,
    demo: bool,
    input_paths: Sequence[str],
) -> dict[str, Any]:
    if demo:
        if input_paths:
            raise SkillReplayCapsuleError("demo replay cannot carry file inputs")
        return {"kind": "demo"}
    if not input_paths:
        return {"kind": "none"}
    items: list[dict[str, Any]] = []
    for ordinal, raw_path in enumerate(input_paths):
        items.append(_input_evidence(str(raw_path), ordinal=ordinal))
    return {"kind": "mapped", "requires_mapping": True, "items": items}


def _invocation_argv(
    skill_alias: str,
    *,
    input_document: Mapping[str, Any],
    forwarded_args: Sequence[str],
) -> list[str]:
    argv = ["oc", "run", skill_alias]
    if input_document["kind"] == "demo":
        argv.append("--demo")
    elif input_document["kind"] == "mapped":
        for item in input_document["items"]:
            argv.extend(["--input", f"${{INPUT_{int(item['ordinal']) + 1}}}"])
    argv.extend(forwarded_args)
    return argv


def _environment_document(
    skill_info: Mapping[str, Any],
    *,
    audit_identity: SkillRunAuditIdentity,
    runtime_source: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "environment_id": audit_identity.environment_id,
        "runtime_source": runtime_source,
        "runtime_language": str(skill_info.get("runtime_language") or "python"),
        "declared_dependencies": sorted(
            {str(item) for item in (skill_info.get("requires") or ())}
        ),
        # ADR 0065 deliberately defines the current producer fingerprint as
        # bounded evidence, not a complete transitive/native environment lock.
        "reconstruction": "evidence-only",
    }


def _effective_parameters(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return {}
    raw = data.get("effective_params") or data.get("params") or {}
    return deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}


def _semantic_result_sha256(payload: Mapping[str, Any]) -> str:
    semantic = deepcopy(dict(payload))
    semantic.pop("completed_at", None)
    rendered = json.dumps(
        _json_safe(semantic),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(rendered).hexdigest()


def _declared_artifact_evidence(
    output_root: Path,
    skill_info: Mapping[str, Any],
) -> list[dict[str, Any]]:
    output_contract = skill_info.get("output_contract")
    raw_artifacts = (
        output_contract.get("artifacts", ())
        if isinstance(output_contract, Mapping)
        else ()
    )
    evidence: list[dict[str, Any]] = []
    for raw in raw_artifacts:
        if not isinstance(raw, Mapping):
            continue
        relative = str(raw.get("path") or "")
        path = output_root / relative
        if not relative or not is_scientific_output_file(path, output_root=output_root):
            raise SkillReplayCapsuleError("declared replay artifact is unavailable")
        details = path.stat()
        evidence.append(
            {
                "kind": str(raw.get("kind") or ""),
                "path": relative,
                "format": str(raw.get("format") or ""),
                "size_bytes": details.st_size,
                "sha256": _sha256_file(path),
            }
        )
    return sorted(evidence, key=lambda item: (item["kind"], item["path"]))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _render_json(value: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
            default=str,
        )
        + "\n"
    )


def _json_safe(value: Any) -> Any:
    """Preserve non-finite scientific values in strict, deterministic JSON."""

    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            label = "NaN"
        elif value > 0:
            label = "Infinity"
        else:
            label = "-Infinity"
        return {"$omicsclaw_non_finite": label}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _validate_capsule_shape(document: Mapping[str, Any]) -> None:
    if (
        document.get("schema_version") != REPLAY_SCHEMA_VERSION
        or document.get("kind") != "omicsclaw.skill-replay"
    ):
        raise SkillReplayCapsuleError("unsupported replay capsule schema")
    for key in (
        "skill_revision",
        "input",
        "parameters",
        "invocation",
        "environment",
        "verification",
    ):
        if not isinstance(document.get(key), Mapping):
            raise SkillReplayCapsuleError(f"replay capsule {key} is invalid")
    revision = document["skill_revision"]
    if set(revision) != {
        "skill_id",
        "skill_version",
        "manifest_hash",
        "source_hash",
    }:
        raise SkillReplayCapsuleError("replay Skill revision is invalid")
    skill_id = revision.get("skill_id")
    if not isinstance(skill_id, str) or not _CANONICAL_SKILL_ID.fullmatch(skill_id):
        raise SkillReplayCapsuleError("replay Skill identifier is invalid")
    for key in ("skill_version", "manifest_hash", "source_hash"):
        if not isinstance(revision.get(key), str) or not revision[key]:
            raise SkillReplayCapsuleError("replay Skill revision is invalid")
    if not _SHA256_OR_UNKNOWN.fullmatch(
        revision["manifest_hash"]
    ) or not _SHA256_OR_UNKNOWN.fullmatch(revision["source_hash"]):
        raise SkillReplayCapsuleError("replay Skill revision digest is invalid")
    input_document = document["input"]
    _validate_input_document(input_document)
    if not isinstance(document["parameters"], Mapping):
        raise SkillReplayCapsuleError("replay parameters are invalid")
    invocation = document["invocation"]
    argv = invocation.get("argv")
    forwarded = invocation.get("forwarded_args")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) for item in argv)
        or not isinstance(forwarded, list)
        or any(not isinstance(item, str) for item in forwarded)
    ):
        raise SkillReplayCapsuleError("replay invocation is invalid")
    normalized_forwarded = _normalized_forwarded_args(forwarded)
    expected_argv = _invocation_argv(
        skill_id,
        input_document=input_document,
        forwarded_args=normalized_forwarded,
    )
    if argv != expected_argv:
        raise SkillReplayCapsuleError("replay invocation does not match its evidence")
    environment = document["environment"]
    if not isinstance(environment.get("environment_id"), str):
        raise SkillReplayCapsuleError("replay environment identity is invalid")
    verification = document["verification"]
    if (
        verification.get("mode") != "fresh-run-contract"
        or not isinstance(verification.get("result_semantic_sha256"), str)
        or not isinstance(verification.get("artifacts"), list)
    ):
        raise SkillReplayCapsuleError("replay verification contract is invalid")


def _validate_input_document(document: Mapping[str, Any]) -> None:
    kind = document.get("kind")
    if kind == "demo":
        if dict(document) != {"kind": "demo"}:
            raise SkillReplayCapsuleError("demo replay input is invalid")
        return
    if kind == "none":
        if dict(document) != {"kind": "none"}:
            raise SkillReplayCapsuleError("no-input replay evidence is invalid")
        return
    if kind != "mapped" or document.get("requires_mapping") is not True:
        raise SkillReplayCapsuleError("replay input is invalid")
    items = document.get("items")
    if not isinstance(items, list) or not items:
        raise SkillReplayCapsuleError("replay input evidence is invalid")
    for ordinal, item in enumerate(items):
        _validate_input_item(item, ordinal=ordinal)


def _comparable_input_document(document: Mapping[str, Any]) -> dict[str, Any]:
    comparable = deepcopy(dict(document))
    items = comparable.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                item.pop("name", None)
    return comparable


def verify_replay_input_mappings(
    input_document: Mapping[str, Any],
    input_paths: Sequence[str],
) -> tuple[str, ...]:
    """Validate caller mappings against frozen input evidence."""

    _validate_input_document(input_document)
    if input_document.get("kind") in {"demo", "none"}:
        if input_paths:
            raise SkillReplayCapsuleError("replay does not accept input mappings")
        return ()
    expected_items = input_document["items"]
    if len(expected_items) != len(input_paths):
        raise SkillReplayCapsuleError("replay input mapping is incomplete")
    mapped: list[str] = []
    for ordinal, (expected, raw_value) in enumerate(
        zip(expected_items, input_paths, strict=True)
    ):
        observed = _input_evidence(str(raw_value), ordinal=ordinal)
        comparable_expected = {
            key: value for key, value in expected.items() if key != "name"
        }
        comparable_observed = {
            key: value for key, value in observed.items() if key != "name"
        }
        if comparable_observed != comparable_expected:
            raise SkillReplayCapsuleError("replay input evidence differs")
        mapped.append(
            str(Path(raw_value).expanduser().resolve())
            if observed["kind"] in {"file", "directory"}
            else str(raw_value)
        )
    return tuple(mapped)


def _input_evidence(raw_value: str, *, ordinal: int) -> dict[str, Any]:
    path = Path(raw_value).expanduser()
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        encoded = raw_value.encode("utf-8")
        return {
            "ordinal": ordinal,
            "kind": "freeform",
            "utf8_size_bytes": len(encoded),
            "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        }
    except OSError as exc:
        raise SkillReplayCapsuleError("replay input is unavailable") from exc
    if first_filesystem_alias_component(path) is not None:
        raise SkillReplayCapsuleError("replay input contains a filesystem alias")
    if stat.S_ISREG(details.st_mode):
        if details.st_nlink != 1:
            raise SkillReplayCapsuleError("replay input file must have one hard link")
        return {
            "ordinal": ordinal,
            "kind": "file",
            "name": path.name,
            "size_bytes": details.st_size,
            "sha256": _sha256_file(path),
        }
    if stat.S_ISDIR(details.st_mode):
        tree_sha256, file_count, size_bytes = _directory_tree_evidence(path)
        return {
            "ordinal": ordinal,
            "kind": "directory",
            "name": path.name,
            "file_count": file_count,
            "size_bytes": size_bytes,
            "tree_sha256": tree_sha256,
        }
    raise SkillReplayCapsuleError("replay input type is unsupported")


def _directory_tree_evidence(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    file_count = 0
    total_size = 0
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        if first_filesystem_alias_component(path) is not None:
            raise SkillReplayCapsuleError("replay input directory contains an alias")
        details = os.lstat(path)
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if stat.S_ISDIR(details.st_mode):
            digest.update(b"D\0" + relative + b"\0")
            continue
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise SkillReplayCapsuleError(
                "replay input directory must contain ordinary single-link files"
            )
        digest.update(b"F\0" + relative + b"\0")
        digest.update(str(details.st_size).encode("ascii") + b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        file_count += 1
        total_size += details.st_size
    return "sha256:" + digest.hexdigest(), file_count, total_size


def _validate_input_item(item: object, *, ordinal: int) -> None:
    if not isinstance(item, Mapping) or item.get("ordinal") != ordinal:
        raise SkillReplayCapsuleError("replay input evidence is invalid")
    kind = item.get("kind")
    expected_keys = {
        "file": {"ordinal", "kind", "name", "size_bytes", "sha256"},
        "directory": {
            "ordinal",
            "kind",
            "name",
            "file_count",
            "size_bytes",
            "tree_sha256",
        },
        "freeform": {"ordinal", "kind", "utf8_size_bytes", "sha256"},
    }.get(kind)
    if expected_keys is None or set(item) != expected_keys:
        raise SkillReplayCapsuleError("replay input evidence is invalid")
    for key in ("size_bytes", "file_count", "utf8_size_bytes"):
        if key in item and (
            not isinstance(item[key], int)
            or isinstance(item[key], bool)
            or item[key] < 0
        ):
            raise SkillReplayCapsuleError("replay input size evidence is invalid")
    if "name" in item and (
        not isinstance(item["name"], str)
        or not item["name"]
        or Path(item["name"]).name != item["name"]
    ):
        raise SkillReplayCapsuleError("replay input name evidence is invalid")
    digest_key = "tree_sha256" if kind == "directory" else "sha256"
    if not isinstance(item[digest_key], str) or not _SHA256.fullmatch(item[digest_key]):
        raise SkillReplayCapsuleError("replay input digest evidence is invalid")


__all__ = [
    "ENVIRONMENT_EVIDENCE_FILENAME",
    "REPLAY_CAPSULE_FILENAME",
    "REPLAY_SCRIPT_FILENAME",
    "SkillReplayCapsuleError",
    "load_skill_replay_capsule",
    "verify_replay_input_mappings",
    "verify_skill_replay_capsules",
    "write_skill_replay_capsule",
]
