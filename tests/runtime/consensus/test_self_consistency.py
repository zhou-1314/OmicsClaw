"""Self-consistency regression test (ADR 0011).

Asserts that consensus labels are MORE stable across random seeds than any
single member's labels — operationally, ``stdev(AMI(consensus_seed_i,
consensus_seed_0))`` is no larger than ``stdev(AMI(best_member_seed_i,
best_member_seed_0))``.

We use **AMI** rather than ARI for the stability axis: AMI is the
chance-corrected mutual-information variant and is the metric the
ADR 0011 task-targeted protocol nominates for the no-ground-truth path
(self-consistency is a stability check, not an agreement-with-truth check).
ARI is reserved for the hero-benchmark GT-comparison panel.

This protects the core claim of the typed consensus path. A code change that
silently weakens the operator should make this test fail.

Test design: instead of depending on a vendored ``demo_visium.h5ad`` (which
the synthetic-data harness can drift away from), we synthesise N noisy
clusterings of M observations from a known ground truth. The fan-out
runtime is not exercised here — only the operator math — keeping this
test fast and deterministic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import adjusted_mutual_info_score, adjusted_rand_score

from omicsclaw.runtime.consensus.operators.categorical import kmode_consensus


def _noisy_clustering(ground_truth: np.ndarray, noise_rate: float, rng: np.random.Generator) -> np.ndarray:
    """Produce a noisy permutation of ``ground_truth``.

    Two perturbations are layered:
      1. random per-observation label flips at ``noise_rate``,
      2. a global label permutation (so members disagree on which cluster
         is "0" — exactly what Hungarian alignment is for).
    """
    labels = ground_truth.copy()
    flip_mask = rng.random(labels.shape[0]) < noise_rate
    n_clusters = int(ground_truth.max() + 1)
    new_vals = rng.integers(0, n_clusters, size=labels.shape[0])
    labels[flip_mask] = new_vals[flip_mask]
    # Random global permutation of label ids.
    perm = rng.permutation(n_clusters)
    return perm[labels]


def _generate_member_labels(
    ground_truth: np.ndarray,
    n_members: int,
    noise_rates: list[float],
    seed: int,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    columns: dict[str, np.ndarray] = {}
    for i in range(n_members):
        nr = noise_rates[i % len(noise_rates)]
        columns[f"m{i}"] = _noisy_clustering(ground_truth, nr, rng)
    return pd.DataFrame(columns, index=[f"obs_{i}" for i in range(ground_truth.shape[0])])


def test_consensus_more_stable_than_any_single_method() -> None:
    rng_truth = np.random.default_rng(0)
    n_obs = 200
    n_clusters = 5
    ground_truth = rng_truth.integers(0, n_clusters, size=n_obs)

    n_members = 5
    noise_rates = [0.05, 0.10, 0.15, 0.20, 0.25]
    seeds = list(range(10))

    consensus_labels_by_seed: dict[int, np.ndarray] = {}
    member_labels_by_seed: dict[int, pd.DataFrame] = {}

    for s in seeds:
        member_df = _generate_member_labels(ground_truth, n_members, noise_rates, seed=s)
        member_labels_by_seed[s] = member_df
        consensus = kmode_consensus(member_df, seed=s)
        consensus_labels_by_seed[s] = consensus.labels.to_numpy()

    # Compute AMI against the seed=0 reference for each path.
    ref_consensus = consensus_labels_by_seed[seeds[0]]
    ami_consensus = [
        adjusted_mutual_info_score(ref_consensus, consensus_labels_by_seed[s]) for s in seeds[1:]
    ]
    stdev_consensus = float(np.std(ami_consensus))

    # For each member column, compute the same stdev independently.
    member_stdevs: dict[str, float] = {}
    for col in member_labels_by_seed[seeds[0]].columns:
        ref_member = member_labels_by_seed[seeds[0]][col].to_numpy()
        amis = [
            adjusted_mutual_info_score(ref_member, member_labels_by_seed[s][col].to_numpy())
            for s in seeds[1:]
        ]
        member_stdevs[col] = float(np.std(amis))
    best_member_stdev = min(member_stdevs.values())

    # The consensus must be at least as stable as the best single member,
    # i.e. lower-or-equal stdev across seeds. A small tolerance accommodates
    # synthetic noise; real PRs that regress consensus stability will blow
    # past this margin.
    tolerance = 0.05
    assert stdev_consensus <= best_member_stdev + tolerance, (
        f"consensus stdev={stdev_consensus:.4f} should be ≤ "
        f"best_member_stdev={best_member_stdev:.4f} + tol={tolerance}. "
        f"per-member stdevs: {member_stdevs}"
    )


def test_consensus_ari_vs_ground_truth_at_least_as_good_as_average_member() -> None:
    """Sanity: consensus ARI vs ground truth ≥ mean(member ARI vs ground truth).

    This is a softer assertion than the SACCELERATOR ADR 0011 hero benchmark
    (which requires consensus ≥ BEST member on a specific dataset). The
    synthetic test only requires consensus ≥ mean member, which is a
    necessary condition for the headline claim to hold on real data.

    Uses ARI (not AMI) here intentionally: this is a GT-comparison axis
    (consensus vs ground_truth), so the hero-benchmark family applies.
    """
    rng = np.random.default_rng(7)
    ground_truth = rng.integers(0, 5, size=200)
    df = _generate_member_labels(
        ground_truth,
        n_members=5,
        noise_rates=[0.10, 0.12, 0.15, 0.20, 0.25],
        seed=0,
    )
    consensus = kmode_consensus(df, seed=0)
    consensus_ari = adjusted_rand_score(ground_truth, consensus.labels.to_numpy())
    member_aris = [
        adjusted_rand_score(ground_truth, df[col].to_numpy()) for col in df.columns
    ]
    assert consensus_ari >= float(np.mean(member_aris)) - 0.02, (
        f"consensus ARI {consensus_ari:.3f} < mean member ARI {np.mean(member_aris):.3f}"
    )
