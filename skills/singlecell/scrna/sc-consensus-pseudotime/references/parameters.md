<!-- AUTO-GENERATED from parameters.yaml — do not edit by hand. -->
<!-- Regenerate: python scripts/generate_parameters_md.py <skill_dir> -->


# Parameters

## Allowed extra CLI flags

- `--members`
- `--pseudotime-methods`
- `--root-cluster`
- `--root-cell`
- `--operator`
- `--all`
- `--confirm-plan`
- `--non-interactive`
- `--alpha`
- `--beta`
- `--max-class-frac`
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
| `pseudotime_methods` | `dpt,palantir,via` |
| `root_cluster` | `` |

**Tips:**
- Members are pseudotime methods (dpt/palantir/via); each must emit a single global pseudotime.
- A shared root is required: pass --root-cluster <name> or --root-cell <id> so direction is pinned.
