"""Tests for the integration intrinsic panel (iLISI / within-batch kNN preservation)."""

from __future__ import annotations

import numpy as np
import pytest

from omicsclaw.runtime.consensus.integration_panel import (
    DEFAULT_PANEL_WEIGHTS,
    PANEL_METRICS,
    combine_panel,
    intrinsic_integration_panel,
)

# iLISI needs the optional Tier-4 ``harmonypy``; absent it the panel fail-soft
# drops ``ilisi_norm``, so iLISI-specific expectations are guarded on this flag
# to stay green in Python-only test environments.
try:
    import harmonypy  # noqa: F401
    _HARMONYPY = True
except Exception:  # pragma: no cover - depends on the install tier
    _HARMONYPY = False


# --------------------------- combine_panel (unit) -------------------------- #

def test_combine_perfect_and_worst() -> None:
    assert combine_panel({"ilisi_norm": 1.0, "knn_preservation_norm": 1.0}) == 1.0
    assert combine_panel({"ilisi_norm": 0.0, "knn_preservation_norm": 0.0}) == 0.0


def test_combine_clips_out_of_range_inputs() -> None:
    # raw outside [0,1] is clipped before weighting (no negative credit, no >1).
    assert combine_panel(
        {"ilisi_norm": 5.0, "knn_preservation_norm": -3.0}
    ) == pytest.approx((0.5 * 1.0 + 0.5 * 0.0) / 1.0)


def test_combine_renormalises_over_present_metrics() -> None:
    # knn_preservation missing -> weight renormalises over ilisi only.
    assert combine_panel({"ilisi_norm": 0.4}) == pytest.approx(0.4)


def test_combine_ignores_nan_and_empty() -> None:
    assert combine_panel({}) == 0.0
    assert combine_panel({"ilisi_norm": float("nan")}) == 0.0


def test_combine_ignores_zero_weight_diagnostics() -> None:
    # batch_asw / cluster_asw are diagnostics (weight 0): present but never scored.
    assert combine_panel({"batch_asw_norm": 0.9, "cluster_asw_norm": 0.9}) == 0.0
    # ... and they don't perturb a real scored metric.
    assert combine_panel(
        {"ilisi_norm": 0.3, "batch_asw_norm": 0.9, "cluster_asw_norm": 0.9}
    ) == pytest.approx(0.3)


def test_combine_respects_custom_weights() -> None:
    w = {"ilisi_norm": 1.0, "knn_preservation_norm": 0.0}
    assert combine_panel(
        {"ilisi_norm": 0.3, "knn_preservation_norm": 0.9}, weights=w
    ) == pytest.approx(0.3)


# --------------------- intrinsic_integration_panel (behavior) -------------- #

def _two_batch_blobs(seed: int = 0, batch_offset: float = 8.0):
    """Two batches, two within-batch clusters each.

    Returns ``(cluster_labels, batch_labels, x_pca)`` where ``x_pca`` carries a
    batch offset (the technical effect) on top of a shared 2-cluster biology.
    """
    rng = np.random.default_rng(seed)
    coords, clusters, batches = [], [], []
    for b in (0, 1):
        for c in (0, 1):
            pts = rng.normal(loc=(c * 5.0, b * batch_offset), scale=0.3, size=(30, 2))
            coords.append(pts)
            clusters += [str(c)] * 30
            batches += [str(b)] * 30
    return np.array(clusters), np.array(batches), np.vstack(coords)


def test_panel_scalar_in_unit_interval_and_reports_metrics() -> None:
    clusters, batches, x_pca = _two_batch_blobs()
    scalar, raw = intrinsic_integration_panel(clusters, x_pca, batches, x_pca)
    assert 0.0 <= scalar <= 1.0
    assert set(raw).issubset(set(PANEL_METRICS))
    # The structure metric always computes; iLISI only when harmonypy is
    # installed (otherwise it is fail-soft dropped).
    assert "knn_preservation_norm" in raw
    if _HARMONYPY:
        assert "ilisi_norm" in raw


