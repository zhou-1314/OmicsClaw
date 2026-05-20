"""TypedRunBundle reader — T1 preflight per ADR 0012 §"Failure semantics".

Reads a verified typed consensus run directory (the output of
`consensus-domains` / `sc-consensus-clustering`) and assembles a frozen
``TypedRunBundle`` holding everything downstream slices will consume.

Failure modes are exhaustive at this level — every avoidable error is
detected here and raised as a typed exception, so later slices can
assume the bundle is structurally sound.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anndata as ad
import pandas as pd

from _errors import AdataMismatchError, TypedRunInvalidError


_REQUIRED_FILES = ("plan.json", "consensus_labels.tsv")
_OPTIONAL_FILES = ("member_scores.csv", "cross_method_nmi.csv")


@dataclass(frozen=True)
class TypedRunBundle:
    """Frozen handle on one verified typed consensus run, ready to feed
    into Slice 3 inline DE and Slice 5 LLM annotation.

    Field invariants (enforced at load time, never re-checked downstream):
    - ``typed_run_dir`` exists and is absolute.
    - ``plan`` is the parsed plan.json content.
    - ``consensus_labels`` has ``observation`` and
      ``consensus_<operator>`` columns; observation is a subset of
      ``adata.obs.index`` (asserted at load time without materializing X).
    - ``consensus_label_column`` derives from ``plan['operator']``.
    - ``adata_path`` exists, is absolute, and resolves to an .h5ad.
    - ``member_scores`` / ``nmi_matrix`` are best-effort (empty DataFrame
      if the typed run skipped writing them, e.g. < 2 BCs path).
    """

    typed_run_dir: Path
    plan: dict[str, Any]
    consensus_labels: pd.DataFrame
    consensus_label_column: str
    member_scores: pd.DataFrame
    nmi_matrix: pd.DataFrame
    adata_path: Path


def load_typed_run(
    typed_run_dir: Path | str,
    *,
    adata_override: Path | str | None = None,
) -> TypedRunBundle:
    """Load and validate a typed consensus run directory.

    Parameters
    ----------
    typed_run_dir
        The output directory of a previous ``consensus-domains`` /
        ``sc-consensus-clustering`` invocation.
    adata_override
        If given, used as the adata source path instead of
        ``plan.json:input_path``. Required when the typed run was
        produced before Slice 0 (no ``input_path`` field).

    Raises
    ------
    TypedRunInvalidError
        Required files missing, plan.json malformed, or no input_path
        and no override.
    AdataMismatchError
        Adata file missing at the resolved path, or obs index does not
        contain the consensus_labels observations.
    """
    typed_run_dir = Path(typed_run_dir).resolve()
    if not typed_run_dir.is_dir():
        raise TypedRunInvalidError(
            f"typed run directory does not exist: {typed_run_dir}"
        )

    for required in _REQUIRED_FILES:
        if not (typed_run_dir / required).exists():
            raise TypedRunInvalidError(
                f"required file missing under {typed_run_dir}: {required}"
            )

    # Parse plan.json
    plan_path = typed_run_dir / "plan.json"
    try:
        plan: dict[str, Any] = json.loads(plan_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TypedRunInvalidError(
            f"plan.json malformed or unreadable: cannot parse JSON ({exc})"
        ) from exc

    operator = str(plan.get("operator") or "")
    if not operator:
        raise TypedRunInvalidError(
            "plan.json lacks 'operator' field; cannot derive consensus label column"
        )
    consensus_label_column = f"consensus_{operator}"

    # Resolve adata path: --adata override > plan.input_path
    adata_path = _resolve_adata_path(plan, adata_override, typed_run_dir)

    # Load consensus_labels.tsv
    consensus_labels = pd.read_csv(typed_run_dir / "consensus_labels.tsv", sep="\t")
    if "observation" not in consensus_labels.columns:
        raise TypedRunInvalidError(
            "consensus_labels.tsv missing 'observation' column"
        )
    if consensus_label_column not in consensus_labels.columns:
        raise TypedRunInvalidError(
            f"consensus_labels.tsv missing '{consensus_label_column}' column "
            f"(operator='{operator}' but the file does not carry that consensus output)"
        )

    # Cheap obs-index check: backed='r' avoids materializing X
    _assert_obs_index_contains(adata_path, consensus_labels["observation"].astype(str).tolist())

    # Best-effort optional files
    member_scores = _read_optional_csv(typed_run_dir / "member_scores.csv")
    nmi_matrix = _read_optional_csv(typed_run_dir / "cross_method_nmi.csv", index_col=0)

    return TypedRunBundle(
        typed_run_dir=typed_run_dir,
        plan=plan,
        consensus_labels=consensus_labels,
        consensus_label_column=consensus_label_column,
        member_scores=member_scores,
        nmi_matrix=nmi_matrix,
        adata_path=adata_path,
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _resolve_adata_path(
    plan: dict[str, Any],
    adata_override: Path | str | None,
    typed_run_dir: Path,
) -> Path:
    if adata_override is not None:
        path = Path(adata_override).resolve()
        if not path.exists():
            raise AdataMismatchError(
                f"adata override path does not exist: {path}"
            )
        return path

    input_path = plan.get("input_path")
    if not input_path:
        raise TypedRunInvalidError(
            f"plan.json lacks 'input_path' (legacy typed run produced before "
            f"Slice 0). Pass --adata <path> explicitly. Typed run dir: {typed_run_dir}"
        )
    path = Path(str(input_path)).resolve()
    if not path.exists():
        raise AdataMismatchError(
            f"adata recorded in plan.json:input_path does not exist: {path}. "
            f"Pass --adata <path> if the file has moved."
        )
    return path


def _assert_obs_index_contains(adata_path: Path, observations: list[str]) -> None:
    """Open adata in backed mode and assert obs.index ⊇ observations.

    Using ``backed='r'`` keeps X off-disk so this stays cheap even on
    40k-cell Slide-seq files.
    """
    try:
        adata = ad.read_h5ad(adata_path, backed="r")
    except (OSError, KeyError, ValueError) as exc:
        raise AdataMismatchError(
            f"failed to open adata at {adata_path}: {exc}"
        ) from exc

    try:
        obs_index = set(adata.obs.index.astype(str))
    finally:
        adata.file.close()

    missing = [o for o in observations if o not in obs_index]
    if missing:
        sample = missing[:3]
        raise AdataMismatchError(
            f"{len(missing)}/{len(observations)} consensus_labels observation ids "
            f"absent from adata.obs.index (e.g. {sample!r}). Adata path: {adata_path}"
        )


def _read_optional_csv(path: Path, **read_kwargs: Any) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, **read_kwargs)
    except (OSError, pd.errors.ParserError):
        return pd.DataFrame()
