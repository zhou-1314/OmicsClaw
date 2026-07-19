"""Unified execution-time checks for declarative Skill contracts.

``skill.yaml`` output files are an inventory: method branches and optional
plots may legitimately omit many entries.  This Module therefore verifies only
facts whose representation makes them unconditional guarantees:

* the standard ``result.json`` envelope when it is declared;
* extra top-level keys explicitly named by ``result_json.required_keys``;
* unconditional Semantic artifact contracts; and
* file/artifact guarantees for the method that actually ran.

Security is reported separately.  A manifest security block is a reviewed,
declarative capability statement; it is not an OS sandbox or proof that a
process stayed inside its declared network/filesystem surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Any, Mapping

from omicsclaw.common.output_claim import (
    collect_output_claim_identities,
    is_scientific_output_file,
)
from omicsclaw.common.report import (
    SCAFFOLD_STATUS,
    extract_method_name,
    validate_result_envelope,
)
from omicsclaw.skill.execution.environment import scrub_internal_control_credentials


@dataclass(frozen=True, slots=True)
class ContractViolation:
    """One privacy-minimal execution-contract violation."""

    code: str
    message: str
    subject: str = ""


@dataclass(frozen=True, slots=True)
class SkillSecurityStatus:
    """Honest status of the declarative security metadata."""

    reviewed: bool
    enforcement: str
    data_egress: str = ""
    network: str = ""
    writes: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reviewed": self.reviewed,
            "enforcement": self.enforcement,
        }
        if self.reviewed:
            payload.update(
                {
                    "data_egress": self.data_egress,
                    "network": self.network,
                    "writes": self.writes,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class SkillExecutionContractReport:
    """Result of checking one completed Skill output directory."""

    violations: tuple[ContractViolation, ...]
    actual_method: str | None = None
    security: SkillSecurityStatus = SkillSecurityStatus(False, "undeclared")

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(violation.code for violation in self.violations)

    def format_failure(self) -> str:
        if self.ok:
            return ""
        return "Skill output contract failed:\n" + "\n".join(
            f"- [{violation.code}] {violation.message}"
            for violation in self.violations
        )


def describe_skill_security(skill_info: Mapping[str, Any]) -> SkillSecurityStatus:
    """Return whether security metadata was explicitly reviewed.

    Missing metadata deliberately remains ``undeclared``.  It must never be
    reconstructed as a false ``network:none`` claim.
    """

    raw = skill_info.get("security_contract") or {}
    reviewed = bool(skill_info.get("security_reviewed"))
    if not reviewed or not isinstance(raw, Mapping):
        return SkillSecurityStatus(reviewed=False, enforcement="undeclared")
    return SkillSecurityStatus(
        reviewed=True,
        enforcement="declarative",
        data_egress=str(raw.get("data_egress") or ""),
        network=str(raw.get("network") or ""),
        writes=str(raw.get("writes") or ""),
    )


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _safe_relative_path(value: object) -> str:
    """Normalize a manifest path while keeping it under the run directory.

    Early migrated manifests sometimes used the documentation token
    ``output_dir/<file>``.  It names the run root, not a real nested folder, so
    the prefix is normalized at this compatibility Seam.
    """

    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    parts = list(path.parts)
    if parts and parts[0] == "output_dir":
        parts = parts[1:]
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or not parts
        or any(part in {".", ".."} for part in parts)
        or any("<" in part or ">" in part or "..." in part for part in parts)
    ):
        raise ValueError(f"unsafe relative output path: {raw!r}")
    return PurePosixPath(*parts).as_posix()


def _declared_result_path(outputs: Mapping[str, Any]) -> str | None:
    candidates: list[object] = list(_as_sequence(outputs.get("files")))
    candidates.extend(
        artifact.get("path")
        for artifact in _as_sequence(outputs.get("artifacts"))
        if isinstance(artifact, Mapping)
    )
    for scope in _as_sequence(outputs.get("method_scopes")):
        if not isinstance(scope, Mapping):
            continue
        candidates.extend(_as_sequence(scope.get("files")))
        candidates.extend(
            artifact.get("path")
            for artifact in _as_sequence(scope.get("artifacts"))
            if isinstance(artifact, Mapping)
        )
    for candidate in candidates:
        raw = str(candidate or "").replace("\\", "/")
        if PurePosixPath(raw).name != "result.json":
            continue
        try:
            return _safe_relative_path(candidate)
        except ValueError:
            return raw
    required_keys = _as_sequence(
        _as_mapping(outputs.get("result_json")).get("required_keys")
    )
    return "result.json" if required_keys else None


def _resolved_output_path(root: Path, relative: str) -> Path:
    """Return one lexical output path after validating resolved containment.

    The resolved path is used only to prove containment.  Returning it would
    erase an in-tree alias from the caller's path before the shared ownership
    predicate can reject that alias.
    """

    resolved_root = root.resolve(strict=False)
    lexical_candidate = root / relative
    resolved_candidate = lexical_candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"output path resolves outside the run directory: {relative!r}"
        ) from exc
    return lexical_candidate


def _guarantee_paths(
    outputs: Mapping[str, Any],
    *,
    actual_method: str | None,
) -> tuple[tuple[str, object], ...]:
    guarantees: list[tuple[str, object]] = []
    for artifact in _as_sequence(outputs.get("artifacts")):
        if isinstance(artifact, Mapping):
            guarantees.append(("artifact_missing", artifact.get("path")))
    if not actual_method:
        return tuple(guarantees)
    for scope in _as_sequence(outputs.get("method_scopes")):
        if not isinstance(scope, Mapping):
            continue
        methods = {str(value) for value in _as_sequence(scope.get("methods"))}
        if actual_method not in methods:
            continue
        guarantees.extend(
            ("method_output_missing", path)
            for path in _as_sequence(scope.get("files"))
        )
        guarantees.extend(
            ("method_output_missing", artifact.get("path"))
            for artifact in _as_sequence(scope.get("artifacts"))
            if isinstance(artifact, Mapping)
        )
    # A method artifact path also appears in the scope's file inventory in
    # valid v2 manifests.  Keep one check and one diagnostic per path.
    return tuple(dict.fromkeys(guarantees))


def primary_anndata_output_path(outputs: Mapping[str, Any]) -> str | None:
    """Resolve the current v2 primary-AnnData convention.

    ``processed.h5ad`` wins when present; the two existing non-standard
    producers expose exactly one ``.h5ad`` inventory path.  Ambiguous manifests
    fail closed until representation grows an explicit primary path field.
    """

    normalized: list[str] = []
    for raw_path in _as_sequence(outputs.get("files")):
        if not str(raw_path or "").replace("\\", "/").lower().endswith(".h5ad"):
            continue
        try:
            normalized.append(_safe_relative_path(raw_path))
        except ValueError:
            continue
    candidates = tuple(dict.fromkeys(normalized))
    conventional = tuple(
        path for path in candidates if PurePosixPath(path).name == "processed.h5ad"
    )
    if len(conventional) == 1:
        return conventional[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


_ANNDATA_READ_PROBE = """
import sys