@pytest.mark.skipif(
    not _HARMONYPY, reason="ranking depends on iLISI, which needs optional harmonypy"
)
def test_panel_rewards_balanced_over_under_and_over_integration() -> None:
    clusters, batches, x_pca = _two_batch_blobs()
    rng = np.random.default_rng(1)

    # Good integration: batch offset removed (batches overlap), biology kept.
    good = np.column_stack([x_pca[:, 0], x_pca[:, 1] - batches.astype(int) * 8.0])
    # Unintegrated baseline: batches stay separated (== x_pca) -> low iLISI.
    unintegrated = x_pca.copy()
    # Over-integration: everything collapses to one blob -> structure destroyed.
    over = rng.normal(loc=0.0, scale=0.3, size=x_pca.shape)

    s_good, _ = intrinsic_integration_panel(clusters, good, batches, x_pca)
    s_unint, _ = intrinsic_integration_panel(clusters, unintegrated, batches, x_pca)
    s_over, _ = intrinsic_integration_panel(clusters, over, batches, x_pca)

    assert s_good > s_unint, f"good {s_good} should beat unintegrated {s_unint}"
    assert s_good > s_over, f"good {s_good} should beat over-integrated {s_over}"


def test_panel_unintegrated_preserves_structure_but_mixes_poorly() -> None:
    clusters, batches, x_pca = _two_batch_blobs()
    _, raw = intrinsic_integration_panel(clusters, x_pca, batches, x_pca)
    # embedding == x_pca -> within-batch kNN identical -> perfect preservation.
    assert raw["knn_preservation_norm"] == pytest.approx(1.0)
    # separated batches -> poor mixing (iLISI only present with harmonypy).
    if _HARMONYPY:
        assert raw["ilisi_norm"] < 0.5


def test_panel_single_batch_drops_ilisi_but_still_scores_structure() -> None:
    clusters, _, x_pca = _two_batch_blobs()
    one_batch = np.array(["0"] * x_pca.shape[0])
    scalar, raw = intrinsic_integration_panel(clusters, x_pca, one_batch, x_pca)
    assert "ilisi_norm" not in raw  # undefined for a single batch -> dropped
    assert "knn_preservation_norm" in raw
    assert 0.0 <= scalar <= 1.0


def test_panel_failsoft_on_shape_mismatch() -> None:
    clusters, batches, x_pca = _two_batch_blobs()
    bad_emb = x_pca[:10]  # wrong length -> every metric raises
    scalar, raw = intrinsic_integration_panel(clusters, bad_emb, batches, x_pca)
    assert scalar == 0.0
    assert raw == {}


def test_default_weights_cover_only_scored_axes() -> None:
    assert set(DEFAULT_PANEL_WEIGHTS) == {"ilisi_norm", "knn_preservation_norm"}
    assert "batch_asw_norm" in PANEL_METRICS and "cluster_asw_norm" in PANEL_METRICS


# ----------------------------- driver integration ------------------------- #

import pytest as _pytest  # noqa: E402
from pathlib import Path  # noqa: E402

from omicsclaw.runtime.consensus.driver import _compute_k_stats  # noqa: E402
from omicsclaw.runtime.consensus.member import ConsensusMember  # noqa: E402


def test_compute_k_stats_flags_divergence() -> None:
    import pandas as pd

    diverged = pd.DataFrame({"a": list("aabbcc" * 2), "b": ["x"] * 12})  # k=3 vs k=1
    stats = _compute_k_stats(diverged)
    assert stats["k_by_member"] == {"a": 3, "b": 1}
    assert stats["k_min"] == 1 and stats["k_max"] == 3 and stats["diverged"] is True

    comparable = pd.DataFrame({"a": list("aabbc" * 2 + "aa"), "b": list("aabb" * 3)})  # k=3 vs k=2
    assert _compute_k_stats(comparable)["diverged"] is False


