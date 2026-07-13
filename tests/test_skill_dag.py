from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from omicsclaw.skill.registry import OmicsRegistry
from omicsclaw.skill.capability_resolver import resolve_capability
from omicsclaw.skill.skill_dag import (
    SkillDagCycleError,
    build_candidate_chain,
    build_skill_dag,
    detect_cycle,
    downstream_skills,
    topological_sort,
    upstream_closure,
)


@dataclass
class _Registry:
    entries: dict[str, dict]

    def iter_primary_skills(self):
        return list(self.entries.items())


def _skill(
    *,
    domain: str = "singlecell",
    modalities: list[str] | None = None,
    file_types: list[str] | None = None,
    path_kinds: list[str] | None = None,
    requires_preprocessed: bool = False,
    input_anndata: dict[str, list[str]] | None = None,
    output_files: list[str] | None = None,
    output_anndata: dict | None = None,
    skip_when: list[dict] | None = None,
) -> dict:
    data_shape = {"requires_preprocessed": requires_preprocessed}
    data_shape.update(input_anndata or {})
    return {
        "domain": domain,
        "type": "leaf",
        "validation_level": "fixture-validated",
        "lifecycle_status": "mvp",
        "skip_when": skip_when or [],
        "requires_preprocessed": requires_preprocessed,
        "input_contract": {
            "modalities": modalities if modalities is not None else ["scrna"],
            "file_types": file_types or ["h5ad"],
            "path_kinds": path_kinds or ["file"],
            "preconditions": {"data_shape": data_shape},
        },
        "output_contract": {
            "files": output_files or [],
            "result_json": {"required_keys": []},
            "anndata": output_anndata,
        },
    }


def _edge(graph: dict, source: str, target: str, precondition: str) -> dict:
    return next(
        edge
        for edge in graph["edges"]
        if edge["source"] == source
        and edge["target"] == target
        and edge["matched_precondition_key"] == precondition
    )


def test_exact_anndata_match_emits_provenance_rich_unreviewed_candidate():
    registry = _Registry(
        {
            "preprocess": _skill(
                output_files=["processed.h5ad"],
                output_anndata={"saves_h5ad": True, "obsm": ["X_pca"]},
                skip_when=[{"condition": "never put routing negatives in the DAG"}],
            ),
            "cluster": _skill(input_anndata={"obsm": ["X_pca"]}),
        }
    )

    graph = build_skill_dag(registry)
    edge = _edge(graph, "preprocess", "cluster", "data_shape.obsm.X_pca")

    assert edge == {
        "source": "preprocess",
        "target": "cluster",
        "edge_kind": "alternative",
        "matched_output_key": "anndata.obsm.X_pca",
        "matched_precondition_key": "data_shape.obsm.X_pca",
        "condition_scope": None,
        "confidence": 0.95,
        "reviewed": False,
    }
    assert "skip_when" not in repr(graph)


def test_multiple_exact_producers_are_alternatives_not_fake_requirements():
    registry = _Registry(
        {
            "pca-a": _skill(
                output_files=["a.h5ad"],
                output_anndata={"saves_h5ad": True, "obsm": ["X_pca"]},
            ),
            "pca-b": _skill(
                output_files=["b.h5ad"],
                output_anndata={"saves_h5ad": True, "obsm": ["X_pca"]},
            ),
            "cluster": _skill(input_anndata={"obsm": ["X_pca"]}),
        }
    )

    graph = build_skill_dag(registry)

    assert {
        _edge(graph, name, "cluster", "data_shape.obsm.X_pca")["edge_kind"]
        for name in ("pca-a", "pca-b")
    } == {"alternative"}


def test_review_overlay_can_promote_but_never_create_a_derived_edge():
    registry = _Registry(
        {
            "preprocess": _skill(
                output_files=["processed.h5ad"],
                output_anndata={"saves_h5ad": True, "obsm": ["X_pca"]},
            ),
            "cluster": _skill(input_anndata={"obsm": ["X_pca"]}),
        }
    )
    identity = (
        "preprocess",
        "cluster",
        "anndata.obsm.X_pca",
        "data_shape.obsm.X_pca",
    )
    review = {
        "edge_kind": "preferred",
        "condition_scope": None,
        "reviewed_by": "test reviewer",
        "reviewed_at": "2026-07-13",
        "rationale": "contract and implementation inspected",
    }

    graph = build_skill_dag(registry, reviews={identity: review})
    edge = _edge(graph, "preprocess", "cluster", "data_shape.obsm.X_pca")

    assert edge["edge_kind"] == "preferred"
    assert edge["reviewed"] is True
    assert edge["review"]["reviewed_by"] == "test reviewer"
    assert graph["summary"]["reviewed_edge_count"] == 1
    with pytest.raises(ValueError, match="missing derived edges"):
        build_skill_dag(
            registry,
            reviews={
                ("missing", "cluster", "anndata.obsm.X", "data_shape.obsm.X"): review
            },
        )


