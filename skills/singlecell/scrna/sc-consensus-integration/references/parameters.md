<!-- AUTO-GENERATED from skill.yaml — do not edit by hand. -->
<!-- Regenerate: python scripts/generate_parameters_md.py <skill_dir> -->


# Parameters

## Allowed extra CLI flags

- `--members`
- `--integration-methods`
- `--resolution`
- `--batch-key`
- `--include-scvi`
- `--vote-baseline`
- `--all`
- `--confirm-plan`
- `--non-interactive`
- `--alpha`
- `--beta`
- `--max-class-frac`
- `--operator`
- `--seed`
- `--timeout`
- `--max-parallel`
- `--top-k`
- `--run-id`

## Per-method parameter hints

### `default`

**Core parameters:**

| name | default |
|---|---|
| `integration_methods` | `none,harmony,scanorama` |
| `resolution` | `1.0` |
| `batch_key` | `batch` |

**Tips:**
- Members are integration backends; `none` is the unintegrated X_pca baseline that exposes batch-artifact clusters.
- Add `--include-scvi` for the GPU/stochastic scVI member; serialise GPU members with `--max-parallel 1`.
