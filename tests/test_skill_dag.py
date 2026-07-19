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
    build_candidate_chain_with_revision,
    build_skill_dag,
    candidate_plan_graph_hash,
    candidate_plan_digest,
    detect_cycle,
    downstream_skills,
    load_skill_dag_reviews_with_revision,
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
    input_artifacts: list[dict] | None = None,
    output_artifacts: list[dict] | None = None,
    output_method_scopes: list[dict] | None = None,
    skip_when: list[dict] | None = None,
    compute_resources: dict | None = None,
) -> dict:
    data_shape = {"requires_preprocessed": requires_preprocessed}
    data_shape.update(input_anndata or {})
    return {
        "domain": domain,
        "type": "leaf",
        "validation_level": "fixture-validated",
        "lifecycle_status": "mvp",
        "compute_resources": compute_resources,
        "skip_when": skip_when or [],
        "requires_preprocessed": requires_preprocessed,
        "input_contract": {
            "modalities": modalities if modalities is not None else ["scrna"],
            "file_types": file_types or ["h5ad"],
            "path_kinds": path_kinds or ["file"],
            "artifacts": input_artifacts or [],
            "preconditions": {"data_shape": data_shape},
        },
        "output_contract": {
            "files": output_files or [],
            "result_json": {"required_keys": []},
            "anndata": output_anndata,
            "artifacts": output_artifacts or [],
            "method_scopes": output_method_scopes or [],
        },
    }


def test_candidate_plan_carries_digest_bound_compute_reservations() -> None:
    producer_request = {
        "cpu_cores": 2,
        "memory_mib": 4096,
        "gpu_devices": 0,
        "threads": 2,
        "temporary_disk_mib": 2048,
    }
    consumer_request = producer_request | {"memory_mib": 8192}
    graph = build_skill_dag(
        _Registry(
            {
                "producer": _skill(compute_resources=producer_request),
                "consumer": _skill(compute_resources=consumer_request),
            }
        )
    )

    plan = build_candidate_chain(graph, ["producer", "consumer"])

    assert plan["resource_requests"] == {
        "consumer": consumer_request,
        "producer": producer_request,
    }
    assert plan["resource_ready"] is True
    changed = {
        **plan,
        "resource_requests": {
            **plan["resource_requests"],
            "consumer": consumer_request | {"memory_mib": 4096},
        },
    }
    assert candidate_plan_digest(plan) != candidate_plan_digest(changed)


def _edge(graph: dict, source: str, target: str, precondition: str) -> dict:
    return next(
        edge
        for edge in graph["edges"]
        if edge["source"] == source
        and edge["target"] == target
        and edge["matched_precondition_key"] == precondition
    )


