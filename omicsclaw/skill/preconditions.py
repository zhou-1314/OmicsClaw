"""Evaluate runtime input state against a skill's declared input contract.

This module deliberately does not rank skills or execute them.  It converts
the ``skill.yaml`` ``interface.inputs`` contract and an observed input profile
into a small, deterministic preflight decision that routing surfaces can share.
"""

from __future__ import annotations

import bz2
import csv
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
import gzip
import io
import lzma
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from .registry import OmicsRegistry, ensure_registry_loaded


_SUFFIX_TYPED_DIRECTORY_FORMATS = frozenset({"zarr"})
_CONTENT_PROBE_LIMIT_BYTES = 1024 * 1024
_DIRECTORY_PROBE_MAX_ENTRIES = 2048
_FASTQ_FILE_TYPES = frozenset({"fastq", "fq"})


class PreconditionStatus(str, Enum):
    """Whether the selected skill can consume the observed input as-is."""

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    NEEDS_PREPARATION = "needs_preparation"


def _normalise_names(values: object) -> set[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    return {
        str(value).strip()
        for value in values  # type: ignore[union-attr]
        if str(value).strip()
    }


def _normalise_file_type(value: object) -> str:
    normalised = str(value or "").strip().lower().lstrip(".")
    for compression_suffix in (".bz2", ".gz", ".xz", ".zst"):
        if normalised.endswith(compression_suffix):
            return normalised[: -len(compression_suffix)]
    return normalised


@dataclass
class InputProfile:
    """Observed, domain-neutral facts about a candidate input.

    ``None`` means a collection was not inspected; an empty collection means
    it was inspected and the key was absent.  That distinction prevents a
    filename-only route from pretending it verified AnnData internals, table
    headers, VCF metadata, FASTQ records, or directory layouts.
    """

    file_type: str = ""
    path_kind: str = ""
    modality: str = ""
    preprocessed: bool | None = None
    obs: set[str] | None = None
    var: set[str] | None = None
    layers: set[str] | None = None
    obsm: set[str] | None = None
    uns: set[str] | None = None
    table_columns: set[str] | None = None
    table_column_count: int | None = None
    table_inspection_error: str = ""
    vcf_fileformat: str | None = None
    vcf_columns: set[str] | None = None
    vcf_info_ids: set[str] | None = None
    vcf_format_ids: set[str] | None = None
    vcf_sample_count: int | None = None
    vcf_inspection_error: str = ""
    fastq_record_valid: bool | None = None
    fastq_pairing: str = ""
    fastq_inspection_error: str = ""
    directory_signatures: set[str] | None = None
    directory_probe_truncated: bool = False
    directory_inspection_error: str = ""
    env: set[str] | None = None
    config: set[str] | None = None
    source_path: str = ""
    inspection_error: str = ""

    def __post_init__(self) -> None:
        self.file_type = _normalise_file_type(self.file_type)
        self.path_kind = str(self.path_kind or "").strip().lower()
        if (
            self.path_kind == "directory"
            and self.file_type not in _SUFFIX_TYPED_DIRECTORY_FORMATS
        ):
            self.file_type = ""
        self.modality = str(self.modality or "").strip().lower()
        self.fastq_pairing = str(self.fastq_pairing or "").strip().lower()
        for field_name in (
            "obs",
            "var",
            "layers",
            "obsm",
            "uns",
            "table_columns",
            "vcf_columns",
            "vcf_info_ids",
            "vcf_format_ids",
            "directory_signatures",
            "env",
            "config",
        ):
            setattr(self, field_name, _normalise_names(getattr(self, field_name)))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "InputProfile":
        """Build a profile from JSON/tool arguments without leaking extras."""
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in values.items() if key in fields})


@dataclass(frozen=True)
class PreconditionAssessment:
    """Deterministic preflight result for one skill and one input profile."""

    skill: str
    status: PreconditionStatus
    evaluated: bool
    execution_ready: bool
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    recommended_preparation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "status": self.status.value,
            "evaluated": self.evaluated,
            "execution_ready": self.execution_ready,
            "missing": list(self.missing),
            "reasons": list(self.reasons),
            "recommended_preparation": list(self.recommended_preparation),
        }


_PREPARATION_SKILLS = {
    "singlecell": {
        "preprocessed": "sc-preprocessing",
        "obsm.X_pca": "sc-preprocessing",
    },
    "spatial": {
        "preprocessed": "spatial-preprocess",
        "obsm.X_pca": "spatial-preprocess",
        "obsm.spatial": "spatial-preprocess",
    },
}


_PREPROCESSED_MATRIX_KINDS = {
    "normalized_expression",
    "log1p_normalized_expression",
    "scaled_expression",
}


def _file_type_from_path(path: str | Path) -> str:
    suffixes = [suffix.lower().lstrip(".") for suffix in Path(path).suffixes]
    if not suffixes:
        return ""
    if len(suffixes) >= 2 and suffixes[-1] in {"bz2", "gz", "xz", "zst"}:
        return suffixes[-2]
    return suffixes[-1]


