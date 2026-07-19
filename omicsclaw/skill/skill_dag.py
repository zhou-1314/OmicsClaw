"""Derive the auditable skill compatibility graph and candidate-plan DAGs.

An edge ``A -> B`` means an artifact declared by A can satisfy a machine
precondition declared by B, so A is a candidate upstream skill.  The graph is
deliberately conservative: it uses canonical registry entries, same-domain
contracts, exact AnnData keys, and a lower-confidence ``processed.h5ad`` rule.
It never consumes ``summary.skip_when`` because routing negatives are not data
dependencies. The repository-wide graph is diagnostic evidence and may contain
cycles; only a selected, cycle-free induced plan is exposed as an executable
topological chain.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from heapq import heapify, heappop, heappush
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml

from .strict_yaml import load_unique_yaml


ANNDATA_COLLECTIONS = ("obs", "obsm", "var", "layers", "uns")
EDGE_KINDS = frozenset({"required", "optional", "preferred", "alternative"})
REVIEW_DECISIONS = frozenset({"accepted", "rejected"})
RESOURCE_REQUEST_FIELDS = frozenset(
    {
        "cpu_cores",
        "memory_mib",
        "gpu_devices",
        "threads",
        "temporary_disk_mib",
    }
)
EdgeReviewKey = tuple[str, str, str, str]
_REVIEW_TOP_LEVEL_FIELDS = frozenset({"schema_version", "reviews"})
_REVIEW_ENTRY_FIELDS = frozenset(
    {
        "source",
        "target",
        "matched_output_key",
        "matched_precondition_key",
        "decision",
        "edge_kind",
        "condition_scope",
        "reviewed_by",
        "reviewed_at",
        "rationale",
    }
)
_REVIEW_CONDITION_SCOPE_FIELDS = frozenset({"source_methods"})


class SkillDagCycleError(ValueError):
    """Raised when a topological query encounters a compatibility cycle."""


def supports_unified_method_binding(skill_info: Mapping[str, Any]) -> bool:
    """Return whether a frozen Skill contract accepts ``--method``.

    ``param_hints`` describe profiles and documentation; they are not, by
    themselves, proof that the runtime exposes the shared method selector.
    Candidate plans may bind a profile only when the same frozen Registry
    entry explicitly allows ``--method``.
    """
    flags = skill_info.get("allowed_extra_flags") or ()
    if isinstance(flags, str):
        return flags == "--method"
    try:
        return "--method" in flags
    except TypeError:
        return False


def method_binding_is_runtime_accepted(
    skill_info: Mapping[str, Any],
    method: str,
) -> bool:
    """Prove a formal v2 binding is accepted by the entry's argparse contract."""
    if str(skill_info.get("source") or "") != "v2":
        # Legacy/custom registries predate the executable binding contract.
        return True
    if str(skill_info.get("runtime_language") or "python") != "python":
        return False
    script_value = skill_info.get("script")
    if not script_value:
        return False
    script_path = Path(script_value)
    if script_path.is_symlink():
        return False
    try:
        from .execution.flag_introspection import argparse_path_flag_accepts_value

        accepted = argparse_path_flag_accepts_value(
            script_path,
            "--method",
            method,
        )
    except OSError:
        return False
    return accepted is True