@pytest.mark.parametrize(
    "review_yaml",
    [
        """
        schema_version: 2
        schema_version: 2
        reviews: []
        """,
        """
        schema_version: 2
        reviews: []
        reviews: []
        """,
        """
        schema_version: 2
        reviews:
          - source: producer
            target: consumer
            matched_output_key: artifacts.test.table
            matched_precondition_key: artifacts.test.table
            decision: rejected
            decision: accepted
            edge_kind: preferred
            condition_scope:
            reviewed_by: reviewer
            reviewed_at: '2026-07-16'
            rationale: exact authority must be unambiguous
        """,
        """
        schema_version: 2
        reviews:
          - source: producer
            target: consumer
            matched_output_key: artifacts.test.table
            matched_precondition_key: artifacts.test.table
            decision: accepted
            edge_kind: preferred
            condition_scope:
              source_methods: [a]
              source_methods: [b]
            reviewed_by: reviewer
            reviewed_at: '2026-07-16'
            rationale: exact authority must be unambiguous
        """,
    ],
)
def test_review_authority_rejects_duplicate_yaml_mapping_keys(
    tmp_path: Path,
    review_yaml: str,
) -> None:
    review_path = tmp_path / "skill_dag_reviews.yaml"
    review_path.write_text(review_yaml, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate YAML mapping key"):
        load_skill_dag_reviews_with_revision(review_path)


@pytest.mark.parametrize(
    "review_yaml,match",
    [
        (
            "schema_version: 2\nreviews: []\nrevievs: []\n",
            "unsupported top-level fields",
        ),
        (
            """
            schema_version: 2
            reviews:
              - source: producer
                target: consumer
                matched_output_key: artifacts.test.table
                matched_precondition_key: artifacts.test.table
                decision: accepted
                decison: rejected
                edge_kind: preferred
                condition_scope:
                reviewed_by: reviewer
                reviewed_at: '2026-07-16'
                rationale: typo must not be ignored
            """,
            "unsupported fields",
        ),
    ],
)
def test_review_authority_rejects_unknown_schema_fields(
    tmp_path: Path,
    review_yaml: str,
    match: str,
) -> None:
    review_path = tmp_path / "skill_dag_reviews.yaml"
    review_path.write_text(review_yaml, encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        load_skill_dag_reviews_with_revision(review_path)


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
        "matched_output_path": "processed.h5ad",
        "condition_scope": None,
        "confidence": 0.95,
        "reviewed": False,
    }
    assert "skip_when" not in repr(graph)


def test_exact_anndata_edge_prefers_canonical_processed_h5ad_over_aliases():
    registry = _Registry(
        {
            "preprocess": _skill(
                output_files=["adata_alias.h5ad", "processed.h5ad"],
                output_anndata={"saves_h5ad": True, "obsm": ["X_pca"]},
            ),
            "cluster": _skill(input_anndata={"obsm": ["X_pca"]}),
        }
    )

    edge = _edge(
        build_skill_dag(registry),
        "preprocess",
        "cluster",
        "data_shape.obsm.X_pca",
    )
    assert edge["matched_output_path"] == "processed.h5ad"


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
    assert {
        name: _edge(graph, name, "cluster", "data_shape.obsm.X_pca")[
            "matched_output_path"
        ]
        for name in ("pca-a", "pca-b")
    } == {"pca-a": "a.h5ad", "pca-b": "b.h5ad"}


def test_semantic_artifact_contracts_create_exact_cross_domain_edges_without_filename_guessing():
    registry = _Registry(
        {
            "literature": _skill(
                domain="literature",
                output_files=["extracted_metadata.json"],
                output_artifacts=[
                    {
                        "kind": "literature.dataset_handoff",
                        "path": "extracted_metadata.json",
                        "format": "json",
                    }
                ],
            ),
            "orchestrator": _skill(
                domain="orchestrator",
                input_artifacts=[
                    {"kind": "literature.dataset_handoff", "formats": ["json"]}
                ],
            ),
            "wrong-format": _skill(
                domain="orchestrator",
                input_artifacts=[
                    {"kind": "literature.dataset_handoff", "formats": ["csv"]}
                ],
            ),
        }
    )

    graph = build_skill_dag(registry)

    edge = _edge(
        graph,
        "literature",
        "orchestrator",
        "artifacts.literature.dataset_handoff",
    )
    assert edge["matched_output_key"] == "artifacts.literature.dataset_handoff"
    assert edge["matched_output_path"] == "extracted_metadata.json"
    assert edge["confidence"] == 0.9
    assert not any(item["target"] == "wrong-format" for item in graph["edges"])


def test_method_scoped_semantic_artifact_edge_carries_source_method_condition():
    registry = _Registry(
        {
            "producer": _skill(
                output_files=["artifact.csv"],
                output_method_scopes=[
                    {
                        "methods": ["method_a"],
                        "artifacts": [
                            {
                                "kind": "test.conditional_table",
                                "path": "artifact.csv",
                                "format": "csv",
                            }
                        ],
                    }
                ],
            ),
            "consumer": _skill(
                input_artifacts=[
                    {"kind": "test.conditional_table", "formats": ["csv"]}
                ]
            ),
        }
    )

    graph = build_skill_dag(registry)
    edge = _edge(
        graph,
        "producer",
        "consumer",
        "artifacts.test.conditional_table",
    )

    assert edge["condition_scope"] == {"source_methods": ["method_a"]}


def test_method_scoped_anndata_field_edge_carries_source_method_condition():
    registry = _Registry(
        {
            "velocity": _skill(
                output_files=["processed.h5ad"],
                output_anndata={"saves_h5ad": True, "layers": ["velocity"]},
                output_method_scopes=[
                    {
                        "methods": ["scvelo_dynamical"],
                        "anndata": {"obs": ["latent_time"]},
                    }
                ],
            ),
            "consumer": _skill(input_anndata={"obs": ["latent_time"]}),
        }
    )

    graph = build_skill_dag(registry)
    edge = _edge(graph, "velocity", "consumer", "data_shape.obs.latent_time")

    assert edge["matched_output_path"] == "processed.h5ad"
    assert edge["condition_scope"] == {
        "source_methods": ["scvelo_dynamical"]
    }


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
        "decision": "accepted",
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


def test_review_overlay_cannot_erase_or_invent_a_derived_method_scope():
    registry = _Registry(
        {
            "producer": _skill(
                output_files=["artifact.csv"],
                output_method_scopes=[
                    {
                        "methods": ["method_a"],
                        "artifacts": [
                            {
                                "kind": "test.conditional_table",
                                "path": "artifact.csv",
                                "format": "csv",
                            }
                        ],
                    }
                ],
            ),
            "consumer": _skill(
                input_artifacts=[
                    {"kind": "test.conditional_table", "formats": ["csv"]}
                ]
            ),
        }
    )
    identity = (
        "producer",
        "consumer",
        "artifacts.test.conditional_table",
        "artifacts.test.conditional_table",
    )
    review = {
        "decision": "accepted",
        "edge_kind": "preferred",
        "condition_scope": None,
        "reviewed_by": "test reviewer",
        "reviewed_at": "2026-07-14",
        "rationale": "scope must remain derived from the manifest",
    }

    with pytest.raises(ValueError, match="condition_scope does not match"):
        build_skill_dag(registry, reviews={identity: review})

    review["condition_scope"] = {"source_methods": ["method_a"]}
    edge = _edge(
        build_skill_dag(registry, reviews={identity: review}),
        "producer",
        "consumer",
        "artifacts.test.conditional_table",
    )
    assert edge["condition_scope"] == {"source_methods": ["method_a"]}
    assert edge["reviewed"] is True


def test_review_overlay_can_explicitly_reject_a_derived_edge():
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
        "decision": "rejected",
        "edge_kind": "alternative",
        "condition_scope": None,
        "reviewed_by": "test reviewer",
        "reviewed_at": "2026-07-14",
        "rationale": "the implementation does not preserve this artifact",
    }

    graph = build_skill_dag(registry, reviews={identity: review})

    assert graph["edges"] == []
    assert graph["summary"]["rejected_edge_count"] == 1
    assert graph["diagnostics"]["rejected_edges"] == [
        {
            "source": "preprocess",
            "target": "cluster",
            "matched_output_key": "anndata.obsm.X_pca",
            "matched_precondition_key": "data_shape.obsm.X_pca",
            "review": {
                "decision": "rejected",
                "reviewed_by": "test reviewer",
                "reviewed_at": "2026-07-14",
                "rationale": "the implementation does not preserve this artifact",
            },
        }
    ]


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
        "resource_requests": {},
        "resource_ready": False,
        "missing_resource_requests": ["a", "b", "c"],
    }
    plan = build_candidate_chain(graph, ["c", "a", "b"])
    assert len(candidate_plan_digest(plan)) == 64
    assert candidate_plan_digest(plan) == candidate_plan_digest(dict(reversed(list(plan.items()))))

    cyclic = {
        "nodes": graph["nodes"],
        "edges": graph["edges"] + [{"source": "c", "target": "a"}],
    }
    assert detect_cycle(cyclic) == ["a", "b", "c", "a"]
    assert upstream_closure(cyclic, "a") == ["b", "c"]
    assert downstream_skills(cyclic, "a") == ["b", "c"]
    with pytest.raises(SkillDagCycleError, match="a -> b -> c -> a"):
        topological_sort(cyclic)


