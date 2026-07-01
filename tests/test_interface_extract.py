"""Tests for omicsclaw.skill.interface_extract (ADR 0037 interface population)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omicsclaw.skill import interface_extract as ie  # noqa: E402

_OUTPUT_CONTRACT = """## Output Structure

```
output_directory/
├── report.md
└── tables/
    └── qc_summary.csv
```

## File contents

- `tables/qc_summary.csv` — written by `x.py`.
- `figures/umap_leiden.png` — written by `x.py`.
- `commands.sh` — written by `x.py`.
- `environment.txt` — written by `x.py`.
- `processed.h5ad` — written by `x.py`.
- `report.md` — Markdown summary.
- `result.json` — standardised result envelope.

### Demo-only outputs

- `demo_visium.h5ad` — generated only on `--demo`.

## Notes

Auto-generated.
"""

_IO_BODY = """
## When to use

Some prose mentioning obsm["irrelevant"] outside the table.

## Inputs & Outputs

| Input | Format | Required |
|---|---|---|
| Spatial AnnData | `.h5ad` (with `obsm["spatial"]` recommended) | yes |

| Output | Path | Notes |
|---|---|---|
| Processed | `processed.h5ad` | `layers["counts"]`, `obsm["X_pca"]`, `obsm["X_umap"]`, `var["highly_variable"]`, `obs["leiden"]` |

## Flow