def _read_optional_review_bytes(path: Path) -> bytes | None:
    """Read one optional review file while preserving missing vs empty."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _parse_skill_dag_reviews(
    review_bytes: bytes | None,
    *,
    path: Path,
) -> dict[EdgeReviewKey, dict[str, Any]]:
    """Parse the exact bytes that will be fingerprinted as review authority."""
    if review_bytes is None:
        return {}
    try:
        raw = load_unique_yaml(review_bytes.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid skill DAG review YAML: {path}: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 2:
        raise ValueError(f"invalid skill DAG review schema: {path}")
    unknown_top_level = set(raw) - _REVIEW_TOP_LEVEL_FIELDS
    if unknown_top_level:
        fields = ", ".join(
            sorted(repr(field) for field in unknown_top_level)
        )
        raise ValueError(
            f"skill DAG reviews have unsupported top-level fields: {fields}"
        )
    entries = raw.get("reviews") or []
    if not isinstance(entries, list):
        raise ValueError(f"skill DAG reviews must be a list: {path}")

    reviews: dict[EdgeReviewKey, dict[str, Any]] = {}
    required = (
        "source",
        "target",
        "matched_output_key",
        "matched_precondition_key",
    )
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"skill DAG review #{index} must be a mapping")
        unknown_entry_fields = set(entry) - _REVIEW_ENTRY_FIELDS
        if unknown_entry_fields:
            fields = ", ".join(
                sorted(repr(field) for field in unknown_entry_fields)
            )
            raise ValueError(
                f"skill DAG review #{index} has unsupported fields: {fields}"
            )
        key = tuple(str(entry.get(field) or "").strip() for field in required)
        if not all(key):
            raise ValueError(f"skill DAG review #{index} has an incomplete edge identity")
        edge_kind = str(entry.get("edge_kind") or "").strip()
        if edge_kind not in EDGE_KINDS:
            raise ValueError(f"skill DAG review #{index} has invalid edge_kind {edge_kind!r}")
        if key in reviews:
            raise ValueError(f"duplicate skill DAG review identity: {key}")
        decision = str(entry.get("decision") or "").strip()
        if decision not in REVIEW_DECISIONS:
            raise ValueError(
                f"skill DAG review #{index} has invalid decision {decision!r}"
            )
        reviewed_by = str(entry.get("reviewed_by") or "").strip()
        reviewed_at = str(entry.get("reviewed_at") or "").strip()
        rationale = str(entry.get("rationale") or "").strip()
        if not reviewed_by or not reviewed_at or not rationale:
            raise ValueError(
                f"skill DAG review #{index} requires reviewed_by, reviewed_at, and rationale"
            )
        condition_scope = entry.get("condition_scope")
        if condition_scope is not None and not isinstance(condition_scope, Mapping):
            raise ValueError(f"skill DAG review #{index} condition_scope must be a mapping or null")
        if isinstance(condition_scope, Mapping):
            unknown_scope_fields = (
                set(condition_scope) - _REVIEW_CONDITION_SCOPE_FIELDS
            )
            if unknown_scope_fields:
                fields = ", ".join(
                    sorted(repr(field) for field in unknown_scope_fields)
                )
                raise ValueError(
                    f"skill DAG review #{index} condition_scope has unsupported "
                    f"fields: {fields}"
                )
        reviews[key] = {
            "decision": decision,
            "edge_kind": edge_kind,
            "condition_scope": condition_scope,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "rationale": rationale,
        }
    return reviews


def load_skill_dag_reviews_with_revision(
    path: Path,
) -> tuple[dict[EdgeReviewKey, dict[str, Any]], str]:
    """Load one read-stable review overlay and hash its exact authority bytes."""
    review_bytes = _read_optional_review_bytes(path)
    reviews = _parse_skill_dag_reviews(review_bytes, path=path)
    if _read_optional_review_bytes(path) != review_bytes:
        raise ValueError("skill DAG review authority changed while being read")
    revision = "sha256:" + hashlib.sha256(review_bytes or b"").hexdigest()
    return reviews, revision


def load_skill_dag_reviews(path: Path) -> dict[EdgeReviewKey, dict[str, Any]]:
    """Load one read-stable governed review overlay."""
    reviews, _revision = load_skill_dag_reviews_with_revision(path)
    return reviews


def _strings(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _input_contract(info: Mapping[str, Any]) -> Mapping[str, Any]:
    value = info.get("input_contract") or {}
    return value if isinstance(value, Mapping) else {}


def _output_contract(info: Mapping[str, Any]) -> Mapping[str, Any]:
    value = info.get("output_contract") or {}
    return value if isinstance(value, Mapping) else {}


def _data_shape(info: Mapping[str, Any]) -> Mapping[str, Any]:
    preconditions = _input_contract(info).get("preconditions") or {}
    if not isinstance(preconditions, Mapping):
        return {}
    value = preconditions.get("data_shape") or {}
    return value if isinstance(value, Mapping) else {}


def _anndata_output(info: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _output_contract(info).get("anndata") or {}
    return value if isinstance(value, Mapping) else {}


def _output_files(info: Mapping[str, Any]) -> tuple[str, ...]:
    files = _output_contract(info).get("files") or []
    if not isinstance(files, (list, tuple)):
        return ()
    return tuple(str(value).strip() for value in files if str(value).strip())


def _input_artifacts(info: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    artifacts = _input_contract(info).get("artifacts") or []
    if not isinstance(artifacts, (list, tuple)):
        return ()
    return tuple(item for item in artifacts if isinstance(item, Mapping))


def _output_artifacts(info: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    artifacts = _output_contract(info).get("artifacts") or []
    if not isinstance(artifacts, (list, tuple)):
        return ()
    return tuple(item for item in artifacts if isinstance(item, Mapping))


def _method_scopes(info: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    scopes = _output_contract(info).get("method_scopes") or []
    if not isinstance(scopes, (list, tuple)):
        return ()
    return tuple(item for item in scopes if isinstance(item, Mapping))


def _scoped_output_artifacts(
    info: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], dict[str, Any] | None], ...]:
    result = [(artifact, None) for artifact in _output_artifacts(info)]
    for scope in _method_scopes(info):
        methods = sorted(_strings(scope.get("methods")))
        artifacts = scope.get("artifacts") or []
        if not methods or not isinstance(artifacts, (list, tuple)):
            continue
        condition_scope = {"source_methods": methods}
        result.extend(
            (artifact, condition_scope)
            for artifact in artifacts
            if isinstance(artifact, Mapping)
        )
    return tuple(result)


def _anndata_output_guarantees(
    info: Mapping[str, Any],
    collection: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return declared AnnData fields and the source methods that guarantee them.

    An empty method tuple is an unconditional guarantee.  If several disjoint
    method scopes guarantee the same field, they compile into one edge whose
    condition accepts the union of those methods.
    """

    guarantees: dict[str, set[str] | None] = {
        key: None for key in _strings(_anndata_output(info).get(collection))
    }
    for scope in _method_scopes(info):
        methods = _strings(scope.get("methods"))
        scoped_anndata = scope.get("anndata") or {}
        if not methods or not isinstance(scoped_anndata, Mapping):
            continue
        for key in _strings(scoped_anndata.get(collection)):
            if key in guarantees and guarantees[key] is None:
                continue
            guarantees.setdefault(key, set())
            assert guarantees[key] is not None
            guarantees[key].update(methods)
    return tuple(
        (key, tuple(sorted(methods or ())))
        for key, methods in sorted(guarantees.items())
    )