def test_candidate_chain_requires_matching_method_binding_for_scoped_edge():
    graph = {
        "nodes": [{"skill": name} for name in ("producer", "consumer")],
        "edges": [
            {
                "source": "producer",
                "target": "consumer",
                "edge_kind": "preferred",
                "matched_output_key": "artifacts.test.table",
                "matched_precondition_key": "artifacts.test.table",
                "matched_output_path": "table.csv",
                "condition_scope": {"source_methods": ["method_a"]},
                "confidence": 0.9,
                "reviewed": True,
            }
        ],
    }

    unbound = build_candidate_chain(graph, ["producer", "consumer"])
    assert unbound["edges"] == []
    assert unbound["validated_order"] is False
    assert unbound["unresolved_pairs"] == [
        {
            "source": "producer",
            "target": "consumer",
            "reason": "method_binding_required",
            "skill": "producer",
            "allowed_methods": ["method_a"],
        }
    ]

    matching = build_candidate_chain(
        graph,
        ["producer", "consumer"],
        method_bindings={"producer": "method_a"},
    )
    assert matching["validated_order"] is True
    assert matching["edges"] == graph["edges"]
    assert matching["method_bindings"] == {"producer": "method_a"}

    wrong = build_candidate_chain(
        graph,
        ["producer", "consumer"],
        method_bindings={"producer": "method_b"},
    )
    assert wrong["edges"] == []
    assert wrong["unresolved_pairs"][0] == {
        "source": "producer",
        "target": "consumer",
        "reason": "method_scope_mismatch",
        "skill": "producer",
        "selected_method": "method_b",
        "allowed_methods": ["method_a"],
    }