class _IntegrationReader:
    """Returns per-member cluster labels + a FIXED reader intrinsic (so the test
    can prove the integration panel REPLACED it). The unintegrated baseline
    over-clusters (cluster x batch); integrated members recover the biology."""

    FIXED_INTRINSIC = 0.123

    def __init__(self, clusters, batches, obs_ids):
        self._clusters = clusters
        self._batches = batches
        self._obs_ids = obs_ids

    def read_labels(self, member, output_root):
        import pandas as pd

        if member.name == "unintegrated":
            vals = [f"{c}_{b}" for c, b in zip(self._clusters, self._batches)]
        else:
            vals = list(self._clusters)
        return pd.Series(vals, index=self._obs_ids, name="label")

    def read_intrinsic_quality(self, member, output_root):
        return self.FIXED_INTRINSIC


@_pytest.mark.asyncio
async def test_driver_runs_integration_panel_and_records_k(tmp_path: Path) -> None:
    import anndata as ad

    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus
    from omicsclaw.runtime.consensus.source_registry import ConsensusSource

    clusters, batches, x_pca = _two_batch_blobs()
    obs_ids = [f"cell_{i}" for i in range(x_pca.shape[0])]
    good = np.column_stack([x_pca[:, 0], x_pca[:, 1] - batches.astype(int) * 8.0])
    # Per-member (embedding key, embedding matrix).
    member_emb = {
        "unintegrated": ("X_pca", x_pca),                 # batch-separated -> low iLISI
        "harmony": ("X_harmony", good),                   # mixed -> high iLISI
        "scanorama": ("X_scanorama", good + 1e-3),
    }

    def runner(**kwargs):
        out = Path(kwargs["output_dir"])  # output_root/<member.name>
        out.mkdir(parents=True, exist_ok=True)
        rep_key, emb = member_emb[out.name]
        adata = ad.AnnData(X=np.zeros((len(obs_ids), 1), dtype=np.float32))
        adata.obs_names = obs_ids
        adata.obs["batch"] = batches
        adata.obsm["X_pca"] = x_pca.astype(np.float32)
        adata.obsm[rep_key] = np.asarray(emb, dtype=np.float32)
        adata.write_h5ad(out / "processed.h5ad")
        (out / "result.json").write_text(
            f'{{"summary": {{"representation_used": "{rep_key}"}}}}'
        )

        class _R:
            exit_code = 0

        return _R()

    members = [
        ConsensusMember(name=n, skill_name="sc-integrate-cluster", params={"cluster-method": "leiden"})
        for n in ("unintegrated", "harmony", "scanorama")
    ]
    source = ConsensusSource(
        reader=_IntegrationReader(clusters, batches, obs_ids),
        domain="singlecell", intrinsic_panel="integration",
    )

    run = await run_typed_consensus(
        members=members, source=source, input_path="",
        output_dir=tmp_path / "out", operator="kmode",
        bc_selector=lambda s, k: [x.member for x in s][:3],
        score_config=ScoreConfig(), seed=0, runner=runner, batch_key="batch",
    )

    # Panel ran with the integration metric columns.
    panel_csv = tmp_path / "out" / "member_intrinsic_panel.csv"
    assert panel_csv.exists()
    import pandas as pd

    cols = set(pd.read_csv(panel_csv).columns)
    assert {"member", "knn_preservation_norm", "intrinsic_panel"} <= cols
    if _HARMONYPY:
        assert "ilisi_norm" in cols

    # The panel REPLACED the reader's fixed intrinsic, and mixing pays off.
    assert all(v != _IntegrationReader.FIXED_INTRINSIC for v in run.intrinsic_map.values())
    # harmony mixes batches -> higher iLISI -> higher intrinsic; this ranking
    # needs the iLISI metric (optional harmonypy), so guard it.
    if _HARMONYPY:
        assert run.intrinsic_map["harmony"] > run.intrinsic_map["unintegrated"]

    # k-divergence recorded: unintegrated over-clusters (cluster x batch).
    assert run.k_stats["k_by_member"]["unintegrated"] > run.k_stats["k_by_member"]["harmony"]
