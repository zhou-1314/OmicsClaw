"""Slice 2 — Marker DB loader.

Tests the bundled TSV loaders + user override + malformed-row tolerance.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


# --------------------------------------------------------------------------- #
# Bundled lookup                                                              #
# --------------------------------------------------------------------------- #

def test_marker_db_loads_bundled_brain() -> None:
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    db = MarkerDB.load(tissue="brain")
    # Sanity: at least Astrocyte / Microglia / Oligo entries
    assert len(db) > 0
    candidates = db.candidates("Aqp4")
    assert any(c.cell_type == "Astrocyte" for c in candidates)


def test_marker_db_loads_bundled_immune() -> None:
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    db = MarkerDB.load(tissue="immune")
    candidates = db.candidates("Cd3d")
    assert any("T cell" in c.cell_type for c in candidates)


def test_marker_db_loads_bundled_kidney() -> None:
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    db = MarkerDB.load(tissue="kidney")
    candidates = db.candidates("Nphs1")
    assert any("Podocyte" in c.cell_type for c in candidates)


def test_marker_db_loads_bundled_liver() -> None:
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    db = MarkerDB.load(tissue="liver")
    candidates = db.candidates("Alb")
    assert any("Hepatocyte" in c.cell_type for c in candidates)


# --------------------------------------------------------------------------- #
# Failure modes                                                               #
# --------------------------------------------------------------------------- #

def test_marker_db_unknown_tissue_no_override_raises() -> None:
    from _errors import MarkerDBUnavailableError  # type: ignore[import-not-found]
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    with pytest.raises(MarkerDBUnavailableError, match="lung|tissue|--markers"):
        MarkerDB.load(tissue="lung")


def test_marker_db_no_tissue_no_override_raises() -> None:
    from _errors import MarkerDBUnavailableError  # type: ignore[import-not-found]
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    with pytest.raises(MarkerDBUnavailableError, match="tissue|--markers"):
        MarkerDB.load()


def test_marker_db_user_override_supersedes_tissue(tmp_path: Path) -> None:
    """--markers <path.tsv> takes precedence even if --tissue is set."""
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    custom = tmp_path / "custom.tsv"
    custom.write_text(
        "gene\tcell_type\tsource\tspecies\ttissue\tweight\n"
        "FakeGene42\tImaginaryCell\tcustom_test\tmouse\tunknown\t0.99\n"
    )
    db = MarkerDB.load(tissue="brain", override_path=custom)  # override wins
    candidates = db.candidates("FakeGene42")
    assert len(candidates) == 1
    assert candidates[0].cell_type == "ImaginaryCell"
    # The bundled brain DB's Aqp4 should NOT appear when overridden
    assert db.candidates("Aqp4") == []


def test_marker_db_override_only_no_tissue(tmp_path: Path) -> None:
    """--markers alone (no --tissue) is a valid invocation."""
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    custom = tmp_path / "custom.tsv"
    custom.write_text(
        "gene\tcell_type\tsource\tspecies\ttissue\tweight\n"
        "Gene1\tCellA\tcustom\tmouse\tunknown\t0.8\n"
    )
    db = MarkerDB.load(override_path=custom)
    assert db.candidates("Gene1")[0].cell_type == "CellA"


# --------------------------------------------------------------------------- #
# Schema robustness                                                           #
# --------------------------------------------------------------------------- #

def test_marker_db_missing_required_columns_raises(tmp_path: Path) -> None:
    """Missing 'gene' or 'cell_type' header → MarkerDBUnavailableError."""
    from _errors import MarkerDBUnavailableError  # type: ignore[import-not-found]
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    bad = tmp_path / "bad.tsv"
    bad.write_text("foo\tbar\n1\t2\n")  # No gene / cell_type columns
    with pytest.raises(MarkerDBUnavailableError, match="schema|gene|cell_type|column"):
        MarkerDB.load(override_path=bad)


def test_marker_db_skips_malformed_rows(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Rows missing required cells are skipped with a warning, not fatal."""
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    f = tmp_path / "partial.tsv"
    f.write_text(
        "gene\tcell_type\tsource\tspecies\ttissue\tweight\n"
        "GoodGene\tGoodCell\tsrc\tmouse\tbrain\t0.9\n"
        "\tEmptyGene\tsrc\tmouse\tbrain\t0.9\n"   # missing gene
        "AnotherGene\t\tsrc\tmouse\tbrain\t0.9\n"  # missing cell_type
        "GoodGene2\tGoodCell2\tsrc\tmouse\tbrain\t0.7\n"
    )
    db = MarkerDB.load(override_path=f)
    assert len(db) == 2
    assert db.candidates("GoodGene")[0].cell_type == "GoodCell"
    assert db.candidates("GoodGene2")[0].cell_type == "GoodCell2"


def test_marker_db_candidate_weight_is_float() -> None:
    from _marker_db import MarkerDB  # type: ignore[import-not-found]

    db = MarkerDB.load(tissue="brain")
    candidates = db.candidates("Aqp4")
    assert all(isinstance(c.weight, float) for c in candidates)
    assert all(0.0 <= c.weight <= 1.0 for c in candidates)