def test_real_spatial_pipeline_has_no_reverse_compatibility_edge():
    registry = OmicsRegistry()
    registry.load_all()
    graph = registry.build_compatibility_dag()
    pipeline_path = Path(__file__).parents[1] / "pipelines" / "spatial-pipeline.yaml"
    pipeline = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    steps = [step["skill"] for step in pipeline["steps"]]
    position = {skill: index for index, skill in enumerate(steps)}

    assert detect_cycle(graph) == []
    induced = [
        edge
        for edge in graph["edges"]
        if edge["source"] in position and edge["target"] in position
    ]
    assert any(
        edge["source"] == "spatial-preprocess"
        and edge["target"] == "spatial-domains"
        and edge["reviewed"] is True
        for edge in induced
    )
    assert not [
        edge for edge in induced if position[edge["source"]] > position[edge["target"]]
    ]


def test_registry_exposes_canonical_graph_queries_and_provenance_chain():
    registry = OmicsRegistry()
    registry.load_all()

    graph = registry.build_compatibility_dag()
    assert graph["summary"]["node_count"] == 95
    assert graph["summary"]["reviewed_edge_count"] == 18
    assert graph["summary"]["method_scoped_skill_count"] == 2
    assert graph["summary"]["conditional_edge_count"] == 0
    assert graph["summary"]["edge_kind_counts"]["preferred"] == 18
    artifact_domains = {
        node["domain"]
        for node in graph["nodes"]
        if node["artifact_inputs"] or node["artifact_outputs"]
    }
    assert {
        "genomics",
        "proteomics",
        "metabolomics",
        "bulkrna",
        "orchestrator",
        "literature",
    } <= artifact_domains
    artifact_edge_domains = {
        next(node["domain"] for node in graph["nodes"] if node["skill"] == edge["source"])
        for edge in graph["edges"]
        if edge["matched_output_key"].startswith("artifacts.")
    }
    assert {"genomics", "proteomics", "metabolomics", "bulkrna"} <= artifact_edge_domains
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
    assert set(chain["skill_revisions"]) == set(chain["skills"])
    assert chain["plan_schema_version"] == 2
    assert set(chain["graph_revision"]) == {
        "graph_schema_version",
        "reviews_hash",
        "selected_graph_hash",
    }
    assert chain["graph_revision"]["graph_schema_version"] == 1
    assert chain["graph_revision"]["reviews_hash"].startswith("sha256:")
    assert chain["graph_revision"]["selected_graph_hash"].startswith("sha256:")
    assert (
        candidate_plan_graph_hash(chain)
        == chain["graph_revision"]["selected_graph_hash"]
    )
    for skill in chain["skills"]:
        revision = chain["skill_revisions"][skill]
        assert revision["skill_id"] == skill
        assert revision["skill_version"]
        assert revision["manifest_hash"].startswith("sha256:")
        assert revision["source_hash"].startswith("sha256:")

    changed_revision = dict(chain)
    changed_revision["skill_revisions"] = {
        name: dict(revision)
        for name, revision in chain["skill_revisions"].items()
    }
    changed_revision["skill_revisions"][chain["skills"][0]]["source_hash"] = (
        "sha256:" + "0" * 64
    )
    assert candidate_plan_digest(changed_revision) != candidate_plan_digest(chain)

    bound_chain = registry.build_candidate_skill_chain(
        ["sc-preprocessing", "sc-clustering"],
        method_bindings={"sc-preprocessing": "scanpy"},
    )
    assert bound_chain["method_bindings"] == {"sc-preprocessing": "scanpy"}
    with pytest.raises(ValueError, match="not declared in param_hints"):
        registry.build_candidate_skill_chain(
            ["sc-preprocessing", "sc-clustering"],
            method_bindings={"sc-preprocessing": "invented"},
        )


