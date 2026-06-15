"""Explainability helpers for a typed-consensus run.

Two pure, deterministic add-ons that make a verified consensus auditable:

- ``per_spot_confidence`` — per-observation agreement of the *aligned* member
  labels with the final consensus label (support / entropy / n_members). It
  operates on ``ConsensusResult.aligned_labels`` (post Hungarian + frequency
  relabel), so "agreement with the consensus label" is well-defined: before
  alignment a member's cluster ``3`` is not the consensus ``3``.
- ``render_nmi_heatmap`` — the cross-method NMI matrix as an annotated PNG.
  Matplotlib is optional and imported lazily; the function returns ``None``
  (the run proceeds without the figure) when it is unavailable or rendering
  fails. Headless ``Agg`` backend, no randomness.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd


def per_spot_confidence(
    aligned_labels: pd.DataFrame,
    consensus_labels: pd.Series | None = None,
) -> pd.DataFrame:
    """Per-observation consensus confidence from the operator's aligned labels.

    Operates on ``ConsensusResult.aligned_labels`` — the members that entered
    consensus, in the common post-Hungarian label space.

    ``support`` semantics depend on whether the emitted consensus label is
    comparable to the aligned member labels:

    - When ``consensus_labels`` is given (operators whose emitted label lives in
      the aligned space — ``kmode`` / ``weighted``), ``support`` is the fraction
      of members AGREEING WITH THE EMITTED consensus label, so it always
      describes the label written to ``consensus_<operator>``. This matters for
      ``weighted``: a high-weight minority can win the vote, and plurality would
      otherwise report the fraction for a *different* label. For ``kmode`` it is
      unchanged (the consensus *is* the plurality).
    - When omitted (e.g. ``lca``, whose latent-class output lives in a different
      space, making a direct "agree with the consensus label" comparison
      ill-defined), ``support`` falls back to the plurality fraction — the
      largest block of members sharing one aligned label.

    Returns a DataFrame indexed like ``aligned_labels`` with columns:
      - ``support``   — see above (1.0 = unanimous, low = contested).
      - ``entropy``   — Shannon entropy (bits) of the member label distribution
        at the observation (0.0 = unanimous, higher = more split).
      - ``n_members`` — number of members with a label at that observation.
    """
    n_members = aligned_labels.notna().sum(axis=1)
    if aligned_labels.shape[0] == 0 or aligned_labels.shape[1] == 0:
        return pd.DataFrame(
            {"support": [], "entropy": [], "n_members": []},
            index=aligned_labels.index,
        )

    def _row_stats(row: pd.Series) -> pd.Series:
        vals = row.dropna()
        if vals.empty:
            return pd.Series({"support": 0.0, "entropy": 0.0})
        counts = vals.value_counts()
        total = float(len(vals))
        if consensus_labels is not None:
            # Fraction agreeing with the EMITTED consensus label (describes the
            # label actually written to the TSV, not a possibly-different mode).
            target = consensus_labels.loc[row.name]
            support = float((vals == target).sum()) / total
        else:
            support = float(counts.iloc[0]) / total  # plurality (largest block)
        probs = (counts / total).to_numpy()
        # ``+ 0.0`` normalises IEEE negative zero (unanimous spot) to ``0.0``.
        entropy = float(-sum(p * math.log2(p) for p in probs if p > 0)) + 0.0
        return pd.Series({"support": support, "entropy": entropy})

    stats = aligned_labels.apply(_row_stats, axis=1)
    return pd.DataFrame(
        {
            "support": stats["support"].astype(float),
            "entropy": stats["entropy"].astype(float),
            "n_members": n_members.astype(int),
        },
        index=aligned_labels.index,
    )


def render_nmi_heatmap(nmi_df: pd.DataFrame, out_path: Path | str) -> Path | None:
    """Render the cross-method NMI matrix as an annotated heatmap PNG.

    Deterministic and headless. Returns the written path, or ``None`` when
    matplotlib is unavailable or rendering fails (the caller proceeds without
    the figure).
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 — figure is optional
        return None

    try:
        out_path = Path(out_path)
        members = [str(c) for c in nmi_df.columns]
        n = len(members)
        data = nmi_df.to_numpy(dtype=float)

        fig, ax = plt.subplots(figsize=(max(4.0, 0.8 * n + 2.0), max(3.5, 0.8 * n + 1.5)))
        try:
            im = ax.imshow(data, cmap="RdYlBu_r", vmin=0.0, vmax=1.0)
            ax.set_xticks(range(n))
            ax.set_xticklabels(members, rotation=45, ha="right", fontsize=8)
            ax.set_yticks(range(n))
            ax.set_yticklabels(members, fontsize=8)
            for i in range(n):
                for j in range(n):
                    v = data[i, j]
                    ax.text(
                        j, i, f"{v:.2f}", ha="center", va="center", fontsize=7,
                        color="black" if 0.3 <= v <= 0.85 else "white",
                    )
            ax.set_title("Cross-method NMI (member-vs-member agreement)", fontsize=10)
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
            fig.tight_layout()
            fig.savefig(out_path, dpi=150)
        finally:
            plt.close(fig)  # always release the figure, even if savefig raised
        return out_path
    except Exception:  # noqa: BLE001 — never fail the run for a figure
        return None