def test_preprocessed_match_is_low_confidence_and_requires_compatible_contracts():
    registry = _Registry(
        {
            "preprocess": _skill(
                output_files=["processed.h5ad"],
                output_anndata={
                    "saves_h5ad": True,
                    "processing_state": "preprocessed",
                },
            ),
            "consumer": _skill(requires_preprocessed=True),
            "wrong-domain": _skill(
                domain="spatial",
                requires_preprocessed=True,
                modalities=["visium"],
            ),
            "directory-consumer": _skill(
                requires_preprocessed=True,
                path_kinds=["directory"],
                file_types=["zarr"],
            ),
        }
    )

    graph = build_skill_dag(registry)
    edge = _edge(
        graph,
        "preprocess",
        "consumer",
        "data_shape.requires_preprocessed",
    )

    assert edge["matched_output_key"] == "files.processed.h5ad"
    assert edge["confidence"] == 0.55
    assert edge["condition_scope"] is None
    assert edge["reviewed"] is False
    assert not any(e["target"] in {"wrong-domain", "directory-consumer"} for e in graph["edges"])


def test_downstream_exact_output_cannot_reverse_a_preprocessing_stage():
    registry = _Registry(
        {
            "preprocess": _skill(
                input_anndata={"obsm": ["spatial"]},
                output_files=["processed.h5ad"],
                output_anndata={
                    "saves_h5ad": True,
                    "processing_state": "preprocessed",
                    "obsm": ["X_pca"],
                },
            ),
            "register": _skill(
                requires_preprocessed=True,
                output_files=["registered.h5ad"],
                output_anndata={"saves_h5ad": True, "obsm": ["spatial"]},
            ),
        }
    )

    graph = build_skill_dag(registry)

    assert not any(
        e["source"] == "register" and e["target"] == "preprocess"
        for e in graph["edges"]
    )
    assert _edge(
        graph,
        "preprocess",
        "register",
        "data_shape.requires_preprocessed",
    )
    assert graph["diagnostics"]["cycles"] == []
    assert detect_cycle(graph) == []


def test_generic_processed_edges_require_preprocessing_signature_and_known_modality():
    registry = _Registry(
        {
            "preprocess": _skill(
                output_files=["processed.h5ad"],
                output_anndata={
                    "saves_h5ad": True,
                    "processing_state": "preprocessed",
                    "obsm": ["X_pca"],
                },
            ),
            "pathway-scoring": _skill(
                output_files=["processed.h5ad"],
                output_anndata={"saves_h5ad": True, "obs": ["pathway_score"]},
            ),
            "consumer": _skill(requires_preprocessed=True),
            "unknown-modality": _skill(
                requires_preprocessed=True,
                modalities=[],
            ),
        }
    )

    graph = build_skill_dag(registry)

    assert _edge(
        graph,
        "preprocess",
        "consumer",
        "data_shape.requires_preprocessed",
    )
    assert not any(edge["source"] == "pathway-scoring" for edge in graph["edges"])
    assert not any(edge["target"] == "unknown-modality" for edge in graph["edges"])


def test_cycle_detection_and_topological_queries_are_deterministic():
    graph = {
        "nodes": [{"skill": name} for name in ("a", "b", "c", "d")],
        "edges": [
            {"source": "a", "target": "b"},
            {"source": "b", "target": "c"},
        ],
    }

    assert topological_sort(graph) == ["a", "b", "c", "d"]
    assert upstream_closure(graph, "c") == ["a", "b"]
    assert downstream_skills(graph, "a") == ["b", "c"]
    assert build_candidate_chain(graph, ["c", "a", "b"]) == {
        "requested_skills": ["c", "a", "b"],
        "skills": ["a", "b", "c"],
        "phases": [["a"], ["b"], ["c"]],
        "edges": graph["edges"],
        "validated_order": True,
        "unresolved_pairs": [],
    }

    cyclic = {
        "nodes": graph["nodes"],
        "edges": graph["edges"] + [{"source": "c", "target": "a"}],
    }
    assert detect_cycle(cyclic) == ["a", "b", "c", "a"]
    assert upstream_closure(cyclic, "a") == ["b", "c"]
    assert downstream_skills(cyclic, "a") == ["b", "c"]
    with pytest.raises(SkillDagCycleError, match="a -> b -> c -> a"):
        topological_sort(cyclic)


def test_real_spatial_pipeline_has_no_reverse_compatibility_edge():
    registry = OmicsRegistry()
    registry.load_all()
    graph = build_skill_dag(registry)
    pipeline_path = Path(__file__).parents[1] / "pipelines" / "spatial-pipeline.yaml"
    pipeline = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    steps = [step["skill"] for step in pipeline["steps"]]
    position = {skill: index for index, skill in enumerate(steps)}

    assert detect_cycle(graph) == []
    assert not [
        edge
        for edge in graph["edges"]
        if edge["source"] in position
        and edge["target"] in position
        and position[edge["source"]] > position[edge["target"]]
    ]