def test_registry_rejects_documentation_profile_not_accepted_by_method_cli():
    registry = OmicsRegistry()
    registry.load_all(Path(__file__).resolve().parents[1] / "skills")

    with pytest.raises(ValueError, match="not an accepted --method value"):
        registry.build_candidate_skill_chain(
            ["sc-integrate-cluster"],
            method_bindings={"sc-integrate-cluster": "default"},
        )


@pytest.mark.parametrize(
    ("skill", "method"),
    [
        ("spatial-velocity", "velovi"),
        ("sc-velocity", "scvelo_dynamical"),
    ],
)
def test_registry_accepts_real_runtime_method_choices(
    skill: str,
    method: str,
) -> None:
    registry = OmicsRegistry()
    registry.load_all(Path(__file__).resolve().parents[1] / "skills")

    plan = registry.build_candidate_skill_chain(
        [skill],
        method_bindings={skill: method},
    )

    assert plan["method_bindings"] == {skill: method}


def test_formal_unified_method_profiles_match_runtime_choices_audit() -> None:
    from omicsclaw.skill.skill_dag import (
        method_binding_is_runtime_accepted,
        supports_unified_method_binding,
    )

    registry = OmicsRegistry()
    registry.load_all(Path(__file__).resolve().parents[1] / "skills")
    rejected: dict[str, list[str]] = {}
    for skill, info in registry.iter_primary_skills():
        if not supports_unified_method_binding(info):
            continue
        invalid = [
            method
            for method in (info.get("param_hints") or {})
            if not method_binding_is_runtime_accepted(info, method)
        ]
        if invalid:
            rejected[skill] = invalid

    # This one documentation profile is deliberately not executable: its
    # defaults choose ``none``, while the literal profile name ``default`` is
    # not one of argparse's --method choices. A future explicit binding schema
    # may map profiles to argv values; until then the plan gate rejects it.
    assert rejected == {"sc-integrate-cluster": ["default"]}