def _read_h5ad_profile(path: str) -> dict[str, Any]:
    """Read backed AnnData metadata; split out for cache/invalidation tests."""
    adata = None
    try:
        import anndata as ad

        adata = ad.read_h5ad(path, backed="r")
        obs = set(map(str, adata.obs.columns))
        var = set(map(str, adata.var.columns))
        layers = set(map(str, adata.layers.keys()))
        obsm = set(map(str, adata.obsm.keys()))
        uns = set(map(str, adata.uns.keys()))

        input_contract = adata.uns.get("omicsclaw_input_contract", {})
        matrix_contract = adata.uns.get("omicsclaw_matrix_contract", {})
        if not isinstance(input_contract, Mapping):
            input_contract = {}
        if not isinstance(matrix_contract, Mapping):
            matrix_contract = {}
        matrix_kind = str(matrix_contract.get("X") or "").strip().lower()
        return {
            "modality": str(input_contract.get("modality") or "").strip(),
            "preprocessed": bool(
                "X_pca" in obsm
                or matrix_kind in _PREPROCESSED_MATRIX_KINDS
                or input_contract.get("preprocessed") is True
            ),
            "obs": obs,
            "var": var,
            "layers": layers,
            "obsm": obsm,
            "uns": uns,
            "inspection_error": "",
        }
    except Exception as exc:
        return {"inspection_error": str(exc)}
    finally:
        file_manager = getattr(adata, "file", None)
        close = getattr(file_manager, "close", None)
        if callable(close):
            close()


def _read_bounded_text(path: Path) -> str:
    """Read at most one MiB of decompressed text for structural probes."""
    suffix = path.suffix.lower()
    opener = {
        ".gz": gzip.open,
        ".bz2": bz2.open,
        ".xz": lzma.open,
    }.get(suffix, open)
    with opener(path, "rb") as handle:
        payload = handle.read(_CONTENT_PROBE_LIMIT_BYTES + 1)
    if len(payload) > _CONTENT_PROBE_LIMIT_BYTES:
        payload = payload[:_CONTENT_PROBE_LIMIT_BYTES]
    return payload.decode("utf-8-sig", errors="replace")


def _read_tabular_profile(path: Path, file_type: str) -> dict[str, Any]:
    try:
        text = _read_bounded_text(path)
        if not text:
            return {
                "table_columns": set(),
                "table_column_count": 0,
                "table_inspection_error": "tabular input is empty",
            }
        delimiter = "\t" if file_type == "tsv" else ","
        row = next(csv.reader(io.StringIO(text), delimiter=delimiter), [])
        columns = [column.strip() for column in row]
        if not columns or not any(columns):
            return {
                "table_columns": set(),
                "table_column_count": 0,
                "table_inspection_error": "tabular header is empty",
            }
        return {
            "table_columns": set(columns),
            "table_column_count": len(columns),
            "table_inspection_error": "",
        }
    except Exception as exc:
        return {"table_inspection_error": str(exc)}


def _read_vcf_profile(path: Path) -> dict[str, Any]:
    try:
        text = _read_bounded_text(path)
        fileformat: str | None = None
        columns: list[str] | None = None
        info_ids: set[str] = set()
        format_ids: set[str] = set()
        for line in text.splitlines():
            if line.startswith("##fileformat="):
                fileformat = line.partition("=")[2].strip() or None
            elif line.startswith("##INFO=<"):
                match = re.search(r"(?:^|[,<])ID=([^,>]+)", line)
                if match:
                    info_ids.add(match.group(1).strip())
            elif line.startswith("##FORMAT=<"):
                match = re.search(r"(?:^|[,<])ID=([^,>]+)", line)
                if match:
                    format_ids.add(match.group(1).strip())
            elif line.startswith("#CHROM"):
                columns = [value.strip() for value in line.split("\t")]
                break
            elif line and not line.startswith("#"):
                break

        if columns is None:
            return {
                "vcf_fileformat": fileformat,
                "vcf_columns": set(),
                "vcf_info_ids": info_ids,
                "vcf_format_ids": format_ids,
                "vcf_sample_count": None,
                "vcf_inspection_error": "VCF #CHROM header was not found within the probe limit",
            }
        return {
            "vcf_fileformat": fileformat,
            "vcf_columns": set(columns),
            "vcf_info_ids": info_ids,
            "vcf_format_ids": format_ids,
            "vcf_sample_count": max(len(columns) - 9, 0),
            "vcf_inspection_error": "",
        }
    except Exception as exc:
        return {"vcf_inspection_error": str(exc)}