steps
"""


def test_output_files_includes_figures_excludes_sidecars_demo():
    files = ie.extract_output_files(_OUTPUT_CONTRACT)
    # figures ARE real produced artifacts → included by default
    assert "figures/umap_leiden.png" in files
    assert "tables/qc_summary.csv" in files
    assert {"processed.h5ad", "report.md", "result.json"} <= set(files)
    # framework reproducibility sidecars + demo-only outputs are excluded
    assert "commands.sh" not in files and "environment.txt" not in files
    assert "demo_visium.h5ad" not in files


def test_output_files_can_exclude_figures():
    files = ie.extract_output_files(_OUTPUT_CONTRACT, include_figures=False)
    assert not any(f.startswith("figures/") for f in files)
    assert "tables/qc_summary.csv" in files


def test_output_files_table_fallback_for_handwritten_contract():
    # A hand-written contract with no "## File contents" bullets — fall back to
    # the first-column filenames of the markdown file table.
    hand = (
        "# skill — Output Contract\n\n## Output directory layout\n\n"
        "```\n<output>/\n├── report.md\n└── audit.json\n```\n\n"
        "| File | Contents |\n|---|---|\n"
        "| `report.md` | human report |\n"
        "| `audit.json` | provenance |\n"
    )
    assert ie.extract_output_files(hand) == ["report.md", "audit.json"]


def test_anndata_uns_and_negation():
    body = (
        "## Inputs & Outputs\n"
        "| Output | Path | Notes |\n|---|---|---|\n"
        '| AnnData | a.h5ad | `uns["numbat_calls"]`, no `obsm["phantom"]`, real `obsm["X_pca"]` |\n'
        "## Flow\n"
    )
    keys = ie.extract_anndata_keys(body)
    assert keys["uns"] == ["numbat_calls"]
    assert "phantom" not in keys["obsm"]      # negated mention skipped
    assert "X_pca" in keys["obsm"]


def test_anndata_removal_verb_negation():
    # A key the skill DELETES before save (e.g. sc-markers 'drops uns[...] to keep
    # file small') is not a produced output. Both keys in the drop-list are skipped,
    # while a produced key in a SEPARATE clause survives.
    body = (
        "## Inputs & Outputs\n"
        "| Output | Path | Notes |\n|---|---|---|\n"
        '| AnnData | a.h5ad | drops `uns["rank_genes_groups"]` / '
        '`uns["rank_genes_groups_filtered"]` to keep file small; '
        'adds `obsm["X_pca"]` |\n'
        "## Flow\n"
    )
    keys = ie.extract_anndata_keys(body)
    assert keys["uns"] == []                 # both dropped keys skipped
    assert keys["obsm"] == ["X_pca"]         # produced key in a later clause survives


def test_input_file_types_h5_and_feather():
    body = (
        "## Inputs & Outputs\n"
        "| Input | Format | Required |\n|---|---|---|\n"
        "| Expression | `.h5ad` / 10x `.h5` / `.loom` | yes |\n"
        "| cisTarget DB | `.feather` glob | optional |\n"
        "\n| Output | Path |\n|---|---|\n| p | `p.h5ad` |\n"
        "## Flow\n"
    )
    # `.h5ad` must NOT be shadowed by the new `h5` token; `.h5` + `.feather` captured.
    assert ie.extract_input_file_types(body) == ["h5ad", "h5", "loom", "feather"]


def test_input_file_types_genomics_exts():
    body = (
        "## Inputs & Outputs\n"
        "| Input | Format | Required |\n|---|---|---|\n"
        "| Contigs | `.fasta` / `.fa` | yes |\n"
        "| Alignment | `.sam` text; binary `.bam` | yes |\n"
        "| Peaks | `.bed` (3/6-col) | yes |\n"
        "\n| Output | Path |\n|---|---|\n| p | `p.txt` |\n"
        "## Flow\n"
    )
    # `.fasta` must win over the shorter `fa`; sam/bam/bed all captured.
    assert ie.extract_input_file_types(body) == ["fasta", "fa", "sam", "bam", "bed"]


def test_input_file_types_aligner_logs():
    # bulkrna-read-alignment accepts STAR `Log.final.out`, HISAT2 `.log`, Salmon `meta_info.json`.
    body = (
        "## Inputs & Outputs\n"
        "| Input | Format | Required |\n|---|---|---|\n"
        "| Aligner log | `Log.final.out` (STAR), `.log` (HISAT2), `meta_info.json` (Salmon) | yes |\n"
        "\n| Output | Path |\n|---|---|\n| p | `p.csv` |\n"
        "## Flow\n"
    )
    assert ie.extract_input_file_types(body) == ["out", "log", "json"]


def test_input_file_types_pdf():
    # literature ingests a local PDF path (parsed via pypdf).
    body = (
        "## Inputs & Outputs\n"
        "| Input | Format |\n|---|---|\n| Paper | `.pdf` document |\n"
        "\n| Output | Path |\n|---|---|\n| x | `x.json` |\n"
        "## Flow\n"
    )
    assert ie.extract_input_file_types(body) == ["pdf"]


def test_anndata_fstring_templated_keys():
    body = (
        "## Inputs & Outputs\n"
        "| Output | Path | Notes |\n|---|---|---|\n"
        '| AnnData | a.h5ad | adds `obsm[f"deconvolution_{method}"]` and `obs[f"local_moran_<gene>"]` |\n'
        "## Flow\n"
    )
    keys = ie.extract_anndata_keys(body)
    assert "deconvolution_{method}" in keys["obsm"]
    assert "local_moran_<gene>" in keys["obs"]


def test_anndata_keys_are_produced_only():
    keys = ie.extract_anndata_keys(_IO_BODY)
    assert keys == {
        "obs": ["leiden"],
        "obsm": ["X_pca", "X_umap"],
        "var": ["highly_variable"],
        "layers": ["counts"],
        "uns": [],
    }
    # the INPUT obsm["spatial"] must NOT leak into produced outputs
    assert "spatial" not in keys["obsm"]
    # prose outside the I&O section must not leak in
    assert "irrelevant" not in keys["obsm"]


def test_input_anndata_obsm():
    assert ie.extract_input_anndata_obsm(_IO_BODY) == ["spatial"]


def test_input_file_types():
    assert ie.extract_input_file_types(_IO_BODY) == ["h5ad"]


def test_modalities_are_tags_in_vocab():
    assert ie.extract_modalities(["spatial", "visium", "xenium", "qc", "leiden"]) == ["visium", "xenium"]
    assert ie.extract_modalities(["preprocessing", "qc"]) == []


def test_extractors_tolerate_missing_io_section():
    body = "## When to use\nx\n## Flow\ny\n"
    assert ie.extract_anndata_keys(body) == {"obs": [], "obsm": [], "var": [], "layers": [], "uns": []}
    assert ie.extract_input_file_types(body) == []
    assert ie.extract_input_anndata_obsm(body) == []
