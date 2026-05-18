"""DLPFC 151673 hero benchmark for consensus-domains (ADR 0011).

Fetches sample 151673, runs the 5-member consensus pipeline, and verifies
the consensus ARI exceeds the best single member's ARI by no less than
the noise floor in ``expected_metrics.json``.

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

LOGGER = logging.getLogger("dlpfc_benchmark")

_EXPECTED_METRICS_PATH = Path(__file__).resolve().parent / "expected_metrics.json"
_DEFAULT_DATA_URL = os.environ.get(
    "OMICSCLAW_DLPFC_151673_URL",
    # Anchored to spatialLIBD's published DLPFC dataset; mirror via env var
    # if the source moves.
    "https://research.libd.org/spatialLIBD/151673.h5ad",
)


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


def _ari_against_ground_truth(consensus_tsv: Path, gt_csv: Path, observation_col: str) -> float:
    import pandas as pd
    from sklearn.metrics import adjusted_rand_score

    consensus = pd.read_csv(consensus_tsv, sep="\t").set_index(observation_col)
    gt = pd.read_csv(gt_csv).set_index(observation_col)
    common = consensus.index.intersection(gt.index)
    if common.empty:
        raise RuntimeError("consensus and ground-truth share no observation ids")
    return float(
        adjusted_rand_score(gt.loc[common].iloc[:, 0], consensus.loc[common].iloc[:, 0])
    )


def _evaluate(
    output_dir: Path,
    *,
    consensus_ari: float,
    member_aris: dict[str, float],
    expected: dict,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    survived_methods = list(member_aris.keys())
    if len(survived_methods) < expected["min_members_surviving"]:
        failures.append(
            f"only {len(survived_methods)}/{len(expected['members_required'])} members survived; "
            f"need ≥ {expected['min_members_surviving']}"
        )
    best_member_ari = max(member_aris.values()) if member_aris else 0.0
    noise = float(expected["consensus_ari_noise_floor"])
    if consensus_ari < best_member_ari - noise:
        failures.append(
            f"consensus ARI {consensus_ari:.4f} < best member ARI {best_member_ari:.4f} - "
            f"noise floor {noise}"
        )
    abs_floor = float(expected["consensus_ari_min_absolute"])
    if consensus_ari < abs_floor:
        failures.append(
            f"consensus ARI {consensus_ari:.4f} below absolute floor {abs_floor}"
        )
    return (not failures, failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DLPFC 151673 consensus-domains benchmark")
    parser.add_argument(
        "--output-dir",
        default=str(Path(os.environ.get("TMPDIR", "/tmp")) / "consensus_benchmark_151673"),
    )
    parser.add_argument(
        "--data-url",
        default=_DEFAULT_DATA_URL,
        help="URL to fetch sample 151673 from. Override for mirrors.",
    )
    parser.add_argument("--ground-truth", default=None, help="Ground-truth CSV path")
    parser.add_argument("--observation-col", default="observation")
    parser.add_argument("--force-refetch", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup without running consensus (CI smoke).",
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
        print(f"[dry-run] expected metrics: {expected}")
        return 0

    _maybe_fetch(h5ad_path, args.data_url, args.force_refetch)
    cli = _resolve_consensus_cli()

    members_csv = ",".join(expected["members_required"])
    consensus_cmd = [
        sys.executable,
        str(cli),
        "--input",
        str(h5ad_path),
        "--output",
        str(output_dir),
        "--members",
        members_csv,
        "--operator",
        expected["operator"],
        "--non-interactive",
        "--seed",
        "0",
    ]
    LOGGER.info("Running: %s", " ".join(consensus_cmd))
    proc = subprocess.run(consensus_cmd, check=False)
    if proc.returncode != 0:
        LOGGER.error("consensus-domains exited with %d", proc.returncode)
        return proc.returncode

    consensus_tsv = output_dir / "consensus_labels.tsv"
    if args.ground_truth is None:
        LOGGER.warning(
            "No --ground-truth provided; skipping ARI assertion. "
            "Pass a layer-annotations CSV to enable the verified-vs-published check."
        )
        return 0

    consensus_ari = _ari_against_ground_truth(
        consensus_tsv, Path(args.ground_truth), args.observation_col
    )
    member_aris: dict[str, float] = {}
    for method in expected["members_required"]:
        member_dir = output_dir / method
        member_csv = member_dir / "figure_data" / "spatial_full.csv"
        if not member_csv.exists():
            LOGGER.warning("member %s missing artifact %s; skipping ARI", method, member_csv)
            continue
        import pandas as pd
        from sklearn.metrics import adjusted_rand_score

        member_df = pd.read_csv(member_csv).set_index("observation")["spatial_domain"]
        gt = pd.read_csv(Path(args.ground_truth)).set_index(args.observation_col)
        common = member_df.index.intersection(gt.index)
        member_aris[method] = float(
            adjusted_rand_score(gt.loc[common].iloc[:, 0], member_df.loc[common])
        )

    ok, failures = _evaluate(
        output_dir,
        consensus_ari=consensus_ari,
        member_aris=member_aris,
        expected=expected,
    )
    summary = {
        "consensus_ari": consensus_ari,
        "member_aris": member_aris,
        "passed": ok,
        "failures": failures,
    }
    (output_dir / "benchmark_result.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
