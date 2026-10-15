#!/usr/bin/env bash
# End-to-end smoke test for 0_setup_env.sh.
# Builds a fresh env in a throwaway name and checks imports and sidecars.
# Run on a clean machine before tagging a release.
#
# Usage:
#   bash scripts/smoke_test_setup.sh                   # auto-named throwaway env
#   bash scripts/smoke_test_setup.sh OmicsClaw_my      # custom env name
#
# Side effects: creates and removes the named test environment.

set -euo pipefail

ENV_NAME="${1:-OmicsClaw_smoketest_$(date +%s)}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cleanup() {
    echo "[smoke] removing test env $ENV_NAME"
    mamba env remove -n "$ENV_NAME" -y >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$PROJECT_ROOT"

echo "[smoke] === Stage 1: bash 0_setup_env.sh $ENV_NAME ==="
time bash 0_setup_env.sh "$ENV_NAME"

echo "[smoke] === Stage 2: import smoke ==="
mamba run --no-capture-output -n "$ENV_NAME" python - <<'PY'
import importlib
mods = [
    "scanpy", "anndata", "squidpy", "scvi", "scvelo", "cellrank",
    "torch", "harmonypy", "bbknn", "scanorama",
    "celltypist", "cellphonedb", "gseapy", "pydeseq2",
    "scrublet", "doubletdetection", "arboreto", "palantir",
    "multiqc", "kb_python",
    "esda", "libpysal", "pysal", "ot",
    # thin pip residue (some are optional / may not import without input)
    "SpaGCN", "GraphST", "tangram",
    # omicsclaw itself
    "omicsclaw",
]
failed = []
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  OK  {m}")
    except Exception as exc:  # noqa: BLE001 — we want all failures listed
        print(f"  FAIL {m}: {exc}")
        failed.append(m)
if failed:
    raise SystemExit(f"{len(failed)} import(s) failed: {failed}")
PY

echo "[smoke] === Stage 3: skill sidecar contracts ==="
mamba run --no-capture-output -n "$ENV_NAME" python - <<'PY'
from omicsclaw.skills import load_skills
from omicsclaw.skills.sidecar import validate_skill_dir

index = load_skills("skills")
assert len(index) and not index.skipped, index.skipped
for skill in index.skills:
    errors = validate_skill_dir(skill.directory)
    assert not errors, (skill.name, errors)
print(f"Validated {len(index)} skill sidecars")
PY

echo "[smoke] ✔ all stages passed"