def test_candidate_chain_does_not_mix_cached_edges_with_rewritten_review_authority(
    tmp_path: Path,
) -> None:
    """A long-lived Registry must build the plan and revision from one review."""
    request = {
        "cpu_cores": 1,
        "memory_mib": 1024,
        "gpu_devices": 0,
        "threads": 1,
        "temporary_disk_mib": 1024,
    }
    skills_root = tmp_path / "skills"
    entries = {
        "producer": _skill(
            output_artifacts=[
                {"kind": "test.table", "path": "table.csv", "format": "csv"}
            ],
            compute_resources=request,
        ),
        "consumer": _skill(
            input_artifacts=[{"kind": "test.table", "formats": ["csv"]}],
            compute_resources=request,
        ),
    }
    registry = OmicsRegistry()
    for name, info in entries.items():
        skill_dir = skills_root / "demo" / name
        skill_dir.mkdir(parents=True)
        script = skill_dir / "entry.py"
        script.write_text("pass\n", encoding="utf-8")
        (skill_dir / "skill.yaml").write_text(
            f"id: {name}\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        info.update(
            {
                "alias": name,
                "canonical_name": name,
                "directory_name": name,
                "script": script,
                "version": "1.0.0",
            }
        )
    registry.skills = entries
    registry.canonical_aliases = ["producer", "consumer"]
    registry.domains = {"demo": {"name": "Demo"}}
    registry._loaded = True
    registry._loaded_dir = skills_root.resolve()

    review_path = skills_root / "skill_dag_reviews.yaml"
    review = {
        "source": "producer",
        "target": "consumer",
        "matched_output_key": "artifacts.test.table",
        "matched_precondition_key": "artifacts.test.table",
        "edge_kind": "preferred",
        "condition_scope": None,
        "reviewed_by": "test-reviewer",
        "reviewed_at": "2026-07-17T00:00:00Z",
        "rationale": "Exercise review-authority replacement.",
    }
    review_path.write_text(
        yaml.safe_dump(
            {"schema_version": 2, "reviews": [review | {"decision": "accepted"}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cached = registry.build_compatibility_dag()
    assert [(edge["source"], edge["target"]) for edge in cached["edges"]] == [
        ("producer", "consumer")
    ]

    review_path.write_text(
        yaml.safe_dump(
            {"schema_version": 2, "reviews": [review | {"decision": "rejected"}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    refreshed = registry.build_compatibility_dag()
    assert refreshed["edges"] == []
    assert registry.get_upstream_skills("consumer") == []
    assert registry.get_downstream_skills("producer") == []

    plan = registry.build_candidate_skill_chain(["producer", "consumer"])

    assert plan["edges"] == []
    assert plan["validated_order"] is False
    assert plan["unresolved_pairs"] == [
        {
            "source": "producer",
            "target": "consumer",
            "reason": "no_compatibility_edge",
        }
    ]
    assert (
        candidate_plan_graph_hash(plan)
        == plan["graph_revision"]["selected_graph_hash"]
    )


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("skills", ["b", "a"]),
        ("edges", [{"source": "a", "target": "b", "reviewed": True}]),
        ("validated_order", False),
        (
            "unresolved_pairs",
            [{"source": "a", "target": "b", "reason": "no_compatibility_edge"}],
        ),
        ("method_bindings", {"a": "method_b"}),
    ],
)
def test_selected_graph_hash_binds_every_authority_field(
    field: str,
    replacement: object,
) -> None:
    request = {
        "cpu_cores": 1,
        "memory_mib": 1024,
        "gpu_devices": 0,
        "threads": 1,
        "temporary_disk_mib": 1024,
    }
    plan = {
        "skills": ["a", "b"],
        "edges": [],
        "validated_order": True,
        "unresolved_pairs": [],
        "method_bindings": {"a": "method_a"},
        "resource_requests": {"a": dict(request), "b": dict(request)},
        "resource_ready": True,
        "missing_resource_requests": [],
    }
    changed = dict(plan)
    changed[field] = replacement

    assert candidate_plan_graph_hash(changed) != candidate_plan_graph_hash(plan)


def test_selected_graph_hash_binds_resource_readiness_partition() -> None:
    request = {
        "cpu_cores": 1,
        "memory_mib": 1024,
        "gpu_devices": 0,
        "threads": 1,
        "temporary_disk_mib": 1024,
    }
    ready = {
        "skills": ["a", "b"],
        "edges": [],
        "validated_order": True,
        "unresolved_pairs": [],
        "resource_requests": {"a": dict(request), "b": dict(request)},
        "resource_ready": True,
        "missing_resource_requests": [],
    }
    unready = {
        **ready,
        "resource_requests": {"a": dict(request)},
        "resource_ready": False,
        "missing_resource_requests": ["b"],
    }

    assert candidate_plan_graph_hash(unready) != candidate_plan_graph_hash(ready)


def test_candidate_graph_authority_rejects_review_change_during_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import omicsclaw.skill.skill_dag as skill_dag_module

    review_path = tmp_path / "skill_dag_reviews.yaml"
    review_path.write_text("schema_version: 2\nreviews: []\n", encoding="utf-8")
    original_build = skill_dag_module.build_skill_dag

    def mutate_review_during_build(registry, **kwargs):
        review_path.write_text(
            "schema_version: 2\nreviews: []\n# authority changed\n",
            encoding="utf-8",
        )
        return original_build(registry, **kwargs)

    monkeypatch.setattr(
        skill_dag_module,
        "build_skill_dag",
        mutate_review_during_build,
    )

    with pytest.raises(ValueError, match="changed while being read"):
        build_candidate_chain_with_revision(
            _Registry({"a": _skill()}),
            skills_root=tmp_path,
            skills=["a"],
        )


def test_candidate_graph_authority_validates_methods_from_frozen_registry(
    tmp_path: Path,
) -> None:
    info = _skill()
    info["param_hints"] = {"declared": {}}
    registry = _Registry({"a": info})

    with pytest.raises(ValueError, match="not declared in frozen Registry"):
        build_candidate_chain_with_revision(
            registry,
            skills_root=tmp_path,
            skills=["a"],
            method_bindings={"a": "invented"},
        )

    with pytest.raises(ValueError, match="does not expose the unified --method"):
        build_candidate_chain_with_revision(
            registry,
            skills_root=tmp_path,
            skills=["a"],
            method_bindings={"a": "declared"},
        )

    info["allowed_extra_flags"] = {"--method"}
    plan, _revision = build_candidate_chain_with_revision(
        registry,
        skills_root=tmp_path,
        skills=["a"],
        method_bindings={"a": "declared"},
    )
    assert plan["method_bindings"] == {"a": "declared"}


def test_composite_query_returns_provenance_candidate_topo_chain():
    decision = resolve_capability("run sc-preprocessing and then sc-clustering")

    assert decision.coverage == "partial_skill"
    assert decision.candidate_chain["skills"] == ["sc-preprocessing", "sc-clustering"]
    assert decision.candidate_chain["phases"] == [["sc-preprocessing"], ["sc-clustering"]]
    assert decision.candidate_chain["edges"]
    assert all(edge["source"] == "sc-preprocessing" for edge in decision.candidate_chain["edges"])
    assert all(edge["target"] == "sc-clustering" for edge in decision.candidate_chain["edges"])
    assert decision.candidate_chain["resource_ready"] is True
    assert decision.candidate_chain["missing_resource_requests"] == []
    assert set(decision.candidate_chain["resource_requests"]) == {
        "sc-preprocessing",
        "sc-clustering",
    }
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
