<!-- AUTO-GENERATED from skill.yaml — do not edit by hand. -->
<!-- Regenerate: python scripts/generate_parameters_md.py <skill_dir> -->


# Parameters

## Allowed extra CLI flags

- `--all`
- `--alpha`
- `--beta`
- `--confirm-plan`
- `--max-class-frac`
- `--max-parallel`
- `--members`
- `--non-interactive`
- `--operator`
- `--pseudotime-methods`
- `--root-cell`
- `--root-cluster`
- `--run-id`
- `--seed`
- `--timeout`
- `--top-k`

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
