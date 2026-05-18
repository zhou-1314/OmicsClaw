"""DLPFC 151673 hero benchmark for consensus-domains (ADR 0011).

Fetches sample 151673, runs the 5-member consensus pipeline, and verifies
the consensus passes the **task-targeted metric panel** declared in
``expected_metrics.json`` — ARI + AMI + V-measure + MLAMI (spatial-only).
Each ``hard_metrics`` entry must satisfy ``consensus_metric ≥
best_member_metric − noise_floor`` AND ``consensus_metric ≥ min_absolute``.

H / C / CHAOS / PAS are computed but only reported, not used for pass/fail.

NOT vendored into the repo — the first run downloads ≈50 MB and caches
under ``$XDG_CACHE_HOME/omicsclaw/dlpfc_151673/`` (default
``~/.cache/omicsclaw/...``). Re-runs hit the cache.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger("dlpfc_benchmark")

_EXPECTED_METRICS_PATH = Path(__file__).resolve().parent / "expected_metrics.json"
_DEFAULT_DATA_URL = os.environ.get(
    "OMICSCLAW_DLPFC_151673_URL",
    "https://research.libd.org/spatialLIBD/151673.h5ad",
)


# ---------- fetching + paths ---------------------------------------------- #

def _cache_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "omicsclaw" / "dlpfc_151673"


def _maybe_fetch(target: Path, source_url: str, force: bool) -> Path:
    if target.exists() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request

        LOGGER.info("Downloading DLPFC 151673 from %s -> %s", source_url, target)
        urllib.request.urlretrieve(source_url, str(target))  # noqa: S310
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"DLPFC fetch failed ({exc}). Set OMICSCLAW_DLPFC_151673_URL to a "
            f"working mirror, or download manually to {target}."
        ) from exc
    return target


def _resolve_consensus_cli() -> Path:
    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent
    cli = repo_root / "skills" / "spatial" / "consensus-domains" / "consensus_domains.py"
    if not cli.exists():
        raise FileNotFoundError(f"consensus-domains CLI missing at {cli}")
    return cli


# ---------- metric panel --------------------------------------------------- #

def _compute_metric_panel(
    pred_labels: "pd.Series",
    gt_labels: "pd.Series",
    coords: Optional["pd.DataFrame"] = None,
) -> dict[str, float]:
    """Compute the full metric panel for one (pred, gt) alignment.

    ``pred_labels`` and ``gt_labels`` must be co-indexed (caller handles the
    intersection). ``coords`` is the per-observation spatial coords keyed by
    the same index — required for MLAMI / CHAOS / PAS.
    """
    import pandas as pd
    from sklearn.metrics import (
        adjusted_mutual_info_score,
        adjusted_rand_score,
        homogeneity_completeness_v_measure,
    )

    gt_arr = gt_labels.to_numpy()
    pred_arr = pred_labels.to_numpy()
    h, c, v = homogeneity_completeness_v_measure(gt_arr, pred_arr)
    panel: dict[str, float] = {
        "ARI":          float(adjusted_rand_score(gt_arr, pred_arr)),
        "AMI":          float(adjusted_mutual_info_score(gt_arr, pred_arr)),
        "V_measure":    float(v),
        "Homogeneity":  float(h),
        "Completeness": float(c),
    }
    if coords is not None and len(coords) == len(pred_arr):
        try:
            from omicsclaw.runtime.consensus.spatial_metrics import chaos, mlami, pas

            coord_arr = coords.to_numpy()
            panel["MLAMI"] = float(mlami(pred_arr, coord_arr, seed=0))
            panel["CHAOS"] = float(chaos(pred_arr, coord_arr))
            panel["PAS"]   = float(pas(pred_arr, coord_arr))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("spatial metrics failed (%s); skipping MLAMI/CHAOS/PAS", exc)
    return panel


def _join_pred_gt(pred_tsv: Path, gt_csv: Path, observation_col: str) -> tuple["pd.Series", "pd.Series"]:
    import pandas as pd

    pred = pd.read_csv(pred_tsv, sep="\t").set_index(observation_col)
    gt = pd.read_csv(gt_csv).set_index(observation_col)
    common = pred.index.intersection(gt.index)
    if common.empty:
        raise RuntimeError(
            f"prediction and ground-truth share no observations under column "
            f"{observation_col!r} (pred index sample={pred.index[:3].tolist()}, "
            f"gt index sample={gt.index[:3].tolist()})"
        )
    return pred.loc[common].iloc[:, 0], gt.loc[common].iloc[:, 0]


def _load_coords(h5ad_path: Path, common_index: "pd.Index") -> Optional["pd.DataFrame"]:
    """Load ``obsm['spatial']`` from the AnnData and reindex to ``common_index``."""
    try:
        import anndata as ad
        import pandas as pd

        adata = ad.read_h5ad(h5ad_path)
        if "spatial" not in adata.obsm:
            LOGGER.warning("no obsm['spatial'] in %s; MLAMI/CHAOS/PAS will be skipped", h5ad_path)
            return None
        coords = pd.DataFrame(
            adata.obsm["spatial"],
            index=adata.obs_names.astype(str),
            columns=[f"sp_{i}" for i in range(adata.obsm["spatial"].shape[1])],
        )
        return coords.reindex(common_index.astype(str)).dropna()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("could not load coords from %s (%s)", h5ad_path, exc)
        return None


# ---------- pass-rule evaluation ------------------------------------------ #

def _evaluate_panel(
    *,
    consensus_panel: dict[str, float],
    member_panels: dict[str, dict[str, float]],
    expected: dict,
    has_spatial: bool,
) -> tuple[bool, list[str], dict[str, dict[str, float]]]:
    """Apply ADR 0011 ``all_hard_pass`` against the panel."""
    failures: list[str] = []
    detail: dict[str, dict[str, float]] = {}

    survived = list(member_panels.keys())
    if len(survived) < expected["min_members_surviving"]:
        failures.append(
            f"only {len(survived)}/{len(expected['members_required'])} members survived; "
            f"need ≥ {expected['min_members_surviving']}"
        )

    for entry in expected["hard_metrics"]:
        name = entry["name"]
        applies_to = entry.get("applies_to", "all")
        if applies_to == "spatial_only" and not has_spatial:
            continue

        consensus_v = consensus_panel.get(name)
        if consensus_v is None:
            failures.append(f"{name}: not computed for consensus")
            continue
        member_values = {m: p[name] for m, p in member_panels.items() if name in p}
        best_member_v = max(member_values.values()) if member_values else 0.0
        noise = float(entry["noise_floor"])
        min_abs = float(entry.get("min_absolute", 0.0))

        passed = True
        if consensus_v < best_member_v - noise:
            passed = False
            failures.append(
                f"{name}: consensus {consensus_v:.4f} < best member {best_member_v:.4f} − {noise}"
            )
        if consensus_v < min_abs:
            passed = False
            failures.append(f"{name}: consensus {consensus_v:.4f} below absolute floor {min_abs}")
        detail[name] = {
            "consensus": consensus_v,
            "best_member": best_member_v,
            "noise_floor": noise,
            "min_absolute": min_abs,
            "passed": passed,
        }

    return (not failures, failures, detail)


# ---------- entry --------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DLPFC 151673 consensus-domains benchmark")
    parser.add_argument(
        "--output-dir",
        default=str(Path(os.environ.get("TMPDIR", "/tmp")) / "consensus_benchmark_151673"),
    )
    parser.add_argument("--data-url", default=_DEFAULT_DATA_URL)
    parser.add_argument("--ground-truth", default=None, help="Ground-truth CSV path")
    parser.add_argument("--observation-col", default="observation")
    parser.add_argument("--force-refetch", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup without running consensus.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    expected = json.loads(_EXPECTED_METRICS_PATH.read_text())
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cache = _cache_root()
    h5ad_path = cache / "151673.h5ad"
    if args.dry_run:
        print(f"[dry-run] would fetch {args.data_url} -> {h5ad_path}")
        print(f"[dry-run] expected metrics ({len(expected['hard_metrics'])} hard, "
              f"{len(expected['report_only_metrics'])} report-only)")
        return 0

    _maybe_fetch(h5ad_path, args.data_url, args.force_refetch)
    cli = _resolve_consensus_cli()

    members_csv = ",".join(expected["members_required"])
    consensus_cmd = [
        sys.executable, str(cli),
        "--input", str(h5ad_path),
        "--output", str(output_dir),
        "--members", members_csv,
        "--operator", expected["operator"],
        "--non-interactive",
        "--seed", "0",
    ]
    LOGGER.info("Running: %s", " ".join(consensus_cmd))
    proc = subprocess.run(consensus_cmd, check=False)
    if proc.returncode != 0:
        LOGGER.error("consensus-domains exited with %d", proc.returncode)
        return proc.returncode

    if args.ground_truth is None:
        LOGGER.warning(
            "No --ground-truth provided; skipping metric panel assertion. "
            "Pass a layer-annotations CSV to enable the verified-vs-published check."
        )
        return 0

    # Compute consensus panel
    consensus_tsv = output_dir / "consensus_labels.tsv"
    pred_consensus, gt_aligned = _join_pred_gt(
        consensus_tsv, Path(args.ground_truth), args.observation_col
    )
    coords = _load_coords(h5ad_path, pred_consensus.index)
    has_spatial = coords is not None and len(coords) > 0

    consensus_panel = _compute_metric_panel(pred_consensus, gt_aligned, coords)

    # Compute per-member panel
    member_panels: dict[str, dict[str, float]] = {}
    for method in expected["members_required"]:
        member_csv = output_dir / method / "figure_data" / "spatial_full.csv"
        if not member_csv.exists():
            LOGGER.warning("member %s missing artifact %s; skipping", method, member_csv)
            continue
        import pandas as pd

        member_df = pd.read_csv(member_csv).set_index("observation")["spatial_domain"]
        common = member_df.index.intersection(gt_aligned.index)
        if common.empty:
            continue
        pred_member = member_df.loc[common]
        gt_member = gt_aligned.loc[common]
        member_coords = coords.loc[common] if coords is not None else None
        member_panels[method] = _compute_metric_panel(pred_member, gt_member, member_coords)

    ok, failures, detail = _evaluate_panel(
        consensus_panel=consensus_panel,
        member_panels=member_panels,
        expected=expected,
        has_spatial=has_spatial,
    )

    summary = {
        "consensus_panel": consensus_panel,
        "member_panels": member_panels,
        "pass_detail": detail,
        "report_only_metrics": expected.get("report_only_metrics", []),
        "passed": ok,
        "failures": failures,
    }
    (output_dir / "benchmark_result.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
