# Bundled marker databases — `consensus-interpret`

Per ADR 0012, `consensus-interpret` ships **vendored** marker tables so
the skill is self-contained and reproducible offline. Users can override
any of these with `--markers <path.tsv>` (same schema).

## Files

| File | Tissue keys | Source | License | Approx size |
|---|---|---|---|---|
| `panglaodb_brain.tsv`   | brain, neural, hippocampus, cortex | PanglaoDB 2020-03 release | CC-BY-4.0 | ~600 genes / ~50 cell types |
| `panglaodb_immune.tsv`  | immune, blood, lymph, spleen        | PanglaoDB 2020-03 release | CC-BY-4.0 | ~400 / ~30 |
| `panglaodb_kidney.tsv`  | kidney, renal, nephron              | PanglaoDB 2020-03 release | CC-BY-4.0 | ~300 / ~20 |
| `cellmarker_liver.tsv`  | liver, hepatic                       | CellMarker 2.0           | CC0       | ~250 / ~15 |

## Schema

Tab-separated. **No header skipping** — line 1 is column names:

```
gene	cell_type	source	species	tissue	weight
Aqp4	Astrocyte	panglaodb_brain	mouse	brain	0.9
Pvrl3	CA1 pyramidal neuron	panglaodb_brain	mouse	brain	0.85
...
```

| Column | Type | Notes |
|---|---|---|
| `gene` | str | symbol; case-sensitive; we do NOT canonicalize at load time |
| `cell_type` | str | unique-ish name from source DB; collisions across sources resolved by `source` prefix |
| `source` | str | which DB this row came from (e.g. `panglaodb_brain`); appears in `evidence.markers[].db_source` |
| `species` | str | `mouse` / `human` / both rows duplicated |
| `tissue` | str | the tissue key (also encoded in filename) |
| `weight` | float in [0, 1] | source-DB confidence; PanglaoDB ubiquitousness index; CellMarker fixed at 1.0 unless flagged |

## Acquisition (when curating fresh data)

```bash
# PanglaoDB markers
curl -sLO https://panglaodb.se/markers/PanglaoDB_markers_27_Mar_2020.tsv.gz
gunzip PanglaoDB_markers_27_Mar_2020.tsv.gz

# Filter per tissue + reshape to our schema (one row per gene × cell_type)
python <<'PY'
import pandas as pd
src = pd.read_csv("PanglaoDB_markers_27_Mar_2020.tsv", sep="\t")
for tissue in ("brain", "immune", "kidney"):
    sub = src[src["organ"].str.lower().str.contains(tissue)]
    out = pd.DataFrame({
        "gene": sub["official gene symbol"],
        "cell_type": sub["cell type"],
        "source": f"panglaodb_{tissue}",
        "species": sub["species"].str.lower().str.strip(),  # "Mm" -> "mouse", etc.
        "tissue": tissue,
        "weight": sub["ubiquitousness index"].astype(float),
    })
    out.to_csv(f"panglaodb_{tissue}.tsv", sep="\t", index=False)
PY

# CellMarker 2.0
curl -sLO http://yikedaxue.slwshop.cn/CellMarker_download_files/file/Cell_marker_All.xlsx
# Filter to liver, reshape similarly.
```

## License attribution (required for redistribution)

- **PanglaoDB**: Franzén et al., *Database* 2019; bibcite + CC-BY-4.0
  notice in any published derivative (paper / supplement).
- **CellMarker 2.0**: Hu et al., *Nucleic Acids Research* 2023; CC0
  but bibcite recommended.

Both licenses are compatible with the Apache-2.0 license under which
this skill is shipped. Do **not** drop these notices when redistributing
OmicsClaw.

## Implementation note (scaffold stage)

The four TSV files in this directory are **scaffold placeholders** at
the time of this skill's initial commit — they contain header + a
small set of textbook-canonical markers so `consensus-interpret` tests
can run without external data fetches. Before declaring the skill
publish-ready, run the acquisition pipeline above to expand each file
to the sizes documented in the table.

A full-curation pass is tracked separately and is **not** a blocker
for shipping the skill scaffold + invariant tests.
