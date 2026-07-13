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
from heapq import heapify, heappop, heappush
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml


ANNDATA_COLLECTIONS = ("obs", "obsm", "var", "layers", "uns")
EDGE_KINDS = frozenset({"required", "optional", "preferred", "alternative"})
EdgeReviewKey = tuple[str, str, str, str]


class SkillDagCycleError(ValueError):
    """Raised when a topological query encounters a compatibility cycle."""


def load_skill_dag_reviews(path: Path) -> dict[EdgeReviewKey, dict[str, Any]]:
    """Load the governed manual review overlay without creating graph edges."""

    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ValueError(f"invalid skill DAG review schema: {path}")
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
        key = tuple(str(entry.get(field) or "").strip() for field in required)
        if not all(key):
            raise ValueError(f"skill DAG review #{index} has an incomplete edge identity")
        edge_kind = str(entry.get("edge_kind") or "").strip()
        if edge_kind not in EDGE_KINDS:
            raise ValueError(f"skill DAG review #{index} has invalid edge_kind {edge_kind!r}")
        if key in reviews:
            raise ValueError(f"duplicate skill DAG review identity: {key}")
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
        reviews[key] = {
            "edge_kind": edge_kind,
            "condition_scope": condition_scope,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "rationale": rationale,
        }
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


def _has_h5ad_artifact(info: Mapping[str, Any], *, processed_only: bool = False) -> bool:
    if not bool(_anndata_output(info).get("saves_h5ad")):
        return False
    names = {PurePosixPath(path.replace("\\", "/")).name.lower() for path in _output_files(info)}
    if processed_only:
        return "processed.h5ad" in names
    return any(name.endswith(".h5ad") for name in names)


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
    confidence: float,
) -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "edge_kind": edge_kind,
        "matched_output_key": matched_output_key,
        "matched_precondition_key": matched_precondition_key,
        # Method-specific requirements are not yet represented by skill.yaml.
        # Fail closed instead of inferring a scope from prose or parameter names.
        "condition_scope": None,
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
        }
        for name, info in primary
    ]

    exact_groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
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
                    if key not in _strings(_anndata_output(source_info).get(collection)):
                        continue
                    exact_groups[(target, precondition_key)].append((source, target))

    edges: list[dict[str, Any]] = []
    for (target, precondition_key), candidates in sorted(exact_groups.items()):
        collection, key = precondition_key.split(".")[1:]
        for source, _target in sorted(set(candidates)):
            edges.append(
                _edge(
                    source,
                    target,
                    edge_kind="alternative",
                    matched_output_key=f"anndata.{collection}.{key}",
                    matched_precondition_key=precondition_key,
                    confidence=0.95,
                )
            )

    generic_groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
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
            generic_groups[(target, precondition_key)].append((source, target))

    for (target, precondition_key), candidates in sorted(generic_groups.items()):
        for source, _target in sorted(set(candidates)):
            edges.append(
                _edge(
                    source,
                    target,
                    edge_kind="alternative",
                    matched_output_key="files.processed.h5ad",
                    matched_precondition_key=precondition_key,
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
    for edge in edges:
        identity = (
            edge["source"],
            edge["target"],
            edge["matched_output_key"],
            edge["matched_precondition_key"],
        )
        review = review_map.get(identity)
        if review is None:
            continue
        edge_kind = str(review.get("edge_kind") or "")
        if edge_kind not in EDGE_KINDS:
            raise ValueError(f"invalid reviewed edge_kind {edge_kind!r} for {identity}")
        edge["edge_kind"] = edge_kind
        edge["condition_scope"] = review.get("condition_scope")
        edge["reviewed"] = True
        edge["review"] = {
            "reviewed_by": str(review.get("reviewed_by") or ""),
            "reviewed_at": str(review.get("reviewed_at") or ""),
            "rationale": str(review.get("rationale") or ""),
        }
        matched_reviews.add(identity)
    stale_reviews = sorted(set(review_map) - matched_reviews)
    if stale_reviews:
        raise ValueError(f"skill DAG reviews reference missing derived edges: {stale_reviews}")

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
        "exact_edge_count": sum(edge["confidence"] == 0.95 for edge in edges),
        "generic_edge_count": sum(edge["confidence"] == 0.55 for edge in edges),
        "reviewed_edge_count": sum(bool(edge["reviewed"]) for edge in edges),
        "has_cycle": bool(cycle),
        "edge_kind_counts": {kind: kinds.get(kind, 0) for kind in sorted(EDGE_KINDS)},
    }
    graph["diagnostics"] = {"cycles": [cycle] if cycle else []}
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


def build_candidate_chain(graph: Mapping[str, Any], skills: Iterable[str]) -> dict[str, Any]:
    """Return a cycle-free induced plan plus the provenance edges that ordered it."""

    requested = list(dict.fromkeys(str(skill) for skill in skills))
    selected = set(requested)
    order = topological_sort(graph, skills=selected)
    edges = [dict(edge) for edge in _selected_edges(graph, selected)]
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

    unresolved = [
        {
            "source": source,
            "target": target,
            "reason": "no_compatibility_edge",
        }
        for source, target in zip(requested, requested[1:])
        if not connected(source, target) and not connected(target, source)
    ]
    return {
        "requested_skills": requested,
        "skills": order,
        "phases": _execution_phases(graph, selected),
        "edges": edges,
        "validated_order": not unresolved,
        "unresolved_pairs": unresolved,
    }