def _fastq_pair_identity(path: Path) -> tuple[str, str | None]:
    name = path.name.lower()
    for suffix in (".gz", ".bz2", ".xz", ".zst"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    for suffix in (".fastq", ".fq"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    match = re.search(
        r"(^|[._-])(?P<read>r1|r2|read1|read2|1|2)(?=$|[._-])",
        name,
        flags=re.IGNORECASE,
    )
    if not match:
        return name, None
    token = match.group("read").lower()
    role = "R1" if token in {"r1", "read1", "1"} else "R2"
    key = name[: match.start("read")] + "readx" + name[match.end("read") :]
    return key, role


def _fastq_pairing_for_file(
    path: Path,
    companion_paths: list[str | Path] | None = None,
) -> str:
    key, role = _fastq_pair_identity(path)
    if role is None:
        return "single"
    candidates = [Path(item).expanduser() for item in (companion_paths or [])]
    match = re.search(
        r"(^|[._-])(?P<read>r1|r2|read1|read2|1|2)(?=$|[._-])",
        path.name,
        flags=re.IGNORECASE,
    )
    if match:
        token = match.group("read")
        replacement = {
            "r1": "r2",
            "r2": "r1",
            "read1": "read2",
            "read2": "read1",
            "1": "2",
            "2": "1",
        }[token.lower()]
        if token.isupper():
            replacement = replacement.upper()
        elif token[0].isupper():
            replacement = replacement.capitalize()
        mate_name = (
            path.name[: match.start("read")]
            + replacement
            + path.name[match.end("read") :]
        )
        candidates.append(path.with_name(mate_name))
    opposite = "R2" if role == "R1" else "R1"
    for candidate in candidates:
        candidate_type = _file_type_from_path(candidate)
        candidate_key, candidate_role = _fastq_pair_identity(candidate)
        if (
            candidate.exists()
            and candidate_type in _FASTQ_FILE_TYPES
            and candidate.resolve() != path.resolve()
            and candidate_key == key
            and candidate_role == opposite
        ):
            return "paired"
    return "single"


def _read_fastq_profile(
    path: Path,
    *,
    companion_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    pairing = _fastq_pairing_for_file(path, companion_paths)
    try:
        lines = _read_bounded_text(path).splitlines()
        if len(lines) < 4:
            raise ValueError("FASTQ input does not contain one complete record")
        header, sequence, separator, quality = lines[:4]
        if not header.startswith("@"):
            raise ValueError("FASTQ record header must start with '@'")
        if not sequence:
            raise ValueError("FASTQ sequence is empty")
        if not separator.startswith("+"):
            raise ValueError("FASTQ separator must start with '+'")
        if len(sequence) != len(quality):
            raise ValueError("FASTQ sequence and quality lengths differ")
        if any(ord(char) < 33 or ord(char) > 126 for char in quality):
            raise ValueError("FASTQ quality contains invalid Phred characters")
        return {
            "fastq_record_valid": True,
            "fastq_pairing": pairing,
            "fastq_inspection_error": "",
        }
    except Exception as exc:
        return {
            "fastq_record_valid": False,
            "fastq_pairing": pairing,
            "fastq_inspection_error": str(exc),
        }


def _has_any(names: set[str], *candidates: str) -> bool:
    return any(candidate.lower() in names for candidate in candidates)


def _directory_layout_signatures(path: Path) -> dict[str, Any]:
    """Recognise governed layouts from a bounded, non-symlink directory walk."""
    directories = [path]
    files: list[Path] = []
    queue: list[tuple[Path, int]] = [(path, 0)]
    visited = 0
    truncated = False
    try:
        while queue:
            current, depth = queue.pop(0)
            for item in current.iterdir():
                visited += 1
                if visited > _DIRECTORY_PROBE_MAX_ENTRIES:
                    truncated = True
                    queue.clear()
                    break
                if item.is_symlink():
                    continue
                if item.is_dir():
                    directories.append(item)
                    if depth < 3:
                        queue.append((item, depth + 1))
                elif item.is_file():
                    files.append(item)
    except OSError as exc:
        return {
            "directory_signatures": set(),
            "directory_probe_truncated": truncated,
            "directory_inspection_error": str(exc),
        }

    names_by_directory: dict[Path, set[str]] = {directory: set() for directory in directories}
    for directory in directories:
        if directory != path:
            names_by_directory.setdefault(directory.parent, set()).add(
                directory.name.lower()
            )
    for file_path in files:
        names_by_directory.setdefault(file_path.parent, set()).add(
            file_path.name.lower()
        )

    signatures: set[str] = set()
    fastq_files = [
        file_path
        for file_path in files
        if _file_type_from_path(file_path) in _FASTQ_FILE_TYPES
    ]
    fastq_profile: dict[str, Any] = {}
    if fastq_files:
        signatures.add("fastq-collection")
        groups: dict[str, set[str]] = {}
        unpaired = False
        for file_path in fastq_files:
            key, role = _fastq_pair_identity(file_path)
            if role is None:
                unpaired = True
                continue
            groups.setdefault(key, set()).add(role)
        complete_pairs = [roles for roles in groups.values() if roles == {"R1", "R2"}]
        if complete_pairs:
            signatures.add("paired-fastq")
        if complete_pairs and len(complete_pairs) == len(groups) and not unpaired:
            directory_pairing = "paired"
        elif complete_pairs:
            directory_pairing = "mixed"
        else:
            directory_pairing = "single"
        fastq_profile = _read_fastq_profile(
            fastq_files[0],
            companion_paths=fastq_files[1:],
        )
        fastq_profile["fastq_pairing"] = directory_pairing

    for directory, names in names_by_directory.items():
        has_matrix = _has_any(names, "matrix.mtx", "matrix.mtx.gz")
        has_barcodes = _has_any(names, "barcodes.tsv", "barcodes.tsv.gz")
        has_features = _has_any(
            names,
            "features.tsv",
            "features.tsv.gz",
            "genes.tsv",
            "genes.tsv.gz",
        )
        if has_matrix and has_barcodes and has_features:
            signatures.add("tenx-matrix")
        if (
            _has_any(names, "quants_mat.mtx")
            and _has_any(names, "quants_mat_rows.txt")
            and _has_any(names, "quants_mat_cols.txt")
        ) or (
            _has_any(names, "cells_x_genes.mtx")
            and _has_any(names, "cells_x_genes.genes.txt")
            and _has_any(names, "cells_x_genes.barcodes.txt")
        ):
            signatures.add("pseudoalign-output")
        if (
            _has_any(names, "spliced.mtx", "spliced.mtx.gz")
            and _has_any(names, "unspliced.mtx", "unspliced.mtx.gz")
            and has_barcodes
            and has_features
        ):
            signatures.add("starsolo-velocity")

    for directory, names in names_by_directory.items():
        if directory.name.lower() == "outs" or "outs" in names:
            outs = directory if directory.name.lower() == "outs" else directory / "outs"
            outs_names = names_by_directory.get(outs, set())
            if _has_any(
                outs_names,
                "filtered_feature_bc_matrix.h5",
                "filtered_feature_bc_matrix",
                "possorted_genome_bam.bam",
            ):
                signatures.add("cellranger-output")
        if directory.name.lower() == "solo.out" and _has_any(names, "gene", "velocyto"):
            signatures.add("starsolo-output")
        if _has_any(names, "solo.out"):
            solo_names = names_by_directory.get(directory / "Solo.out", set())
            if _has_any(solo_names, "gene", "velocyto"):
                signatures.add("starsolo-output")
    return {
        "directory_signatures": signatures,
        "directory_probe_truncated": truncated,
        "directory_inspection_error": "",
        **fastq_profile,
    }


@lru_cache(maxsize=64)
def _cached_h5ad_profile(
    resolved_path: str,
    mtime_ns: int,
    size: int,
) -> dict[str, Any]:
    """Cache metadata by file identity; mtime/size changes invalidate it."""
    del mtime_ns, size
    return _read_h5ad_profile(resolved_path)


def probe_input_profile(
    path: str | Path,
    *,
    modality: str = "",
    use_cache: bool = True,
    companion_paths: list[str | Path] | None = None,
) -> InputProfile:
    """Inspect lightweight input structure without loading analysis payloads.

    All paths yield at least filename-derived facts.  ``.h5ad`` uses AnnData's
    backed read mode to expose declared obs/var/layer/obsm/uns keys and the
    OmicsClaw input/matrix contracts. CSV/TSV, VCF, and FASTQ readers consume at
    most one MiB of decompressed text; directory traversal is depth- and entry-
    bounded and emits only governed semantic signatures. Inspection failure is
    recorded rather than raised so callers can fail into preflight instead of
    crashing routing.
    """
    source = Path(path).expanduser()
    path_kind = "directory" if source.is_dir() else "file"
    basic = {
        "file_type": _file_type_from_path(source),
        "path_kind": path_kind,
        "modality": modality,
        "source_path": str(source),
        "env": set(os.environ),
    }
    if not source.exists():
        return InputProfile(
            **basic,
            inspection_error=f"input path does not exist: {source}",
        )
    if path_kind == "directory":
        return InputProfile(**basic, **_directory_layout_signatures(source))
    if not source.is_file():
        return InputProfile(
            **basic,
            inspection_error=f"input is not a regular file: {source}",
        )
    if basic["file_type"] in {"csv", "tsv"}:
        return InputProfile(
            **basic,
            **_read_tabular_profile(source, str(basic["file_type"])),
        )
    if basic["file_type"] == "vcf":
        return InputProfile(**basic, **_read_vcf_profile(source))
    if basic["file_type"] in _FASTQ_FILE_TYPES:
        return InputProfile(
            **basic,
            **_read_fastq_profile(source, companion_paths=companion_paths),
        )
    if basic["file_type"] != "h5ad":
        return InputProfile(**basic)
    if not source.is_file():
        return InputProfile(
            **basic,
            inspection_error=f"input file does not exist: {source}",
        )

    try:
        resolved = source.resolve()
        stat = resolved.stat()
        metadata = (
            _cached_h5ad_profile(
                str(resolved),
                stat.st_mtime_ns,
                stat.st_size,
            )
            if use_cache
            else _read_h5ad_profile(str(resolved))
        )
        profiled = dict(basic)
        profiled.update(metadata)
        profiled["modality"] = modality or str(metadata.get("modality") or "")
        return InputProfile(**profiled)
    except Exception as exc:
        return InputProfile(**basic, inspection_error=str(exc))


_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


def _execution_input_kind(
    value: str,
    *,
    declared_file_types: set[str],
    known_file_types: set[str],
    declared_path_kinds: set[str],
) -> str:
    """Classify one execution input as ``file``, ``directory``, or ``freeform``.

    Existing paths provide authoritative kinds. Missing explicit paths and
    known suffixes remain auditable so typos cannot bypass the gate. URLs,
    DOIs, accessions, and prose are freeform and are accepted only when the
    selected skill declares that kind.
    """
    stripped = value.strip()
    if not stripped:
        return "freeform"
    parsed = urlparse(stripped)
    if parsed.scheme.lower() in {"http", "https", "ftp", "s3", "gs"}:
        return "freeform"
    if _DOI_PATTERN.match(stripped):
        return "freeform"

    candidate = Path(stripped).expanduser()
    if candidate.is_dir():
        return "directory"
    if candidate.exists():
        return "file"
    explicit_path_syntax = bool(
        candidate.is_absolute()
        or stripped.startswith(("./", "../", "~"))
        or re.match(r"^[A-Za-z]:[\\/]", stripped)
    )
    if any(char.isspace() for char in stripped) and not explicit_path_syntax:
        return "freeform"
    file_type = _file_type_from_path(candidate)
    if file_type in declared_file_types or file_type in known_file_types:
        return "file"
    if explicit_path_syntax:
        if not file_type and declared_path_kinds == {"directory"}:
            return "directory"
        return "file"
    return "freeform"


def _blocked_execution_assessment(
    skill_name: str,
    info: Mapping[str, Any],
    *,
    missing: str,
    reason: str,
) -> PreconditionAssessment:
    return PreconditionAssessment(
        skill=str(info.get("alias") or skill_name),
        status=PreconditionStatus.BLOCKED,
        evaluated=True,
        execution_ready=False,
        missing=[missing],
        reasons=[reason],
    )


def _combine_execution_assessments(
    skill_name: str,
    assessments: list[PreconditionAssessment],
) -> PreconditionAssessment:
    """Collapse repeated-input assessments into one stable gate decision."""
    if len(assessments) == 1:
        return assessments[0]

    blocked = any(item.status is PreconditionStatus.BLOCKED for item in assessments)
    status = (
        PreconditionStatus.BLOCKED if blocked else PreconditionStatus.NEEDS_PREPARATION
    )
    missing: list[str] = []
    reasons: list[str] = []
    recommendations: list[str] = []
    for item in assessments:
        missing.extend(item.missing)
        reasons.extend(item.reasons)
        recommendations.extend(item.recommended_preparation)
    return PreconditionAssessment(
        skill=assessments[0].skill if assessments else skill_name,
        status=status,
        evaluated=True,
        execution_ready=False,
        missing=missing,
        reasons=reasons,
        recommended_preparation=list(dict.fromkeys(recommendations)),
    )


def preflight_skill_execution(
    skill_name: str,
    *,
    input_path: str | None = None,
    input_paths: list[str] | None = None,
    demo: bool = False,
    session_path: str | None = None,
    companion_paths: list[str] | None = None,
    registry: OmicsRegistry | None = None,
) -> PreconditionAssessment | None:
    """Audit explicit execution inputs through the shared runner seam.

    ``None`` means the gate is not applicable: demo mode, no input, or a
    documented non-local/free-form input.  Any returned assessment was based
    on an observed local input and is therefore authoritative for execution.
    Callers must refuse execution when ``execution_ready`` is false.
    """
    if demo:
        return None

    registry = registry or ensure_registry_loaded()
    info = registry.skills.get(skill_name)
    if not info:
        return None
    contract = info.get("input_contract") or {}
    path_kinds = {
        str(value).strip().lower()
        for value in (contract.get("path_kinds") or ["file"])
    }
    declared_file_types = {
        _normalise_file_type(value)
        for value in contract.get("file_types", [])
        if _normalise_file_type(value) not in {"", "*"}
    }
    known_file_types = {
        _normalise_file_type(value)
        for candidate_info in registry.skills.values()
        for value in (candidate_info.get("input_contract") or {}).get("file_types", [])
        if _normalise_file_type(value) not in {"", "*"}
    }
    candidates = list(input_paths or []) or ([input_path] if input_path else [])
    session_supplied_input = False
    if not candidates and session_path:
        try:
            from omicsclaw.common.session import SpatialSession

            session = SpatialSession.load(session_path)
            if session.h5ad_path:
                candidates = [session.h5ad_path]
                session_supplied_input = True
            else:
                return _blocked_execution_assessment(
                    skill_name,
                    info,
                    missing="session",
                    reason="session does not contain a usable local input path",
                )
        except Exception as exc:
            return _blocked_execution_assessment(
                skill_name,
                info,
                missing="session",
                reason=f"session input inspection failed: {exc}",
            )

    classified_inputs: list[tuple[int, str, str]] = []
    for index, value in enumerate(candidates, start=1):
        if not value:
            continue
        input_kind = _execution_input_kind(
            value,
            declared_file_types=declared_file_types,
            known_file_types=known_file_types,
            declared_path_kinds=path_kinds,
        )
        if session_supplied_input and input_kind == "freeform":
            # A session field is a persisted local path, never literature text.
            input_kind = "file"
        classified_inputs.append((index, value, input_kind))

    if not classified_inputs:
        return None

    assessments: list[PreconditionAssessment] = []
    for input_index, value, input_kind in classified_inputs:
        if input_kind not in path_kinds:
            assessment = _blocked_execution_assessment(
                skill_name,
                info,
                missing="path_kind",
                reason=(
                    f"{input_kind} input is incompatible; "
                    f"declared path kinds are {sorted(path_kinds)}"
                ),
            )
            if len(candidates) > 1:
                assessment = replace(
                    assessment,
                    missing=[
                        f"input[{input_index}].{name}" for name in assessment.missing
                    ],
                    reasons=[
                        f"input[{input_index}]: {reason}"
                        for reason in assessment.reasons
                    ],
                )
            assessments.append(assessment)
            continue
        if input_kind == "freeform":
            continue

        is_directory = input_kind == "directory"
        # Execution checks always inspect the current file instead of reusing
        # a resolver cache entry.  A later path replacement is still an OS-level
        # TOCTOU concern, but stale cached metadata cannot authorize this run.
        observed_companions = [
            companion
            for _, candidate, candidate_kind in classified_inputs
            for companion in [candidate]
            if candidate != value and candidate_kind == "file"
        ]
        observed_companions.extend(companion_paths or [])
        profile = probe_input_profile(
            value,
            use_cache=False,
            companion_paths=observed_companions,
        )
        # AnnData data-shape keys remain format-specific. Non-H5AD facts are
        # enforced only when the manifest opts into a matching ``content``
        # contract; unrepresented PDF/text semantics are never guessed.
        assessment = evaluate_skill_preconditions(
            skill_name,
            profile,
            registry=registry,
            require_verified_modality=False,
            require_verified_file_type=not is_directory,
            require_observed_data_shape=profile.file_type == "h5ad",
        )
        if len(candidates) > 1:
            assessment = replace(
                assessment,
                missing=[f"input[{input_index}].{name}" for name in assessment.missing],
                reasons=[
                    f"input[{input_index}]: {reason}" for reason in assessment.reasons
                ],
            )
        assessments.append(assessment)
    if not assessments:
        # Every supplied input was an explicitly allowed freeform value.
        return None
    if all(item.execution_ready for item in assessments):
        return (
            assessments[0]
            if len(assessments) == 1
            else PreconditionAssessment(
                skill=assessments[0].skill,
                status=PreconditionStatus.ELIGIBLE,
                evaluated=True,
                execution_ready=True,
            )
        )
    return _combine_execution_assessments(skill_name, assessments)


def format_precondition_failure(assessment: PreconditionAssessment) -> str:
    """Render a stable, actionable execution-gate diagnostic."""
    from omicsclaw.common.user_guidance import format_user_guidance_payload

    guidance = (
        [
            "Run one of these preparation skills first: "
            + ", ".join(assessment.recommended_preparation)
        ]
        if assessment.recommended_preparation
        else ["Prepare or replace the input, then retry the same skill."]
    )
    payload = format_user_guidance_payload(
        {
            "kind": "preflight",
            "status": "blocked",
            "skill": assessment.skill,
            "precondition_status": assessment.status.value,
            "missing_requirements": assessment.reasons or assessment.missing,
            "guidance": guidance,
        }
    )
    lines = [
        payload,
        f"Skill `{assessment.skill}` failed the execution precondition gate.",
        f"Precondition status: {assessment.status.value}",
    ]
    if assessment.missing:
        lines.append("Missing preconditions: " + ", ".join(assessment.missing))
    if assessment.reasons:
        lines.append("Reasons:")
        lines.extend(f"- {reason}" for reason in assessment.reasons)
    if assessment.recommended_preparation:
        lines.append(
            "Recommended preparation: "
            + ", ".join(f"`{skill}`" for skill in assessment.recommended_preparation)
        )
    lines.append(
        "Prepare or replace the input, then retry the same skill before execution."
    )
    return "\n".join(lines)


def _required_names(data_shape: Mapping[str, Any], key: str) -> set[str]:
    return _normalise_names(data_shape.get(key, [])) or set()


def evaluate_skill_preconditions(
    skill_name: str,
    profile: InputProfile | Mapping[str, Any],
    *,
    registry: OmicsRegistry | None = None,
    require_verified_modality: bool = True,
    require_verified_file_type: bool = True,
    require_observed_data_shape: bool = True,
) -> PreconditionAssessment:
    """Evaluate ``profile`` against ``skill_name``'s declared input contract.

    Semantic routing keeps ``require_verified_modality=True`` so an unknown
    input identity cannot masquerade as an eligible auto-route.  Explicit
    execution sets it false: choosing a named skill supplies user intent, while
    a positively observed incompatible modality still blocks execution.
    """
    registry = registry or ensure_registry_loaded()
    info = registry.skills.get(skill_name)
    if not info:
        return PreconditionAssessment(
            skill=skill_name,
            status=PreconditionStatus.BLOCKED,
            evaluated=True,
            execution_ready=False,
            missing=["skill"],
            reasons=[f"unknown skill '{skill_name}'"],
        )

    profile = (
        profile
        if isinstance(profile, InputProfile)
        else InputProfile.from_mapping(profile)
    )
    contract = info.get("input_contract") or {}
    modalities = {
        str(value).strip().lower() for value in contract.get("modalities", [])
    }
    file_types = {
        _normalise_file_type(value) for value in contract.get("file_types", [])
    }
    preconditions = contract.get("preconditions") or {}
    data_shape = preconditions.get("data_shape") or {}
    content = preconditions.get("content") or {}

    blocked: list[str] = []
    preparation: list[str] = []
    missing: list[str] = []

    if profile.inspection_error:
        missing.append("inspection")
        blocked.append(f"input inspection failed: {profile.inspection_error}")

    concrete_file_types = file_types - {"*"}
    if concrete_file_types:
        if not profile.file_type:
            if require_verified_file_type:
                missing.append("file_type")
                preparation.append("input file type has not been verified")
        elif profile.file_type not in concrete_file_types:
            missing.append("file_type")
            blocked.append(
                f"file type '{profile.file_type}' is incompatible; "
                f"expected one of {sorted(concrete_file_types)}"
            )
    concrete_modalities = modalities - {"*"}
    if concrete_modalities:
        if not profile.modality:
            if require_verified_modality:
                missing.append("modality")
                preparation.append("input modality has not been verified")
        elif profile.modality not in concrete_modalities:
            missing.append("modality")
            blocked.append(
                f"modality '{profile.modality}' is incompatible; "
                f"expected one of {sorted(concrete_modalities)}"
            )

    if data_shape.get("requires_preprocessed") and profile.preprocessed is not True:
        if profile.preprocessed is False or require_observed_data_shape:
            missing.append("preprocessed")
            preparation.append("input has not been verified as preprocessed")

    for key in ("obs", "var", "layers", "obsm", "uns"):
        required = _required_names(data_shape, key)
        if not required:
            continue
        observed = getattr(profile, key)
        absent = (
            set()
            if observed is None and not require_observed_data_shape
            else required
            if observed is None
            else required - observed
        )
        for name in sorted(absent):
            missing.append(f"{key}.{name}")
            preparation.append(f"required {key} key '{name}' is not available")

    for key in ("env", "config"):
        required = _normalise_names(preconditions.get(key, [])) or set()
        if not required:
            continue
        observed = getattr(profile, key)
        absent = required if observed is None else required - observed
        for name in sorted(absent):
            missing.append(f"{key}.{name}")
            blocked.append(f"required {key} value '{name}' is not available")

    tabular = content.get("tabular") or {}
    if tabular and profile.file_type in {"csv", "tsv"}:
        if profile.table_inspection_error:
            missing.append("content.tabular.inspection")
            blocked.append(
                f"tabular input inspection failed: {profile.table_inspection_error}"
            )
        elif profile.table_columns is None or profile.table_column_count is None:
            missing.append("content.tabular.inspection")
            preparation.append("tabular header has not been inspected")
        else:
            minimum = tabular.get("min_columns")
            if minimum is not None and profile.table_column_count < int(minimum):
                missing.append("content.tabular.min_columns")
                blocked.append(
                    "tabular input has "
                    f"{profile.table_column_count} columns; at least {int(minimum)} required"
                )
            required_columns = _normalise_names(
                tabular.get("required_columns", [])
            ) or set()
            for column in sorted(required_columns - profile.table_columns):
                missing.append(f"content.tabular.column.{column}")
                blocked.append(f"required tabular column '{column}' is not available")

    vcf = content.get("vcf") or {}
    if vcf and profile.file_type == "vcf":
        if profile.vcf_inspection_error:
            missing.append("content.vcf.inspection")
            blocked.append(f"VCF input inspection failed: {profile.vcf_inspection_error}")
        elif profile.vcf_columns is None:
            missing.append("content.vcf.inspection")
            preparation.append("VCF header has not been inspected")
        else:
            if vcf.get("require_fileformat_header") and not profile.vcf_fileformat:
                missing.append("content.vcf.fileformat")
                blocked.append("VCF ##fileformat header is not available")
            required_columns = _normalise_names(
                vcf.get("required_columns", [])
            ) or set()
            for column in sorted(required_columns - profile.vcf_columns):
                missing.append(f"content.vcf.column.{column}")
                blocked.append(f"required VCF column '{column}' is not available")
            for field_name, observed in (
                ("info", profile.vcf_info_ids),
                ("format", profile.vcf_format_ids),
            ):
                required_ids = _normalise_names(
                    vcf.get(f"required_{field_name}_ids", [])
                ) or set()
                if observed is None:
                    for name in sorted(required_ids):
                        missing.append(f"content.vcf.{field_name}.{name}")
                        preparation.append(
                            f"VCF {field_name.upper()} id '{name}' has not been inspected"
                        )
                else:
                    for name in sorted(required_ids - observed):
                        missing.append(f"content.vcf.{field_name}.{name}")
                        blocked.append(
                            f"required VCF {field_name.upper()} id '{name}' is not available"
                        )
            minimum_samples = vcf.get("min_samples")
            if minimum_samples is not None:
                if profile.vcf_sample_count is None:
                    missing.append("content.vcf.min_samples")
                    preparation.append("VCF sample count has not been inspected")
                elif profile.vcf_sample_count < int(minimum_samples):
                    missing.append("content.vcf.min_samples")
                    blocked.append(
                        f"VCF has {profile.vcf_sample_count} samples; "
                        f"at least {int(minimum_samples)} required"
                    )

    fastq = content.get("fastq") or {}
    if fastq and (
        profile.file_type in _FASTQ_FILE_TYPES
        or profile.fastq_record_valid is not None
    ):
        if fastq.get("require_valid_record"):
            if profile.fastq_record_valid is None:
                missing.append("content.fastq.record")
                preparation.append("FASTQ record structure has not been inspected")
            elif not profile.fastq_record_valid:
                missing.append("content.fastq.record")
                blocked.append(
                    "FASTQ record inspection failed: "
                    + (profile.fastq_inspection_error or "invalid record structure")
                )
        required_pairing = str(fastq.get("pairing") or "any").lower()
        if required_pairing != "any":
            if not profile.fastq_pairing:
                missing.append("content.fastq.pairing")
                preparation.append("FASTQ mate layout has not been inspected")
            elif profile.fastq_pairing != required_pairing:
                missing.append("content.fastq.pairing")
                blocked.append(
                    f"FASTQ layout is '{profile.fastq_pairing}'; "
                    f"'{required_pairing}' required"
                )

    directory = content.get("directory") or {}
    if directory and profile.path_kind == "directory":
        required_signatures = _normalise_names(
            directory.get("any_of_signatures", [])
        ) or set()
        if profile.directory_inspection_error:
            missing.append("content.directory.inspection")
            blocked.append(
                "directory inspection failed: " + profile.directory_inspection_error
            )
        elif profile.directory_signatures is None:
            missing.append("content.directory.signature")
            preparation.append("directory layout has not been inspected")
        elif not required_signatures.intersection(profile.directory_signatures):
            missing.append("content.directory.signature")
            reason = (
                "directory probe was truncated before a required layout was found"
                if profile.directory_probe_truncated
                else "directory does not match any required semantic layout"
            )
            target = preparation if profile.directory_probe_truncated else blocked
            target.append(f"{reason}; expected one of {sorted(required_signatures)}")

    canonical_skill = str(info.get("alias") or skill_name)
    if blocked:
        return PreconditionAssessment(
            skill=canonical_skill,
            status=PreconditionStatus.BLOCKED,
            evaluated=True,
            execution_ready=False,
            missing=missing,
            reasons=blocked + preparation,
        )
    if preparation:
        recommendation_map = _PREPARATION_SKILLS.get(str(info.get("domain") or ""), {})
        recommendations = list(
            dict.fromkeys(
                recommendation_map[item]
                for item in missing
                if item in recommendation_map
                and recommendation_map[item] != canonical_skill
            )
        )
        return PreconditionAssessment(
            skill=canonical_skill,
            status=PreconditionStatus.NEEDS_PREPARATION,
            evaluated=True,
            execution_ready=False,
            missing=missing,
            reasons=preparation,
            recommended_preparation=recommendations,
        )
    return PreconditionAssessment(
        skill=canonical_skill,
        status=PreconditionStatus.ELIGIBLE,
        evaluated=True,
        execution_ready=True,
    )


__all__ = [
    "InputProfile",
    "PreconditionAssessment",
    "PreconditionStatus",
    "evaluate_skill_preconditions",
    "format_precondition_failure",
    "preflight_skill_execution",
    "probe_input_profile",
]
