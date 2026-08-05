"""Deterministic BiomniBench-DA 12-2 preflight for bulkrna-enrichment."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


_DISCLAIMER = (
    "OmicsClaw is a research and educational tool for multi-omics analysis. "
    "It is not a medical device and does not provide clinical diagnoses. "
    "Consult a domain expert before making decisions based on these results."
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


def _extract_shared_genes(path: Path) -> tuple[pd.DataFrame, list[str]]:
    table = pd.read_excel(path, sheet_name="Sheet1", header=1)
    gene_column = "Gene name"
    overlap_column = "Genes overlapped with DEGs of KD groups"
    selected = table.loc[
        table[overlap_column].astype(str).str.strip().str.lower().eq("v"),
        gene_column,
    ]
    genes = selected.dropna().astype(str).str.strip()
    genes = genes[genes.ne("")].drop_duplicates().tolist()
    return table, genes


def _read_gmt(path: Path) -> dict[str, list[str]]:
    gene_sets: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            gene_sets[fields[0]] = list(dict.fromkeys(fields[2:]))
    return gene_sets


def _write_trace(
    path: Path,
    *,
    source_shape: tuple[int, int],
    query_size: int,
    background_size: int,
    query_in_background: int,
    pathway_count: int,
    tested_count: int,
    g2m: pd.Series,
    g2m_rank: int,
    top: pd.DataFrame,
) -> None:
    top_rows = [
        f"| {row.term} | {row.overlap} | {row.pvalue:.6g} | {row.padj:.6g} |"
        for row in top.itertuples()
    ]
    trace = f"""# Objective

Determine whether the MSigDB Hallmark G2M checkpoint is over-represented among
the genes explicitly marked as shared between the overexpression and knockdown
experiments, using FDR < 0.05 as the decision rule.

# Data Sources

- `TS7.xlsx`, `Sheet1`: {source_shape[0]} data rows x {source_shape[1]} columns
  after reading row 2 as the header. The query uses `Gene name` only where
  `Genes overlapped with DEGs of KD groups` equals `v`; missing/blank values are
  removed and symbols are deduplicated.
- `GSEA_gmt.gmt`: {pathway_count} MSigDB Hallmark definitions. GSEApy returns
  {tested_count} pathways with at least one usable overlap.

# Approach

## Step 1: extract the shared DEG query

**Decision and rationale**: The overlap flag in the overexpression half of TS7
directly encodes membership in the knockdown DEG set. This avoids combining the
two mirrored halves in a way that could duplicate symbols.

```python
table = pd.read_excel("TS7.xlsx", sheet_name="Sheet1", header=1)
shared = table.loc[
    table["Genes overlapped with DEGs of KD groups"]
        .astype(str).str.strip().str.lower().eq("v"),
    "Gene name",
]
shared = shared.dropna().astype(str).str.strip()
shared = shared[shared.ne("")].drop_duplicates()
```

**Quantitative result**: {query_size} unique shared genes.

The Skill's DE-table Interface requires effect and significance columns even
though this task supplies a membership list. The adapter therefore writes
constant `log2FoldChange=2`, `pvalue=1e-12`, and `padj=1e-10` values so all
{query_size} selected symbols enter ORA. These values are membership sentinels;
they are not TS7 effect sizes or p-values and are never interpreted quantitatively.

## Step 2: run Hallmark over-representation analysis

**Decision and rationale**: The supplied GMT is parsed locally and passed as a
custom gene-set dictionary to GSEApy `enrichr`. This performs the suite's
hypergeometric/Fisher-style ORA and Benjamini-Hochberg adjustment without
substituting an online or R-side pathway database.

```python
gene_sets = {{}}
for line in open("GSEA_gmt.gmt"):
    name, _description, *genes = line.rstrip("\\n").split("\\t")
    gene_sets[name] = list(dict.fromkeys(genes))
background = sorted({{gene for genes in gene_sets.values() for gene in genes}})
enrichment = gseapy.enrichr(
    gene_list=shared.tolist(), gene_sets=gene_sets,
    organism="human", background=background, outdir=None, no_plot=True,
).results
enrichment = enrichment.sort_values("Adjusted P-value")
```