def _has_h5ad_artifact(info: Mapping[str, Any], *, processed_only: bool = False) -> bool:
    if not bool(_anndata_output(info).get("saves_h5ad")):
        return False
    names = {PurePosixPath(path.replace("\\", "/")).name.lower() for path in _output_files(info)}
    if processed_only:
        return "processed.h5ad" in names
    return any(name.endswith(".h5ad") for name in names)


def _h5ad_output_path(
    info: Mapping[str, Any],
    *,
    processed_only: bool = False,
) -> str:
    """Return the deterministic declared path used for artifact propagation."""
    candidates = [
        path
        for path in _output_files(info)
        if PurePosixPath(path.replace("\\", "/")).name.lower().endswith(".h5ad")
    ]
    if processed_only:
        candidates = [
            path
            for path in candidates
            if PurePosixPath(path.replace("\\", "/")).name.lower()
            == "processed.h5ad"
        ]
    else:
        canonical = [
            path
            for path in candidates
            if PurePosixPath(path.replace("\\", "/")).name.lower()
            == "processed.h5ad"
        ]
        if canonical:
            candidates = canonical
    return sorted(candidates)[0] if candidates else ""


def _accepts_h5ad(info: Mapping[str, Any]) -> bool:
    contract = _input_contract(info)
    path_kinds = _strings(contract.get("path_kinds") or ["file"])
    if "file" not in path_kinds:
        return False
    file_types = {value.lower().lstrip(".") for value in _strings(contract.get("file_types"))}
    return not file_types or "h5ad" in file_types


def _compatible(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    require_known_modalities: bool = False,
) -> bool:
    if str(source.get("domain") or "") != str(target.get("domain") or ""):
        return False
    if not _accepts_h5ad(target):
        return False
    # A downstream transformation must not become the inferred prerequisite of
    # a root-stage consumer merely because it preserves a common AnnData key.
    if bool(_data_shape(source).get("requires_preprocessed")) and not bool(
        _data_shape(target).get("requires_preprocessed")
    ):
        return False
    source_modalities = _strings(_input_contract(source).get("modalities"))
    target_modalities = _strings(_input_contract(target).get("modalities"))
    if require_known_modalities and (not source_modalities or not target_modalities):
        return False
    return not source_modalities or not target_modalities or bool(source_modalities & target_modalities)


def _edge(
    source: str,
    target: str,
    *,
    edge_kind: str,
    matched_output_key: str,
    matched_precondition_key: str,
    matched_output_path: str = "",
    condition_scope: Mapping[str, Any] | None = None,
    confidence: float,
) -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "edge_kind": edge_kind,
        "matched_output_key": matched_output_key,
        "matched_precondition_key": matched_precondition_key,
        "matched_output_path": matched_output_path,
        "condition_scope": dict(condition_scope) if condition_scope else None,
        "confidence": confidence,
        "reviewed": False,
    }


