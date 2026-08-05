"""scAgentBench PAGA adapter for governed sc-pseudotime evaluation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np


_INPUT = (
    "datasets_for_bioagent_benchmark/agent_benchmark/main/paga/paul15.h5"
)
_GOLD = (
    "datasets_for_bioagent_benchmark/groundtruth_result/main/paga.csv"
)
_PROMPTS = (
    "datasets_for_bioagent_benchmark/input_prompt/main/prompt_gradient.json"
)


def _write_result(
    result_path: Path,
    *,
    outcome: str,
    reason_code: str,
    metrics: dict[str, float] | None = None,
    skill_revision: dict[str, str] | None = None,
    evidence_refs: list[str] | None = None,
) -> None:
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outcome": outcome,
                "reason_code": reason_code,
                "metrics": metrics or {},
                "skill_revision": skill_revision or {},
                "evidence_refs": evidence_refs or [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _revision_from_result(result) -> dict[str, str]:
    identity = getattr(result, "audit_identity", None)
    if identity is None:
        return {}
    return {
        "skill_id": identity.skill_id,
        "version": identity.skill_version,
        "manifest_hash": identity.skill_hash,
        "source_hash": identity.source_hash,
    }


def _load_legacy_paul15(path: Path):
    """Load the suite's historical Scanpy HDF5 layout without downloading data."""
    import anndata as ad
    import h5py

    with h5py.File(path, "r") as handle:
        matrix = handle["data.debatched"][()].astype(np.float32)
        gene_names = handle["data.debatched_rownames"][()].astype(str)
        cell_names = handle["data.debatched_colnames"][()].astype(str)
        clusters = handle["cluster.id"][()].flatten().astype(int)
        informative_genes = handle["info.genes_strings"][()].astype(str)

    adata = ad.AnnData(matrix.T)
    adata.var_names = gene_names
    adata.obs_names = cell_names
    cell_types = 6 * ["Ery"]
    cell_types += "MEP Mk GMP GMP DC Baso Baso Mo Mo Neu Neu Eos Lymph".split()
    adata.obs["paul15_clusters"] = [
        f"{cluster}{cell_types[cluster - 1]}" for cluster in clusters
    ]
    adata.obs["paul15_clusters"] = adata.obs["paul15_clusters"].astype("category")
    adata.var_names = [name.split(";")[0] for name in adata.var_names]
    informative_genes = np.intersect1d(informative_genes, adata.var_names)
    adata = adata[:, informative_genes].copy()
    adata.uns["iroot"] = 840
    return adata


def _prepare_input(source: Path, destination: Path) -> None:
    import scanpy as sc

    from skills.singlecell._lib.adata_utils import (
        record_matrix_contract,
        record_standardized_input_contract,
    )

    adata = _load_legacy_paul15(source)
    sc.pp.recipe_zheng17(adata)
    sc.tl.pca(adata, svd_solver="arpack")
    sc.pp.neighbors(adata, n_neighbors=4, n_pcs=20)
    sc.tl.diffmap(adata)
    sc.pp.neighbors(adata, n_neighbors=10, use_rep="X_diffmap")
    sc.tl.louvain(adata, resolution=1.0, random_state=0)
    record_standardized_input_contract(
        adata,
        expression_source="scagentbench:paul15/data.debatched",
        gene_name_source="data.debatched_rownames",
        standardizer_skill="scagentbench-paga-adapter",
    )
    record_matrix_contract(
        adata,
        x_kind="normalized_expression",
        producer_skill="scagentbench-paga-adapter",
        preprocess_method="scanpy_recipe_zheng17",
        primary_cluster_key="louvain",
    )
    adata.write_h5ad(destination)


def _aligned_cosine(gold: np.ndarray, observed: np.ndarray) -> float:
    scores: list[float] = []
    for gold_row, observed_row in zip(gold, observed, strict=True):
        gold_norm = float(np.linalg.norm(gold_row))
        observed_norm = float(np.linalg.norm(observed_row))
        if gold_norm == 0.0 and observed_norm == 0.0:
            scores.append(1.0)
        elif gold_norm == 0.0 or observed_norm == 0.0:
            scores.append(0.0)
        else:
            scores.append(
                float(np.dot(gold_row, observed_row) / (gold_norm * observed_norm))
            )
    return float(np.mean(scores))


def _edge_f1(gold: np.ndarray, observed: np.ndarray, threshold: float = 0.03) -> float:
    upper = np.triu_indices(gold.shape[0], k=1)
    gold_edges = gold[upper] >= threshold
    observed_edges = observed[upper] >= threshold
    true_positive = int(np.count_nonzero(gold_edges & observed_edges))
    false_positive = int(np.count_nonzero(~gold_edges & observed_edges))
    false_negative = int(np.count_nonzero(gold_edges & ~observed_edges))
    denominator = 2 * true_positive + false_positive + false_negative
    return 1.0 if denominator == 0 else (2.0 * true_positive) / denominator


def _native_pairwise_mean(gold: np.ndarray, observed: np.ndarray) -> float:
    """Reproduce scAgentBench's published all-row-pairs cosine calculation."""
    from sklearn.metrics.pairwise import cosine_similarity

    score = float(cosine_similarity(gold[1:], observed[1:]).mean())
    return max(score, 0.0)


