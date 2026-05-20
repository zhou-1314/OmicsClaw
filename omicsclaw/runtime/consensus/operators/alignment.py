"""Hungarian alignment of categorical clustering labels to a reference label space.

Used by ``kmode_consensus`` and ``weighted_consensus`` to make labels comparable
across members before per-observation voting (member-A's ``cluster_0`` and
member-B's ``cluster_0`` are not the same biological entity until aligned).
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment


def align_labels(reference: np.ndarray, source: np.ndarray) -> np.ndarray:
    """Remap ``source`` labels to match ``reference``'s label space.

    Returns a new array with the same shape as ``source`` where each label has
    been relabelled to the reference label maximising co-occurrence (Hungarian
    assignment on the negated contingency matrix).

    Labels in ``source`` that have no match in ``reference`` (when ``source``
    has more clusters than ``reference``) keep their original string form
    prefixed with ``"_extra_"``.
    """
    if reference.shape != source.shape:
        raise ValueError(
            f"reference and source must share shape, got {reference.shape} vs {source.shape}"
        )

    ref_labels = np.unique(reference)
    src_labels = np.unique(source)
    ref_idx = {lbl: i for i, lbl in enumerate(ref_labels)}
    src_idx = {lbl: i for i, lbl in enumerate(src_labels)}

    contingency = np.zeros((len(ref_labels), len(src_labels)), dtype=np.int64)
    for r, s in zip(reference, source):
        contingency[ref_idx[r], src_idx[s]] += 1

    n = max(len(ref_labels), len(src_labels))
    cost = np.zeros((n, n), dtype=np.int64)
    cost[: contingency.shape[0], : contingency.shape[1]] = contingency

    row_ind, col_ind = linear_sum_assignment(-cost)

    mapping: dict = {}
    for r, c in zip(row_ind, col_ind):
        if c >= len(src_labels):
            continue
        if r < len(ref_labels):
            mapping[src_labels[c]] = ref_labels[r]
        else:
            mapping[src_labels[c]] = f"_extra_{r}"

    return np.array([mapping[s] for s in source])