**Quantitative result**: {tested_count} pathways were returned and ranked by FDR.
The explicit ORA universe is the {background_size}-gene union of the supplied
Hallmark definitions; {query_in_background} of {query_size} query symbols occur
in that universe. The hypergeometric counts therefore use `M={background_size}`
and `N={query_in_background}` rather than an implicit library default.

## Step 3: evaluate G2M and the global result

| Pathway | Overlap | Raw p-value | FDR |
| --- | ---: | ---: | ---: |
{chr(10).join(top_rows)}

G2M checkpoint is rank {g2m_rank}, with overlap `{g2m['overlap']}`, raw
p-value `{float(g2m['pvalue']):.8g}`, and FDR `{float(g2m['padj']):.8g}`.

# Results

The G2M checkpoint is significantly enriched because its FDR is below 0.05.
The strongest signals are mitotic spindle, UV response down, and G2M checkpoint,
which is consistent with a shared proliferation/cell-cycle transcriptional
component. This is an association in the supplied in-vitro DEG lists; it does
not establish that G2M activity causes the perturbation response.

# References

- Liberzon A. et al. 2015. *The Molecular Signatures Database Hallmark Gene Set
  Collection*. Cell Systems 1(6):417-425. DOI: 10.1016/j.cels.2015.12.004.
- Numerical values and identifiers above are derived from the supplied
  `TS7.xlsx` and `GSEA_gmt.gmt` files and the recorded transformation.

# Disclaimer

