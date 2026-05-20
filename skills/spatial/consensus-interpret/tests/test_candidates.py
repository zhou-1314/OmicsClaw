"""Slice 4 — Marker → cell-type candidate ranking (deterministic, pre-LLM).

This stage bounds LLM hallucination at the INPUT level by constraining
the cell-type vocabulary the LLM may pick from to candidates whose
markers appear both in the cluster's DE top-K and the marker DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _make_de(rows: list[tuple[int, int, str]]) -> pd.DataFrame:
    """rows: list of (cluster, rank, gene); pads score/pval_adj for schema."""
    return pd.DataFrame(
        [{"cluster": c, "rank": r, "gene": g, "score": 10.0 / r, "pval_adj": 0.01} for c, r, g in rows]
    )


def _stub_db(by_gene: dict[str, list[tuple[str, float]]]) -> object:
    """Minimal MarkerDB stub: gene -> [(cell_type, weight)] -> Candidate-like list."""
    from _marker_db import Candidate  # type: ignore[import-not-found]

    class _Stub:
        def candidates(self, gene: str):
            return [
                Candidate(cell_type=ct, weight=w, source="stub", species="m", tissue="t")
                for ct, w in by_gene.get(gene, ())
            ]

    return _Stub()


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #

def test_candidates_rank_by_weighted_inverse_rank() -> None:
    """score(cell_type, cluster) = Σ db.weight * 1/de_rank over matching markers."""
    from _candidates import rank_celltype_candidates  # type: ignore[import-not-found]

    de_df = _make_de([
        (0, 1, "Aqp4"),       # weight 0.9, rank 1 -> 0.9
        (0, 2, "Gfap"),       # weight 0.9, rank 2 -> 0.45
        (0, 3, "Mbp"),        # weight 0.95, rank 3 -> 0.317
    ])
    db = _stub_db({
        "Aqp4": [("Astrocyte", 0.9)],
        "Gfap": [("Astrocyte", 0.9)],
        "Mbp":  [("Oligodendrocyte", 0.95)],
    })

    ranked = rank_celltype_candidates(de_df, db, top_k=5)
    cluster_0 = ranked[0]
    assert len(cluster_0) >= 2
    assert cluster_0[0].cell_type == "Astrocyte"
    assert cluster_0[0].score == pytest.approx(0.9 + 0.9 / 2, abs=1e-3)
    assert cluster_0[1].cell_type == "Oligodendrocyte"


def test_candidates_supporting_markers_recorded() -> None:
    """Each RankedCandidate carries the (gene, de_rank, weight) triples
    used so Slice 5 LLM prompt can cite verbatim evidence."""
    from _candidates import rank_celltype_candidates  # type: ignore[import-not-found]

    de_df = _make_de([(0, 1, "Aqp4"), (0, 5, "Gfap")])
    db = _stub_db({"Aqp4": [("Astrocyte", 0.9)], "Gfap": [("Astrocyte", 0.85)]})

    ranked = rank_celltype_candidates(de_df, db, top_k=3)
    astro = ranked[0][0]
    assert astro.cell_type == "Astrocyte"
    genes_used = {m.gene for m in astro.supporting_markers}
    assert genes_used == {"Aqp4", "Gfap"}
    # Order should be by de_rank ascending
    assert [m.gene for m in astro.supporting_markers] == ["Aqp4", "Gfap"]
    assert astro.supporting_markers[0].de_rank == 1
    assert astro.supporting_markers[0].weight == 0.9


def test_candidates_per_cluster_isolated() -> None:
    """Markers in cluster 0 must not influence cluster 1's ranking."""
    from _candidates import rank_celltype_candidates  # type: ignore[import-not-found]

    de_df = _make_de([
        (0, 1, "Aqp4"),    # cluster 0 -> Astrocyte
        (1, 1, "Mbp"),     # cluster 1 -> Oligo
    ])
    db = _stub_db({
        "Aqp4": [("Astrocyte", 0.9)],
        "Mbp":  [("Oligodendrocyte", 0.95)],
    })

    ranked = rank_celltype_candidates(de_df, db, top_k=3)
    assert ranked[0][0].cell_type == "Astrocyte"
    assert ranked[1][0].cell_type == "Oligodendrocyte"
    # Cluster 0 must not contain Oligo
    assert all(c.cell_type != "Oligodendrocyte" for c in ranked[0])


def test_candidates_top_k_caps_output() -> None:
    from _candidates import rank_celltype_candidates  # type: ignore[import-not-found]

    de_df = _make_de([(0, r, f"g{r}") for r in range(1, 11)])
    db = _stub_db({f"g{r}": [(f"CellType{r}", 0.5)] for r in range(1, 11)})

    ranked = rank_celltype_candidates(de_df, db, top_k=3)
    assert len(ranked[0]) == 3


# --------------------------------------------------------------------------- #
# Tie-break + edge cases                                                      #
# --------------------------------------------------------------------------- #

def test_candidates_alphabetical_tiebreak() -> None:
    """Ties in score broken by alphabetical cell_type (deterministic)."""
    from _candidates import rank_celltype_candidates  # type: ignore[import-not-found]

    # Two cell types with identical score
    de_df = _make_de([(0, 1, "GeneA"), (0, 1, "GeneB")])
    de_df.loc[1, "rank"] = 2  # GeneB at rank 2 for the same cluster
    # Both markers map to different cells with identical weight*rank product:
    # GeneA at rank 1 with weight 0.5 -> ZetaCell  score 0.5
    # GeneB at rank 2 with weight 1.0 -> AlphaCell score 0.5
    db = _stub_db({
        "GeneA": [("ZetaCell", 0.5)],
        "GeneB": [("AlphaCell", 1.0)],
    })

    ranked = rank_celltype_candidates(de_df, db, top_k=5)
    cells = [c.cell_type for c in ranked[0]]
    # Both have score 0.5; tied → alphabetical → AlphaCell first
    assert cells.index("AlphaCell") < cells.index("ZetaCell")


def test_candidates_no_markers_in_db_returns_empty() -> None:
    """A cluster whose top markers all miss the DB gets an empty list
    (T2 signal — handled in Slice 8 coverage check)."""
    from _candidates import rank_celltype_candidates  # type: ignore[import-not-found]

    de_df = _make_de([(0, 1, "Ghost1"), (0, 2, "Ghost2")])
    db = _stub_db({})  # empty

    ranked = rank_celltype_candidates(de_df, db, top_k=5)
    assert ranked[0] == []


def test_candidates_empty_de_returns_empty_dict() -> None:
    from _candidates import rank_celltype_candidates  # type: ignore[import-not-found]

    de_df = pd.DataFrame(columns=["cluster", "rank", "gene", "score", "pval_adj"])
    db = _stub_db({"Aqp4": [("Astrocyte", 0.9)]})

    ranked = rank_celltype_candidates(de_df, db, top_k=5)
    assert ranked == {}
