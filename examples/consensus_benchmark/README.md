# consensus_benchmark — DLPFC 151673 hero benchmark

ADR 0011 §"DLPFC 151673 hero benchmark" — verifies that
``consensus-domains`` consensus ARI vs published cortical-layer ground
truth is no worse than the best participating single method, within a
2% noise floor:

```
ARI(consensus, gt)  ≥  max_i  ARI(method_i, gt)  −  0.02
```

## Why 151673

DLPFC sample **151673** (12-layer Visium replicate of human DLPFC; Maynard
et al. 2021) is the canonical Visium benchmark with manual cortex-layer
annotations. BANKSY, GraphST, SEDR, STAGATE, and SpaGCN have all
independently reported ARI numbers on this sample, so divergent
behaviour is easy to spot.

## Run

```bash
# Run the full benchmark (≈ 10-15 minutes including method-side training)
python examples/consensus_benchmark/run_dlpfc_151673.py \
    --output-dir /tmp/consensus_benchmark_151673
```

The script:
1. Fetches sample 151673 from SACCELERATOR's data corpus or a configured
   mirror. **DLPFC data is NOT vendored** into this repository; the
   first run downloads ≈50 MB and caches it under
   ``~/.cache/omicsclaw/dlpfc_151673/``.
2. Runs ``consensus-domains`` with five members
   (banksy / graphst / sedr / leiden / spagcn) and the ``kmode`` operator.
3. Computes ARI vs the ground-truth layer annotation for the consensus
   and every individual member.
4. Compares against ``expected_metrics.json`` and emits a non-zero exit
   code on regression.

## How to run as a test

The corresponding pytest module
(``tests/runtime/consensus/test_dlpfc_benchmark.py``) gates the full
network-attached benchmark behind the ``RUN_DLPFC_BENCHMARK=1``
environment variable. Local / no-network runs skip the assertion
entirely:

```bash
RUN_DLPFC_BENCHMARK=1 python -m pytest tests/runtime/consensus/test_dlpfc_benchmark.py
```

## Files

- ``run_dlpfc_151673.py`` — entry point
- ``expected_metrics.json`` — pass/fail bounds, with rationale per metric