def main() -> int:
    dataset = Path(os.environ["OMICSCLAW_EVALUATION_DATASET"]).resolve()
    output = Path(os.environ["OMICSCLAW_EVALUATION_OUTPUT"]).resolve()
    result_path = Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).resolve()
    source = dataset / _INPUT
    gold_path = dataset / _GOLD
    prompt_path = dataset / _PROMPTS
    try:
        prompts = json.loads(prompt_path.read_text(encoding="utf-8"))
        prompt = prompts["paga"]["prompt_input"]["advanced"]
        if "PAGA" not in prompt or not source.is_file() or not gold_path.is_file():
            raise ValueError("case files do not match the PAGA task")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _write_result(result_path, outcome="failed", reason_code="dataset_invalid")
        return 1

    staged_input = output / "paul15_preprocessed.h5ad"
    run_output = output / "skill-run"
    try:
        _prepare_input(source, staged_input)
    except (OSError, KeyError, TypeError, ValueError):
        _write_result(result_path, outcome="failed", reason_code="dataset_invalid")
        return 1

    hidden_environment: dict[str, str] = {}
    for name in (
        "OMICSCLAW_EVALUATION_DATASET",
        "OMICSCLAW_EVALUATION_OUTPUT",
        "OMICSCLAW_EVALUATION_RESULT",
        "OMICSCLAW_EVALUATION_SKILL_REVISION",
    ):
        if name in os.environ:
            hidden_environment[name] = os.environ.pop(name)
    try:
        from omicsclaw.skill.runner import run_skill

        run_result = run_skill(
            "sc-pseudotime",
            input_path=str(staged_input),
            output_dir=str(run_output),
            extra_args=[
                "--method",
                "dpt",
                "--cluster-key",
                "louvain",
                "--use-rep",
                "X_diffmap",
                "--n-neighbors",
                "10",
                "--n-pcs",
                "20",
                "--n-dcs",
                "10",
                "--n-genes",
                "10",
                "--root-cell",
                "840",
            ],
        )
    finally:
        os.environ.update(hidden_environment)

    observed_revision = _revision_from_result(run_result)
    try:
        expected_revision = json.loads(
            os.environ["OMICSCLAW_EVALUATION_SKILL_REVISION"]
        )
    except (KeyError, TypeError, json.JSONDecodeError):
        expected_revision = {}
    if observed_revision != expected_revision:
        _write_result(
            result_path,
            outcome="failed",
            reason_code="revision_mismatch",
            skill_revision=observed_revision,
        )
        return 1
    if not run_result.success:
        _write_result(
            result_path,
            outcome="failed",
            reason_code="skill_failed",
            skill_revision=observed_revision,
        )
        return 1

    processed = run_output / "processed.h5ad"
    try:
        import anndata as ad
        from scipy import sparse

        skill_result = json.loads((run_output / "result.json").read_text())
        params = skill_result["data"]["params"]
        parameter_contract_holds = (
            params["method"] == "dpt"
            and params["cluster_key"] == "louvain"
            and params["use_rep"] == "X_diffmap"
            and str(params["root_cell"]) == "840"
        )
        adata = ad.read_h5ad(processed, backed="r")
        try:
            connectivities = adata.uns["paga"]["connectivities"]
            observed = (
                connectivities.toarray()
                if sparse.issparse(connectivities)
                else np.asarray(connectivities)
            ).astype(float)
        finally:
            adata.file.close()
        gold = np.loadtxt(gold_path, delimiter=",").astype(float)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _write_result(
            result_path,
            outcome="failed",
            reason_code="semantic_contract_failed",
            skill_revision=observed_revision,
        )
        return 1

    shape_matches = observed.shape == gold.shape
    finite = bool(np.isfinite(observed).all())
    symmetric = shape_matches and bool(np.allclose(observed, observed.T, atol=1e-8))
    zero_diagonal = shape_matches and bool(
        np.allclose(np.diag(observed), 0.0, atol=1e-8)
    )
    if shape_matches and finite:
        aligned_cosine = _aligned_cosine(gold, observed)
        edge_f1 = _edge_f1(gold, observed)
        native_score = _native_pairwise_mean(gold, observed)
        denominator = float(np.linalg.norm(gold))
        relative_error = float(np.linalg.norm(gold - observed) / denominator)
    else:
        aligned_cosine = 0.0
        edge_f1 = 0.0
        native_score = 0.0
        relative_error = 1.0

    passed = (
        parameter_contract_holds
        and shape_matches
        and finite
        and symmetric
        and zero_diagonal
        and aligned_cosine >= 0.95
        and edge_f1 >= 0.90
    )
    metrics = {
        "native_pairwise_mean": native_score,
        "aligned_cosine": aligned_cosine,
        "edge_f1": edge_f1,
        "relative_frobenius_error": relative_error,
        "clusters": float(observed.shape[0]),
    }
    report = {
        "task_id": "paga",
        "passed": passed,
        "thresholds": {"aligned_cosine": 0.95, "edge_f1": 0.90},
        "checks": {
            "parameter_contract": parameter_contract_holds,
            "shape_matches": shape_matches,
            "finite": finite,
            "symmetric": symmetric,
            "zero_diagonal": zero_diagonal,
        },
        "metrics": metrics,
        "native_metric_warning": (
            "scAgentBench averages all row-pair cosines; gold compared with itself "
            "is not 1.0, so it is retained for comparability but not used alone as "
            "the conformance gate."
        ),
    }
    report_bytes = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
    (output / "scagentbench_report.json").write_bytes(report_bytes)
    np.savetxt(output / "paga.csv", observed, delimiter=",")
    report_digest = "sha256:" + hashlib.sha256(report_bytes).hexdigest()
    _write_result(
        result_path,
        outcome="succeeded" if passed else "failed",
        reason_code="none" if passed else "semantic_contract_failed",
        metrics=metrics,
        skill_revision=observed_revision,
        evidence_refs=[report_digest],
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