{_DISCLAIMER}
"""
    path.write_text(trace, encoding="utf-8")


def main() -> int:
    dataset = Path(os.environ["OMICSCLAW_EVALUATION_DATASET"]).resolve()
    output = Path(os.environ["OMICSCLAW_EVALUATION_OUTPUT"]).resolve()
    result_path = Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).resolve()
    excel_path = dataset / "environment" / "data" / "TS7.xlsx"
    gmt_path = dataset / "environment" / "data" / "GSEA_gmt.gmt"
    rubric_path = dataset / "tests" / "rubric.txt"
    try:
        source_table, query_genes = _extract_shared_genes(excel_path)
        gene_sets = _read_gmt(gmt_path)
        rubric = rubric_path.read_text(encoding="utf-8")
        if "G2M Checkpoint Quantitative Results" not in rubric:
            raise ValueError("unexpected rubric")
    except (OSError, KeyError, TypeError, ValueError):
        _write_result(result_path, outcome="failed", reason_code="dataset_invalid")
        return 1

    de_path = output / "shared_degs.csv"
    gene_sets_path = output / "hallmark_gene_sets.json"
    pd.DataFrame(
        {
            "gene": query_genes,
            "log2FoldChange": np.full(len(query_genes), 2.0),
            "pvalue": np.full(len(query_genes), 1e-12),
            "padj": np.full(len(query_genes), 1e-10),
        }
    ).to_csv(de_path, index=False)
    gene_sets_path.write_text(json.dumps(gene_sets, sort_keys=True), encoding="utf-8")

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
            "bulkrna-enrichment",
            input_path=str(de_path),
            output_dir=str(output / "skill-run"),
            extra_args=[
                "--method",
                "ora",
                "--gene-set-file",
                str(gene_sets_path),
                "--padj-cutoff",
                "0.05",
                "--lfc-cutoff",
                "1.0",
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

    run_output = output / "skill-run"
    try:
        skill_result = json.loads((run_output / "result.json").read_text())
        params = skill_result["data"]["params"]
        method_used = skill_result["data"]["method_used"]
        background_size = int(skill_result["data"]["background_genes"])
        query_in_background = int(
            skill_result["data"]["query_genes_in_background"]
        )
        results = pd.read_csv(run_output / "tables" / "enrichment_results.csv")
        results = results.sort_values(["padj", "pvalue", "term"]).reset_index(drop=True)
        g2m = results.loc[results["term"].eq("HALLMARK_G2M_CHECKPOINT")].iloc[0]
        g2m_rank = int(results.index[results["term"].eq("HALLMARK_G2M_CHECKPOINT")][0]) + 1
    except (OSError, IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _write_result(
            result_path,
            outcome="failed",
            reason_code="semantic_contract_failed",
            skill_revision=observed_revision,
        )
        return 1

    checks = {
        "query_gene_count": len(query_genes) == 1543,
        "method_provenance": (
            params["method"] == "ora"
            and Path(params["gene_set_file"]).name == gene_sets_path.name
            and method_used == "ora_gseapy"
        ),
        "pathways_tested": len(results) == 49,
        "background_gene_count": background_size == 4384,
        "query_in_background": query_in_background == 400,
        "g2m_overlap": str(g2m["overlap"]) == "37/200",
        "g2m_pvalue": bool(
            np.isclose(float(g2m["pvalue"]), 1.689556e-5, rtol=0.02)
        ),
        "g2m_fdr": bool(np.isclose(float(g2m["padj"]), 2.759608e-4, rtol=0.02)),
        "g2m_rank": g2m_rank == 3,
        "g2m_significant": float(g2m["padj"]) < 0.05,
    }
    passed = all(checks.values())
    top = results.head(10)
    _write_trace(
        output / "trace.md",
        source_shape=source_table.shape,
        query_size=len(query_genes),
        background_size=background_size,
        query_in_background=query_in_background,
        pathway_count=len(gene_sets),
        tested_count=len(results),
        g2m=g2m,
        g2m_rank=g2m_rank,
        top=top,
    )
    answer = (
        "Yes. HALLMARK_G2M_CHECKPOINT is significantly enriched among the "
        f"{len(query_genes)} shared DEGs: overlap {g2m['overlap']}, raw p-value "
        f"{float(g2m['pvalue']):.8g}, Benjamini-Hochberg FDR "
        f"{float(g2m['padj']):.8g}, rank {g2m_rank} of {len(results)} returned "
        "Hallmark pathways. The FDR is below 0.05."
    )
    (output / "answer.txt").write_text(answer + "\n", encoding="utf-8")

    metrics = {
        "checks_passed": float(sum(checks.values())),
        "checks_total": float(len(checks)),
        "query_genes": float(len(query_genes)),
        "pathways_tested": float(len(results)),
        "background_genes": float(background_size),
        "query_genes_in_background": float(query_in_background),
        "significant_pathways": float(results["padj"].lt(0.05).sum()),
        "g2m_overlap_genes": 37.0,
        "g2m_pathway_size": 200.0,
        "g2m_pvalue": float(g2m["pvalue"]),
        "g2m_fdr": float(g2m["padj"]),
        "g2m_rank": float(g2m_rank),
    }
    report = {
        "task_id": "da-12-2",
        "status": "deterministic_preflight_only",
        "official_llm_judge_score": None,
        "passed": passed,
        "checks": checks,
        "metrics": metrics,
        "top_pathways": top[
            ["term", "overlap", "pvalue", "padj"]
        ].to_dict(orient="records"),
    }
    report_bytes = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
    (output / "biomnibench_report.json").write_bytes(report_bytes)
    evidence_refs = [
        "sha256:" + hashlib.sha256(report_bytes).hexdigest(),
        "sha256:" + hashlib.sha256((output / "trace.md").read_bytes()).hexdigest(),
        "sha256:" + hashlib.sha256((output / "answer.txt").read_bytes()).hexdigest(),
    ]
    _write_result(
        result_path,
        outcome="succeeded" if passed else "failed",
        reason_code="none" if passed else "semantic_contract_failed",
        metrics=metrics,
        skill_revision=observed_revision,
        evidence_refs=evidence_refs,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
