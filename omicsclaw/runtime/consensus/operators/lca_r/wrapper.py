"""Python wrapper around the LCA R subprocess.

The wrapper is intentionally simple: write the aligned member labels to a
temp TSV, ``Rscript consensus_lca.r ...``, read the output back. Errors
surface as ``LCAUnavailableError`` (R or diceR missing) or ``RuntimeError``
(R returned non-zero) so callers can degrade gracefully — e.g. ``plan.py``
falls back to kmode/weighted when LCA is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from omicsclaw.runtime.consensus.operators.categorical import (
    ConsensusResult,
    _align_all_to_reference,
)
from omicsclaw.skill.execution.environment import (
    scrub_internal_control_credentials,
)

_THIS_DIR = Path(__file__).resolve().parent
_LCA_R_SCRIPT = _THIS_DIR / "consensus_lca.r"


class LCAUnavailableError(RuntimeError):
    """Raised when LCA cannot run because the R toolchain is missing.

    Callers should catch this and fall back to a Python operator (or surface
    the error to the user with installation instructions).
    """


def rscript_available() -> bool:
    """``True`` when ``Rscript`` is on ``PATH``."""
    return shutil.which("Rscript") is not None


def lca_consensus(
    labels_df: pd.DataFrame,
    *,
    seed: int | None = None,
    rscript_bin: str | None = None,
    keep_tmp: bool = False,
) -> ConsensusResult:
    """Run diceR::LCA on the columns of ``labels_df`` via the R script.

    Performs the same Hungarian alignment + frequency relabel that kmode and
    weighted do, so the three operators are interchangeable from the caller's
    perspective.
    """
    if labels_df.shape[1] < 2:
        raise ValueError("lca_consensus requires at least 2 members")

    rbin = rscript_bin or os.environ.get("OMICSCLAW_RSCRIPT", "Rscript")
    if shutil.which(rbin) is None:
        raise LCAUnavailableError(
            f"{rbin!r} not found on PATH. Install via:\n"
            f"  mamba env update -f {_THIS_DIR / 'env.yaml'}\n"
            "or fall back to --operator kmode/weighted."
        )
    if not _LCA_R_SCRIPT.exists():
        raise LCAUnavailableError(f"LCA R script missing at {_LCA_R_SCRIPT}")

    aligned_df, _ = _align_all_to_reference(labels_df)

    with tempfile.TemporaryDirectory(prefix="oc_lca_") as tmp:
        tmp_path = Path(tmp)
        input_tsv = tmp_path / "aligned.tsv"
        output_tsv = tmp_path / "consensus.tsv"
        aligned_df.to_csv(input_tsv, sep="\t", index=True, index_label="observation")

        cmd = [rbin, str(_LCA_R_SCRIPT), "-i", str(input_tsv), "-o", str(output_tsv)]
        if seed is not None:
            cmd += ["--seed", str(int(seed))]

        proc = subprocess.run(  # noqa: S603 — args list, no shell
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=scrub_internal_control_credentials(os.environ),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"LCA R subprocess failed (exit={proc.returncode}).\n"
                f"stderr:\n{proc.stderr}"
            )
        if not output_tsv.exists():
            raise RuntimeError(
                f"LCA R script returned 0 but produced no output at {output_tsv}.\n"
                f"stderr:\n{proc.stderr}"
            )

        output_df = pd.read_csv(output_tsv, sep="\t", index_col=0)
        if "consensus_lca" not in output_df.columns:
            raise RuntimeError(
                f"LCA output missing 'consensus_lca' column; got {list(output_df.columns)}"
            )

        # Re-align output index to the original input order in case R reorders.
        labels_series = output_df["consensus_lca"].reindex(labels_df.index)
        if labels_series.isna().any():
            missing = labels_series[labels_series.isna()].index.tolist()
            raise RuntimeError(
                f"LCA output missing labels for observations: {missing[:5]}..."
            )

        if keep_tmp:
            # Caller wanted to inspect the temp files; copy them out before
            # the TemporaryDirectory context cleans up.
            keep_dir = Path(tempfile.mkdtemp(prefix="oc_lca_keep_"))
            shutil.copy(input_tsv, keep_dir / "aligned.tsv")
            shutil.copy(output_tsv, keep_dir / "consensus.tsv")

    consensus_arr = np.asarray(labels_series.to_numpy())
    labels_out = pd.Series(consensus_arr, index=labels_df.index, name="consensus_lca")
    return ConsensusResult(
        labels=labels_out,
        aligned_labels=aligned_df,
        method="lca",
        n_clusters_returned=int(pd.unique(consensus_arr).shape[0]),
        seed=seed,
    )
