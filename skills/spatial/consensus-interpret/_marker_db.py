"""Marker database loader for `consensus-interpret`.

Loads tissue-keyed marker TSVs (bundled under ``data/markers/`` per
ADR 0012) or a user-provided override. Exposes a stable
``MarkerDB.candidates(gene) -> list[Candidate]`` interface so Slice 4
(marker → cell-type pre-LLM ranking) and Slice 6 (invariant grep test)
can program against a single shape.

Schema: tab-separated with header
``gene\\tcell_type\\tsource\\tspecies\\ttissue\\tweight``. Rows missing
``gene`` or ``cell_type`` are skipped with a warning; malformed
``weight`` defaults to 0.5 (logged at debug level).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from _errors import MarkerDBUnavailableError

logger = logging.getLogger("consensus-interpret.marker_db")

_BUNDLED_DIR = Path(__file__).resolve().parent / "data" / "markers"

# Tissue key → bundled TSV filename (mirrors parameters.yaml marker_db_bundled).
_BUNDLED_FILES: dict[str, str] = {
    "brain": "panglaodb_brain.tsv",
    "immune": "panglaodb_immune.tsv",
    "kidney": "panglaodb_kidney.tsv",
    "liver": "cellmarker_liver.tsv",
}

_REQUIRED_COLUMNS = ("gene", "cell_type")


@dataclass(frozen=True)
class Candidate:
    """One (gene → cell type) annotation entry from the marker DB."""

    cell_type: str
    weight: float
    source: str
    species: str
    tissue: str


class MarkerDB:
    """In-memory marker → cell-type lookup.

    Construct via :meth:`load` — never call ``__init__`` directly.
    """

    __slots__ = ("_by_gene", "_source_label")

    def __init__(self, by_gene: dict[str, list[Candidate]], source_label: str) -> None:
        self._by_gene = by_gene
        self._source_label = source_label

    # ----- public API ----- #

    def candidates(self, gene: str) -> list[Candidate]:
        """All candidates for ``gene`` (case-sensitive lookup)."""
        return list(self._by_gene.get(gene, ()))

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_gene.values())

    @property
    def source_label(self) -> str:
        """Human-readable label of where this DB came from (e.g.
        ``"bundled:brain"``, ``"override:/path/to/custom.tsv"``).
        Recorded in audit.json so reviewers can trace evidence."""
        return self._source_label

    # ----- constructor ----- #

    @classmethod
    def load(
        cls,
        tissue: str | None = None,
        override_path: Path | str | None = None,
    ) -> "MarkerDB":
        """Load from bundled tissue DB or a user-provided TSV.

        Precedence: ``override_path`` > ``tissue``. Either must be set;
        if neither resolves, ``MarkerDBUnavailableError`` (exit 5).
        """
        if override_path is not None:
            path = Path(override_path).resolve()
            if not path.exists():
                raise MarkerDBUnavailableError(
                    f"--markers path does not exist: {path}"
                )
            label = f"override:{path}"
        elif tissue is not None:
            filename = _BUNDLED_FILES.get(tissue)
            if filename is None:
                supported = ", ".join(sorted(_BUNDLED_FILES))
                raise MarkerDBUnavailableError(
                    f"tissue '{tissue}' not in bundled marker DBs (supported: {supported}). "
                    f"Pass --markers <path.tsv> for unsupported tissues."
                )
            path = _BUNDLED_DIR / filename
            if not path.exists():
                raise MarkerDBUnavailableError(
                    f"bundled marker DB file missing on disk: {path} (corrupt install?)"
                )
            label = f"bundled:{tissue}"
        else:
            raise MarkerDBUnavailableError(
                "either --tissue (one of brain/immune/kidney/liver) or "
                "--markers <path.tsv> must be provided"
            )

        return cls._from_tsv(path, label)

    # ----- impl ----- #

    @classmethod
    def _from_tsv(cls, path: Path, label: str) -> "MarkerDB":
        try:
            df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False)
        except (OSError, pd.errors.ParserError) as exc:
            raise MarkerDBUnavailableError(
                f"failed to read marker TSV {path}: {exc}"
            ) from exc

        missing_cols = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
        if missing_cols:
            raise MarkerDBUnavailableError(
                f"marker TSV {path} schema invalid — missing required columns: "
                f"{missing_cols}. Required columns: {list(_REQUIRED_COLUMNS)}."
            )

        by_gene: dict[str, list[Candidate]] = {}
        skipped = 0
        for _, row in df.iterrows():
            gene = (row.get("gene") or "").strip()
            cell_type = (row.get("cell_type") or "").strip()
            if not gene or not cell_type:
                skipped += 1
                continue
            weight_raw = (row.get("weight") or "").strip()
            try:
                weight = float(weight_raw) if weight_raw else 0.5
            except ValueError:
                logger.debug("malformed weight %r for gene=%s, defaulting to 0.5", weight_raw, gene)
                weight = 0.5
            cand = Candidate(
                cell_type=cell_type,
                weight=weight,
                source=(row.get("source") or "").strip() or label,
                species=(row.get("species") or "").strip() or "unknown",
                tissue=(row.get("tissue") or "").strip() or "unknown",
            )
            by_gene.setdefault(gene, []).append(cand)

        if skipped:
            logger.warning("marker DB %s: skipped %d malformed rows", path, skipped)

        return cls(by_gene=by_gene, source_label=label)