try:
    import anndata
except BaseException:
    raise SystemExit(70)

backed = None
try:
    try:
        backed = anndata.read_h5ad(sys.argv[1], backed="r")
    except BaseException:
        raise SystemExit(1)
finally:
    file_manager = getattr(backed, "file", None)
    close = getattr(file_manager, "close", None)
    if callable(close):
        try:
            close()
        except BaseException:
            raise SystemExit(1)
"""


def _anndata_is_readable(
    path: Path,
    *,
    runtime_python: str | None,
    runtime_env: Mapping[str, str] | None,
    runtime_cwd: str | Path | None,
) -> bool:
    """Probe AnnData with the same Python runtime that produced the output."""

    python = str(runtime_python or sys.executable)
    source_env = (
        os.environ
        if runtime_env is None
        else {str(key): str(value) for key, value in runtime_env.items()}
    )
    env = scrub_internal_control_credentials(source_env)
    try:
        completed = subprocess.run(
            [python, "-P", "-c", _ANNDATA_READ_PROBE, str(path)],
            cwd=(str(runtime_cwd) if runtime_cwd is not None else None),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("AnnData runtime validator unavailable") from exc
    if completed.returncode == 70:
        raise RuntimeError("AnnData runtime validator unavailable")
    return completed.returncode == 0


def verify_skill_run_outputs(
    skill_info: Mapping[str, Any],
    output_dir: str | Path,
    *,
    requested_method: str | None = None,
    runtime_python: str | None = None,
    runtime_env: Mapping[str, str] | None = None,
    runtime_cwd: str | Path | None = None,
) -> SkillExecutionContractReport:
    """Verify a successful subprocess against its declared output guarantees.

    Legacy/uncontracted registry entries remain compatible: an absent
    ``output_contract`` has no facts to verify.  Formal v2 skills expose the
    contract through :class:`~omicsclaw.skill.lazy_metadata.LazySkillMetadata`.
    """

    outputs = _as_mapping(skill_info.get("output_contract"))
    security = describe_skill_security(skill_info)
    if not outputs:
        return SkillExecutionContractReport((), requested_method, security)

    root = Path(output_dir)
    claim_identities = collect_output_claim_identities(root)
    violations: list[ContractViolation] = []
    payload: dict[str, Any] | None = None
    result_path = _declared_result_path(outputs)
    if result_path is not None:
        try:
            normalized_result_path = _safe_relative_path(result_path)
        except ValueError:
            violations.append(
                ContractViolation(
                    "contract_path_invalid",
                    "declared result.json path is not a safe output-relative path",
                    str(result_path),
                )
            )
            normalized_result_path = ""
        result_file: Path | None = None
        if normalized_result_path:
            try:
                result_file = _resolved_output_path(root, normalized_result_path)
            except ValueError:
                violations.append(
                    ContractViolation(
                        "contract_path_invalid",
                        "declared result.json resolves outside the run directory",
                        normalized_result_path,
                    )
                )
        if result_file is not None and not is_scientific_output_file(
            result_file,
            output_root=root,
            claim_identities=claim_identities,
        ):
            violations.append(
                ContractViolation(
                    "result_json_missing",
                    "declared result.json was not produced",
                    normalized_result_path,
                )
            )
        elif result_file is not None:
            try:
                decoded = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                violations.append(
                    ContractViolation(
                        "result_json_invalid",
                        "declared result.json is unreadable or invalid JSON",
                        normalized_result_path,
                    )
                )
            else:
                if isinstance(decoded, dict):
                    payload = decoded
                problems = validate_result_envelope(decoded)
                if problems:
                    violations.append(
                        ContractViolation(
                            "result_envelope_invalid",
                            "; ".join(problems),
                            normalized_result_path,
                        )
                    )
                if isinstance(decoded, dict) and decoded.get("status") == SCAFFOLD_STATUS:
                    violations.append(
                        ContractViolation(
                            "result_status_invalid",
                            "scaffold output is not a completed scientific run",
                            "status",
                        )
                    )
                if isinstance(decoded, dict):
                    for key in _as_sequence(
                        _as_mapping(outputs.get("result_json")).get("required_keys")
                    ):
                        name = str(key)
                        if name not in decoded:
                            violations.append(
                                ContractViolation(
                                    "result_required_key_missing",
                                    f"result.json is missing required top-level key {name!r}",
                                    name,
                                )
                            )

    anndata_contract = _as_mapping(outputs.get("anndata"))
    if anndata_contract.get("saves_h5ad") is True:
        primary_anndata = primary_anndata_output_path(outputs)
        if primary_anndata is None:
            violations.append(
                ContractViolation(
                    "anndata_contract_invalid",
                    "saves_h5ad requires one unambiguous primary .h5ad inventory path",
                    "interface.outputs.anndata",
                )
            )
        else:
            try:
                anndata_path = _resolved_output_path(root, primary_anndata)
            except ValueError:
                violations.append(
                    ContractViolation(
                        "contract_path_invalid",
                        "declared primary AnnData resolves outside the run directory",
                        primary_anndata,
                    )
                )
            else:
                if not is_scientific_output_file(
                    anndata_path,
                    output_root=root,
                    claim_identities=claim_identities,
                ):
                    violations.append(
                        ContractViolation(
                            "anndata_missing",
                            "declared primary AnnData was not produced as an owned output file",
                            primary_anndata,
                        )
                    )
                elif not _anndata_is_readable(
                    anndata_path,
                    runtime_python=runtime_python,
                    runtime_env=runtime_env,
                    runtime_cwd=runtime_cwd,
                ):
                    violations.append(
                        ContractViolation(
                            "anndata_invalid",
                            "declared primary AnnData is not a readable .h5ad container",
                            primary_anndata,
                        )
                    )

    actual_method = extract_method_name(payload, fallback=requested_method)
    for missing_code, raw_path in _guarantee_paths(
        outputs,
        actual_method=actual_method,
    ):
        try:
            relative = _safe_relative_path(raw_path)
        except ValueError:
            violations.append(
                ContractViolation(
                    "contract_path_invalid",
                    "declared output guarantee is not a safe output-relative path",
                    str(raw_path or ""),
                )
            )
            continue
        try:
            output_path = _resolved_output_path(root, relative)
        except ValueError:
            violations.append(
                ContractViolation(
                    "contract_path_invalid",
                    "declared output guarantee resolves outside the run directory",
                    relative,
                )
            )
            continue
        if not is_scientific_output_file(
            output_path,
            output_root=root,
            claim_identities=claim_identities,
        ):
            label = (
                "declared Semantic artifact was not produced"
                if missing_code == "artifact_missing"
                else f"method {actual_method!r} did not produce its declared output"
            )
            violations.append(ContractViolation(missing_code, label, relative))

    return SkillExecutionContractReport(
        tuple(violations),
        actual_method=actual_method,
        security=security,
    )


__all__ = [
    "ContractViolation",
    "SkillExecutionContractReport",
    "SkillSecurityStatus",
    "describe_skill_security",
    "primary_anndata_output_path",
    "verify_skill_run_outputs",
]