def _adjacency(edges: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        result[str(edge["source"])].add(str(edge["target"]))
    return result


def build_skill_dag(
    registry,
    *,
    reviews: Mapping[EdgeReviewKey, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile canonical registry contracts into a deterministic compatibility graph.

    Exact AnnData-key matches are added first. Generic preprocessing edges need
    the explicit ``outputs.anndata.processing_state=preprocessed`` postcondition;
    filenames are never interpreted as processing evidence. Generated edges are
    unreviewed alternatives until a governance overlay assigns stronger meaning.
    """

    primary = sorted(registry.iter_primary_skills(), key=lambda item: item[0])
    nodes = [
        {
            "skill": name,
            "domain": str(info.get("domain") or ""),
            "type": str(info.get("type") or "leaf"),
            "validation_level": str(info.get("validation_level") or "smoke-only"),
            "lifecycle_status": str(info.get("lifecycle_status") or "mvp"),
            "compute_resources": dict(info.get("compute_resources") or {}),
            "artifact_inputs": sorted(
                {
                    str(artifact.get("kind") or "")
                    for artifact in _input_artifacts(info)
                    if str(artifact.get("kind") or "")
                }
            ),
            "artifact_outputs": sorted(
                {
                    str(artifact.get("kind") or "")
                    for artifact, _scope in _scoped_output_artifacts(info)
                    if str(artifact.get("kind") or "")
                }
            ),
        }
        for name, info in primary
    ]

    artifact_edges: list[dict[str, Any]] = []
    for source, source_info in primary:
        for produced, condition_scope in _scoped_output_artifacts(source_info):
            kind = str(produced.get("kind") or "").strip()
            output_format = str(produced.get("format") or "").strip()
            output_path = str(produced.get("path") or "").strip()
            if not kind or not output_format or not output_path:
                continue
            for target, target_info in primary:
                if source == target:
                    continue
                for accepted in _input_artifacts(target_info):
                    if str(accepted.get("kind") or "").strip() != kind:
                        continue
                    formats = _strings(accepted.get("formats"))
                    if formats and output_format not in formats:
                        continue
                    artifact_edges.append(
                        _edge(
                            source,
                            target,
                            edge_kind="alternative",
                            matched_output_key=f"artifacts.{kind}",
                            matched_precondition_key=f"artifacts.{kind}",
                            matched_output_path=output_path,
                            condition_scope=condition_scope,
                            confidence=0.9,
                        )
                    )

    exact_groups: dict[
        tuple[str, str],
        list[tuple[str, str, str, tuple[str, ...]]],
    ] = defaultdict(list)
    for target, target_info in primary:
        shape = _data_shape(target_info)
        for collection in ANNDATA_COLLECTIONS:
            for key in sorted(_strings(shape.get(collection))):
                precondition_key = f"data_shape.{collection}.{key}"
                for source, source_info in primary:
                    if source == target or not _compatible(source_info, target_info):
                        continue
                    if not _has_h5ad_artifact(source_info):
                        continue
                    guarantees = dict(
                        _anndata_output_guarantees(source_info, collection)
                    )
                    if key not in guarantees:
                        continue
                    exact_groups[(target, precondition_key)].append(
                        (
                            source,
                            target,
                            _h5ad_output_path(source_info),
                            guarantees[key],
                        )
                    )

    edges: list[dict[str, Any]] = artifact_edges
    for (target, precondition_key), candidates in sorted(exact_groups.items()):
        collection, key = precondition_key.split(".")[1:]
        for source, _target, output_path, source_methods in sorted(set(candidates)):
            edges.append(
                _edge(
                    source,
                    target,
                    edge_kind="alternative",
                    matched_output_key=f"anndata.{collection}.{key}",
                    matched_precondition_key=precondition_key,
                    matched_output_path=output_path,
                    condition_scope=(
                        {"source_methods": list(source_methods)}
                        if source_methods
                        else None
                    ),
                    confidence=0.95,
                )
            )

    generic_groups: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for target, target_info in primary:
        if not bool(_data_shape(target_info).get("requires_preprocessed")):
            continue
        precondition_key = "data_shape.requires_preprocessed"
        for source, source_info in primary:
            if source == target or not _compatible(
                source_info,
                target_info,
                require_known_modalities=True,
            ):
                continue
            if bool(_data_shape(source_info).get("requires_preprocessed")):
                continue
            if not _has_h5ad_artifact(source_info, processed_only=True):
                continue
            if _anndata_output(source_info).get("processing_state") != "preprocessed":
                continue
            generic_groups[(target, precondition_key)].append(
                (
                    source,
                    target,
                    _h5ad_output_path(source_info, processed_only=True),
                )
            )

    for (target, precondition_key), candidates in sorted(generic_groups.items()):
        for source, _target, output_path in sorted(set(candidates)):
            edges.append(
                _edge(
                    source,
                    target,
                    edge_kind="alternative",
                    matched_output_key="files.processed.h5ad",
                    matched_precondition_key=precondition_key,
                    matched_output_path=output_path,
                    confidence=0.55,
                )
            )

    edges.sort(
        key=lambda item: (
            item["source"],
            item["target"],
            item["matched_precondition_key"],
            item["matched_output_key"],
        )
    )
    review_map = dict(reviews or {})
    matched_reviews: set[EdgeReviewKey] = set()
    rejected_edges: list[dict[str, Any]] = []
    accepted_edges: list[dict[str, Any]] = []
    for edge in edges:
        identity = (
            edge["source"],
            edge["target"],
            edge["matched_output_key"],
            edge["matched_precondition_key"],
        )
        review = review_map.get(identity)
        if review is None:
            accepted_edges.append(edge)
            continue
        decision = str(review.get("decision") or "")
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"invalid review decision {decision!r} for {identity}")
        edge_kind = str(review.get("edge_kind") or "")
        if edge_kind not in EDGE_KINDS:
            raise ValueError(f"invalid reviewed edge_kind {edge_kind!r} for {identity}")
        review_scope = review.get("condition_scope")
        if review_scope != edge["condition_scope"]:
            raise ValueError(
                "skill DAG review condition_scope does not match the derived "
                f"edge for {identity}: {review_scope!r} != {edge['condition_scope']!r}"
            )
        review_evidence = {
            "decision": decision,
            "reviewed_by": str(review.get("reviewed_by") or ""),
            "reviewed_at": str(review.get("reviewed_at") or ""),
            "rationale": str(review.get("rationale") or ""),
        }
        matched_reviews.add(identity)
        if decision == "rejected":
            rejected_edges.append(
                {
                    "source": identity[0],
                    "target": identity[1],
                    "matched_output_key": identity[2],
                    "matched_precondition_key": identity[3],
                    "review": review_evidence,
                }
            )
            continue
        edge["edge_kind"] = edge_kind
        edge["reviewed"] = True
        edge["review"] = review_evidence
        accepted_edges.append(edge)
    stale_reviews = sorted(set(review_map) - matched_reviews)
    if stale_reviews:
        raise ValueError(f"skill DAG reviews reference missing derived edges: {stale_reviews}")

    edges = accepted_edges

    kinds = Counter(edge["edge_kind"] for edge in edges)
    graph = {
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges,
    }
    cycle = detect_cycle(graph)
    summary = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "method_scoped_skill_count": sum(
            bool(_method_scopes(info)) for _name, info in primary
        ),
        "conditional_edge_count": sum(
            edge.get("condition_scope") is not None for edge in edges
        ),
        "exact_edge_count": sum(edge["confidence"] == 0.95 for edge in edges),
        "generic_edge_count": sum(edge["confidence"] == 0.55 for edge in edges),
        "artifact_edge_count": sum(edge["confidence"] == 0.9 for edge in edges),
        "reviewed_edge_count": sum(bool(edge["reviewed"]) for edge in edges),
        "rejected_edge_count": len(rejected_edges),
        "has_cycle": bool(cycle),
        "edge_kind_counts": {kind: kinds.get(kind, 0) for kind in sorted(EDGE_KINDS)},
    }
    graph["diagnostics"] = {
        "cycles": [cycle] if cycle else [],
        "rejected_edges": rejected_edges,
    }
    graph["summary"] = summary
    return graph


def _selected_edges(
    graph: Mapping[str, Any],
    nodes: set[str],
    edge_kinds: set[str] | None = None,
) -> list[Mapping[str, Any]]:
    return sorted(
        [
        edge
        for edge in graph.get("edges", [])
        if str(edge.get("source")) in nodes
        and str(edge.get("target")) in nodes
        and (edge_kinds is None or str(edge.get("edge_kind")) in edge_kinds)
        ],
        key=lambda edge: (
            str(edge.get("source")),
            str(edge.get("target")),
            str(edge.get("matched_precondition_key")),
            str(edge.get("matched_output_key")),
        ),
    )


def _node_names(graph: Mapping[str, Any]) -> set[str]:
    names = {str(node["skill"]) for node in graph.get("nodes", [])}
    for edge in graph.get("edges", []):
        names.add(str(edge["source"]))
        names.add(str(edge["target"]))
    return names


def detect_cycle(
    graph: Mapping[str, Any],
    *,
    skills: Iterable[str] | None = None,
    edge_kinds: Iterable[str] | None = None,
) -> list[str]:
    """Return the first deterministic cycle as ``[a, ..., a]``, else ``[]``."""

    nodes = set(skills) if skills is not None else _node_names(graph)
    kinds = set(edge_kinds) if edge_kinds is not None else None
    adjacency = _adjacency(_selected_edges(graph, nodes, kinds))
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        stack.append(node)
        for target in sorted(adjacency.get(node, set())):
            if state.get(target, 0) == 0:
                cycle = visit(target)
                if cycle:
                    return cycle
            elif state[target] == 1:
                return stack[stack.index(target) :] + [target]
        stack.pop()
        state[node] = 2
        return []

    for node in sorted(nodes):
        if state.get(node, 0) == 0:
            cycle = visit(node)
            if cycle:
                return cycle
    return []


def topological_sort(
    graph: Mapping[str, Any],
    *,
    skills: Iterable[str] | None = None,
    edge_kinds: Iterable[str] | None = None,
) -> list[str]:
    """Return deterministic producer-before-consumer order for an induced graph."""

    nodes = set(skills) if skills is not None else _node_names(graph)
    unknown = nodes - _node_names(graph)
    if unknown:
        raise KeyError(f"unknown skills: {sorted(unknown)}")
    kinds = set(edge_kinds) if edge_kinds is not None else None
    edges = _selected_edges(graph, nodes, kinds)
    adjacency = _adjacency(edges)
    indegree = {node: 0 for node in nodes}
    for source, targets in adjacency.items():
        for target in targets:
            indegree[target] += 1

    ready = [node for node, degree in indegree.items() if degree == 0]
    heapify(ready)
    order: list[str] = []
    while ready:
        node = heappop(ready)
        order.append(node)
        for target in sorted(adjacency.get(node, set())):
            indegree[target] -= 1
            if indegree[target] == 0:
                heappush(ready, target)
    if len(order) != len(nodes):
        cycle = detect_cycle(graph, skills=nodes, edge_kinds=kinds)
        raise SkillDagCycleError("compatibility cycle: " + " -> ".join(cycle))
    return order


def upstream_closure(graph: Mapping[str, Any], skill: str) -> list[str]:
    """Return all transitive candidate producers for ``skill``."""

    if skill not in _node_names(graph):
        raise KeyError(f"unknown skill: {skill}")
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        reverse[str(edge["target"])].add(str(edge["source"]))
    found: set[str] = set()
    pending = list(reverse.get(skill, set()))
    while pending:
        node = pending.pop()
        if node in found:
            continue
        found.add(node)
        pending.extend(reverse.get(node, set()) - found)
    found.discard(skill)
    return sorted(found)


def downstream_skills(graph: Mapping[str, Any], skill: str) -> list[str]:
    """Return all transitive candidate consumers of ``skill``."""

    if skill not in _node_names(graph):
        raise KeyError(f"unknown skill: {skill}")
    adjacency = _adjacency(graph.get("edges", []))
    found: set[str] = set()
    pending = list(adjacency.get(skill, set()))
    while pending:
        node = pending.pop()
        if node in found:
            continue
        found.add(node)
        pending.extend(adjacency.get(node, set()) - found)
    found.discard(skill)
    return sorted(found)


def _execution_phases(graph: Mapping[str, Any], skills: set[str]) -> list[list[str]]:
    edges = _selected_edges(graph, skills)
    adjacency = _adjacency(edges)
    indegree = {skill: 0 for skill in skills}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    phases: list[list[str]] = []
    ready = sorted(skill for skill, degree in indegree.items() if degree == 0)
    consumed = 0
    while ready:
        phase = ready
        phases.append(phase)
        consumed += len(phase)
        next_ready: list[str] = []
        for source in phase:
            for target in sorted(adjacency.get(source, set())):
                indegree[target] -= 1
                if indegree[target] == 0:
                    next_ready.append(target)
        ready = sorted(next_ready)
    if consumed != len(skills):
        cycle = detect_cycle(graph, skills=skills)
        raise SkillDagCycleError("compatibility cycle: " + " -> ".join(cycle))
    return phases


def _edge_source_methods(edge: Mapping[str, Any]) -> tuple[str, ...]:
    scope = edge.get("condition_scope")
    if not isinstance(scope, Mapping):
        return ()
    return tuple(sorted(_strings(scope.get("source_methods"))))


def _edge_is_conditional(edge: Mapping[str, Any]) -> bool:
    return edge.get("condition_scope") is not None


def build_candidate_chain(
    graph: Mapping[str, Any],
    skills: Iterable[str],
    *,
    method_bindings: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a cycle-free induced plan whose conditional edges are bound.

    Method-scoped compatibility is fail-closed: a conditional edge participates
    in ordering only when the producer has an explicit matching method binding.
    """

    requested = list(dict.fromkeys(str(skill) for skill in skills))
    selected = set(requested)
    bindings = {
        str(skill): str(method)
        for skill, method in (method_bindings or {}).items()
        if str(skill) in selected and str(method).strip()
    }
    selected_edges = _selected_edges(graph, selected)
    edges = [
        dict(edge)
        for edge in selected_edges
        if not _edge_is_conditional(edge)
        or (
            bool(_edge_source_methods(edge))
            and bindings.get(str(edge.get("source"))) in _edge_source_methods(edge)
        )
    ]
    bound_graph = {"nodes": graph.get("nodes", []), "edges": edges}
    order = topological_sort(bound_graph, skills=selected)
    adjacency = _adjacency(edges)

    def connected(left: str, right: str) -> bool:
        pending = [left]
        visited: set[str] = set()
        while pending:
            node = pending.pop()
            if node == right:
                return True
            if node in visited:
                continue
            visited.add(node)
            pending.extend(adjacency.get(node, set()) - visited)
        return False

    def unresolved_pair(source: str, target: str) -> dict[str, Any] | None:
        if connected(source, target) or connected(target, source):
            return None
        conditional = [
            edge
            for edge in selected_edges
            if {
                str(edge.get("source")),
                str(edge.get("target")),
            }
            == {source, target}
            and _edge_is_conditional(edge)
        ]
        if conditional:
            producer = str(conditional[0].get("source"))
            allowed = sorted(
                {
                    method
                    for edge in conditional
                    if str(edge.get("source")) == producer
                    for method in _edge_source_methods(edge)
                }
            )
            selected_method = bindings.get(producer)
            result: dict[str, Any] = {
                "source": source,
                "target": target,
                "reason": (
                    "method_scope_mismatch"
                    if selected_method is not None
                    else "method_binding_required"
                ),
                "skill": producer,
            }
            if selected_method is not None:
                result["selected_method"] = selected_method
            result["allowed_methods"] = allowed
            return result
        return {
            "source": source,
            "target": target,
            "reason": "no_compatibility_edge",
        }

    unresolved = [
        result
        for source, target in zip(requested, requested[1:])
        if (result := unresolved_pair(source, target)) is not None
    ]
    node_by_skill = {
        str(node.get("skill")): node
        for node in graph.get("nodes", [])
        if isinstance(node, Mapping) and str(node.get("skill") or "")
    }
    resource_requests = {
        skill: dict(node_by_skill[skill].get("compute_resources") or {})
        for skill in sorted(selected)
        if skill in node_by_skill
        and isinstance(node_by_skill[skill].get("compute_resources"), Mapping)
        and node_by_skill[skill].get("compute_resources")
    }
    missing_resource_requests = sorted(selected - set(resource_requests))
    plan = {
        "requested_skills": requested,
        "skills": order,
        "phases": _execution_phases(bound_graph, selected),
        "edges": edges,
        "validated_order": not unresolved,
        "unresolved_pairs": unresolved,
        "resource_requests": resource_requests,
        "resource_ready": not missing_resource_requests,
        "missing_resource_requests": missing_resource_requests,
    }
    if bindings:
        plan["method_bindings"] = dict(sorted(bindings.items()))
    return plan


def candidate_graph_authority_payload(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize the selected-plan fields governed by graph authority."""
    raw_skills = plan.get("skills")
    raw_edges = plan.get("edges")
    raw_unresolved = plan.get("unresolved_pairs")
    raw_bindings = plan.get("method_bindings")
    raw_resource_requests = plan.get("resource_requests")
    raw_resource_ready = plan.get("resource_ready")
    raw_missing_resources = plan.get("missing_resource_requests")
    if raw_bindings is None:
        raw_bindings = {}
    if not isinstance(raw_skills, list) or not all(
        isinstance(skill, str) and skill.strip() for skill in raw_skills
    ):
        raise ValueError("candidate graph authority skills must be a string list")
    if not isinstance(raw_edges, list) or not all(
        isinstance(edge, Mapping) for edge in raw_edges
    ):
        raise ValueError("candidate graph authority edges must be an object list")
    if not isinstance(plan.get("validated_order"), bool):
        raise ValueError("candidate graph authority validated_order must be boolean")
    if not isinstance(raw_unresolved, list) or not all(
        isinstance(pair, Mapping) for pair in raw_unresolved
    ):
        raise ValueError(
            "candidate graph authority unresolved_pairs must be an object list"
        )
    if not isinstance(raw_bindings, Mapping):
        raise ValueError("candidate graph authority method_bindings must be an object")
    if not isinstance(raw_resource_requests, Mapping):
        raise ValueError("candidate graph authority resource_requests must be an object")
    if not isinstance(raw_resource_ready, bool):
        raise ValueError("candidate graph authority resource_ready must be boolean")
    if not isinstance(raw_missing_resources, list) or not all(
        isinstance(skill, str) and skill.strip() for skill in raw_missing_resources
    ):
        raise ValueError(
            "candidate graph authority missing_resource_requests must be a string list"
        )
    skills = [skill.strip() for skill in raw_skills]
    if len(set(skills)) != len(skills):
        raise ValueError("candidate graph authority skills must be unique")
    bindings: dict[str, str] = {}
    for raw_skill, raw_method in raw_bindings.items():
        skill = str(raw_skill or "").strip()
        method = str(raw_method or "").strip()
        if not skill or not method:
            raise ValueError(
                "candidate graph authority method bindings must be non-empty strings"
            )
        bindings[skill] = method
    resources: dict[str, dict[str, int]] = {}
    for raw_skill, raw_request in raw_resource_requests.items():
        skill = str(raw_skill or "").strip()
        if skill not in skills or skill in resources:
            raise ValueError(
                "candidate graph authority resource_requests must use selected skills"
            )
        if not isinstance(raw_request, Mapping) or set(raw_request) != RESOURCE_REQUEST_FIELDS:
            raise ValueError(
                f"candidate graph authority resource request has invalid schema: {skill!r}"
            )
        request: dict[str, int] = {}
        for field in sorted(RESOURCE_REQUEST_FIELDS):
            value = raw_request[field]
            minimum = 0 if field in {"gpu_devices", "temporary_disk_mib"} else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(
                    f"candidate graph authority resource value is invalid: {skill!r}:{field}"
                )
            request[field] = value
        if request["threads"] > request["cpu_cores"]:
            raise ValueError(
                f"candidate graph authority threads exceed cpu reservation: {skill!r}"
            )
        resources[skill] = request
    missing_resources = [skill.strip() for skill in raw_missing_resources]
    if len(set(missing_resources)) != len(missing_resources):
        raise ValueError(
            "candidate graph authority missing_resource_requests must be unique"
        )
    resource_skills = set(resources)
    missing_skills = set(missing_resources)
    selected_skills = set(skills)
    if (
        resource_skills & missing_skills
        or resource_skills | missing_skills != selected_skills
        or raw_resource_ready != (not missing_resources)
    ):
        raise ValueError(
            "candidate graph authority resources must exactly partition selected skills"
        )
    return {
        "skills": skills,
        "edges": [dict(edge) for edge in raw_edges],
        "validated_order": plan["validated_order"],
        "unresolved_pairs": [dict(pair) for pair in raw_unresolved],
        "method_bindings": dict(sorted(bindings.items())),
        "resource_requests": {
            skill: resources[skill] for skill in sorted(resources)
        },
        "resource_ready": raw_resource_ready,
        "missing_resource_requests": sorted(missing_resources),
    }


def candidate_plan_graph_hash(plan: Mapping[str, Any]) -> str:
    """Fingerprint the normalized selected graph authority in a plan."""
    payload = json.dumps(
        candidate_graph_authority_payload(plan),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _validate_registry_method_bindings(
    registry: Any,
    *,
    skills: list[str],
    method_bindings: Mapping[str, str] | None,
) -> None:
    """Require every selected method to be declared by the frozen Registry."""
    if not method_bindings:
        return
    selected = set(skills)
    entries = dict(registry.iter_primary_skills())
    for raw_skill, raw_method in method_bindings.items():
        skill = str(raw_skill or "").strip()
        method = str(raw_method or "").strip()
        if skill not in selected:
            raise ValueError(
                f"method binding references an unselected skill: {skill!r}"
            )
        info = entries.get(skill)
        hints = info.get("param_hints") if isinstance(info, Mapping) else None
        if not isinstance(hints, Mapping) or method not in hints:
            raise ValueError(
                f"method {method!r} is not declared in frozen Registry "
                f"param_hints for {skill!r}"
            )
        if not supports_unified_method_binding(info):
            raise ValueError(
                f"skill {skill!r} does not expose the unified --method flag"
            )
        if not method_binding_is_runtime_accepted(info, method):
            raise ValueError(
                f"method {method!r} is not an accepted --method value for {skill!r}"
            )


def build_candidate_chain_with_revision(
    registry: Any,
    *,
    skills_root: Path,
    skills: Iterable[str],
    method_bindings: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a selected plan and revision from one read-stable authority."""
    selected_skills = list(dict.fromkeys(str(skill) for skill in skills))
    _validate_registry_method_bindings(
        registry,
        skills=selected_skills,
        method_bindings=method_bindings,
    )
    review_path = skills_root / "skill_dag_reviews.yaml"
    review_bytes = _read_optional_review_bytes(review_path)
    reviews = _parse_skill_dag_reviews(review_bytes, path=review_path)
    graph = build_skill_dag(registry, reviews=reviews)
    selected = build_candidate_chain(
        graph,
        selected_skills,
        method_bindings=method_bindings,
    )
    if _read_optional_review_bytes(review_path) != review_bytes:
        raise ValueError("skill DAG review authority changed while being read")
    revision = {
        "graph_schema_version": int(graph.get("schema_version") or 0),
        "reviews_hash": "sha256:"
        + hashlib.sha256(review_bytes or b"").hexdigest(),
        "selected_graph_hash": candidate_plan_graph_hash(selected),
    }
    return selected, revision


def candidate_graph_revision(
    registry: Any,
    *,
    skills_root: Path,
    skills: Iterable[str],
    method_bindings: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fingerprint selected graph authority and its review overlay."""
    _selected, revision = build_candidate_chain_with_revision(
        registry,
        skills_root=skills_root,
        skills=skills,
        method_bindings=method_bindings,
    )
    return revision


def candidate_plan_digest(plan: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 identity used by confirmation gates."""
    payload = json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