def test_registry_exposes_canonical_graph_queries_and_provenance_chain():
    registry = OmicsRegistry()
    registry.load_all()

    graph = registry.build_compatibility_dag()
    assert graph["summary"]["node_count"] == 95
    assert graph["summary"]["reviewed_edge_count"] == 6
    assert graph["summary"]["edge_kind_counts"]["preferred"] == 6
    assert "spatial-preprocess" in registry.get_upstream_skills("spatial-trajectory")
    assert "spatial-trajectory" in registry.get_downstream_skills("spatial-preprocess")
    assert registry.topological_skill_order(
        ["spatial-trajectory", "spatial-raw-processing", "spatial-preprocess"]
    ) == ["spatial-raw-processing", "spatial-preprocess", "spatial-trajectory"]

    chain = registry.build_candidate_skill_chain(
        ["spatial-trajectory", "spatial-raw-processing", "spatial-preprocess"]
    )
    assert chain["skills"] == [
        "spatial-raw-processing",
        "spatial-preprocess",
        "spatial-trajectory",
    ]
    assert all(
        {
            "matched_output_key",
            "matched_precondition_key",
            "confidence",
            "reviewed",
        }.issubset(edge)
        for edge in chain["edges"]
    )


def test_composite_query_returns_provenance_candidate_topo_chain():
    decision = resolve_capability("run sc-preprocessing and then sc-clustering")

    assert decision.coverage == "partial_skill"
    assert decision.candidate_chain["skills"] == ["sc-preprocessing", "sc-clustering"]
    assert decision.candidate_chain["phases"] == [["sc-preprocessing"], ["sc-clustering"]]
    assert decision.candidate_chain["edges"]
    assert all(edge["source"] == "sc-preprocessing" for edge in decision.candidate_chain["edges"])
    assert all(edge["target"] == "sc-clustering" for edge in decision.candidate_chain["edges"])
    assert decision.to_dict()["candidate_chain"] == decision.candidate_chain

    reverse_wording = resolve_capability(
        "run sc-clustering and then sc-preprocessing"
    )
    assert reverse_wording.candidate_chain["skills"] == [
        "sc-preprocessing",
        "sc-clustering",
    ]


def test_composite_query_does_not_invent_order_without_graph_evidence():
    decision = resolve_capability("run sc-de and then sc-markers")

    assert decision.coverage == "partial_skill"
    assert decision.candidate_chain["requested_skills"] == ["sc-de", "sc-markers"]
    assert decision.candidate_chain["edges"] == []
    assert decision.candidate_chain["validated_order"] is False
    assert decision.candidate_chain["unresolved_pairs"] == [
        {
            "source": "sc-de",
            "target": "sc-markers",
            "reason": "no_compatibility_edge",
        }
    ]


@pytest.mark.parametrize(
    "query, expected",
    [
        (
            "run sc-de and then sc-enrichment",
            ["sc-de", "sc-enrichment"],
        ),
        (
            "run sc-qc plus sc-doublet-detection",
            ["sc-qc", "sc-doublet-detection"],
        ),
        (
            "please combine sc-preprocessing and sc-clustering",
            ["sc-preprocessing", "sc-clustering"],
        ),
    ],
)
def test_composite_query_never_silently_drops_a_resolved_intent(query, expected):
    decision = resolve_capability(query)

    assert decision.coverage == "partial_skill"
    assert decision.candidate_chain["requested_skills"] == expected


def test_composite_query_supports_chinese_sequence_wording():
    decision = resolve_capability("先运行 sc-preprocessing，然后运行 sc-clustering")

    assert decision.coverage == "partial_skill"
    assert decision.candidate_chain["requested_skills"] == [
        "sc-preprocessing",
        "sc-clustering",
    ]
    assert decision.candidate_chain["skills"] == [
        "sc-preprocessing",
        "sc-clustering",
    ]


def test_composite_query_deduplicates_repeated_skill_aliases():
    decision = resolve_capability(
        "run sc-preprocessing and then sc-preprocessing"
    )

    assert decision.candidate_chain == {}


@pytest.mark.parametrize(
    "query",
    [
        "what is the difference between sc-de and sc-markers?",
        "compare sc-de and sc-markers",
        "explain sc-de and sc-markers",
        "can sc-de and sc-markers be used together?",
        "should I use sc-de and sc-markers?",
        "how do I use sc-de and sc-markers?",
        "explain how to run sc-de and sc-markers",
        "do sc-de and sc-markers require the same input?",
    ],
)
def test_multi_skill_comparison_does_not_create_an_execution_plan(query):
    decision = resolve_capability(query)

    assert decision.candidate_chain == {}


def test_pathway_output_is_not_misrepresented_as_clustering_preprocessing():
    decision = resolve_capability(
        "run sc-pathway-scoring and then sc-clustering"
    )

    assert decision.candidate_chain["requested_skills"] == [
        "sc-pathway-scoring",
        "sc-clustering",
    ]
    assert decision.candidate_chain["edges"] == []
    assert decision.candidate_chain["validated_order"] is False
