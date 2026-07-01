# OmicsClaw v2 Skill Template

This directory is a **human-copy starter** for new OmicsClaw skills. It is not
read by codegen — see [`docs/adr/0033-skill-template-is-human-copy-only.md`](../../docs/adr/0033-skill-template-is-human-copy-only.md).

The goal: `cp -r` this directory into the right `skills/<domain>/` location,
rename the placeholders, and you should be ~80% of the way to a gold-standard
skill like `skills/singlecell/scrna/sc-de` or `skills/spatial/spatial-de`.

## The v2 layout (ADR 0037)

A v2 skill is defined by ONE machine contract, `skill.yaml`, validated by
`omicsclaw.skill.schema`. Everything else is generated one-way from it:

- **`skill.yaml`** — the single source of truth (identity, `summary`,
  `interface`, `runtime`, `deps`). Hand-edit THIS file.
- **`SKILL.md`** — a narrative methodology card whose frontmatter header and
  `## Inputs & Outputs` block are GENERATED from `skill.yaml`
  (`scripts/generate_skill_md.py`); only the narrative sections are hand-written.
- **`references/parameters.md`** — GENERATED from `skill.yaml.interface.parameters`
  (`scripts/generate_parameters_md.py`).

There is no `parameters.yaml` sidecar in v2 — its fields moved into `skill.yaml`.

## Bootstrap steps

```bash
# 1. Copy and rename
cp -r templates/skill skills/<domain>/<my-new-skill>
cd skills/<domain>/<my-new-skill>
mv replace_me.py <my_new_skill>.py
mv tests/test_replace_me.py tests/test_<my_new_skill>.py

# 2. Edit the placeholders
#    - skill.yaml          resolve every `# TODO` (id/name/domain, summary,
#                          interface, runtime.entry, deps.python) — the machine
#                          contract and single source of truth
#    - <my_new_skill>.py   replace the synthetic-CSV demo with real I/O
#    - SKILL.md            write the narrative body sections (the frontmatter
#                          header + Inputs & Outputs block are generated)
#    - references/*.md     fill in methodology / output contract

# 3. Regenerate the derived artifacts from skill.yaml
python scripts/generate_skill_md.py       skills/<domain>/<my-new-skill>
python scripts/generate_parameters_md.py  skills/<domain>/<my-new-skill>
python scripts/audit_skill_requires.py --write   # finalize deps.python

# 4. Verify
python scripts/skill_lint.py skills/<domain>/<my-new-skill>
python <my_new_skill>.py --demo --output /tmp/<my-new-skill>_demo
pytest tests/
```

## What the lint enforces

`scripts/skill_lint.py` is the structural contract every v2 skill must pass.
The full rule set lives in the script; the high-leverage rules are:

| Surface | Rule |
|---|---|
| `skill.yaml` | Must validate against `omicsclaw.skill.schema` (`schema_version: 2`, known `domain`, `id`/`name`/`version`, `summary`, `runtime.entry`) |
| `skill.yaml` `summary.skip_when` | Must declare ≥ 1 rule (parity with the v1 "Skip when" description contract) |
| `skill.yaml` `runtime.entry` | Must resolve to a real file in the skill dir (unless `lifecycle.status: draft`) |
| `skill.yaml` `interface.parameters.allowed_extra_flags` | Must exactly match the `--flag` literals declared via `add_argument(...)` in the script (excluding the runner-blocked trio `--input`/`--output`/`--demo`); kebab-case only |
| `SKILL.md` body | ≤ 200 lines; must contain `## When to use`, `## Flow`, `## Gotchas`, `## Key CLI`, `## See also` (the `## Inputs & Outputs` block is generated) |
| `SKILL.md` Gotchas | Each non-empty bullet must anchor to a real code path (`<script>.py:LINE`), `result.json["key"]`, or a `tables/`/`figures/` filename that the script actually writes |
| `references/` | Must contain `methodology.md`, `output_contract.md`, `parameters.md` |
| `references/output_contract.md` | Every `tables/X.csv` / `figures/X.png` / etc. it mentions must appear as a substring in the script (or any sibling `_lib/*.py` it imports) |
| `references/parameters.md` | Must match the output of `scripts/generate_parameters_md.py` — regenerate after every `skill.yaml` edit |

## Soft conventions (not lint-enforced, but every gold skill does this)

### Shared helpers live in `skills/<domain>/_lib/`

When two skills in the same domain need the same utility (matrix-contract
validation, pseudobulk aggregation, gallery rendering, …), put it under
`skills/<domain>/_lib/` and import via `from skills.<domain>._lib.<module>`.
Do **not** put helpers under the skill's own directory unless they are
genuinely single-use.

The template is domain-agnostic and cannot scaffold this for you — see the
existing `skills/singlecell/_lib/` and `skills/spatial/_lib/` for shape.

### Real demo data lives in `data/`

The template's `replace_me.py` synthesises its demo in memory because that
keeps the template domain-agnostic and avoids committing binary fixtures.
When your skill needs a real demo (e.g. a small h5ad, a tiny VCF), drop it
under `<skill>/data/` and load it from `--demo`. See
`skills/singlecell/scrna/sc-de/data/pbmc3k_processed.h5ad` for shape.

### Optional R Enhanced visualisation layer

OmicsClaw has a three-tier visualisation flow: Python standard figures → R
Enhanced figures (`omicsclaw.py replot`) → parameter tuning. The R layer is
opt-in. If your skill exports `figure_data/*.csv` payloads and you want a
publication-quality R renderer, add:

```
<skill>/r_visualization/
├── <name>_publication_template.R   # consumes figure_data/, writes figures/r_enhanced/
└── README.md                       # input contract + renderer list
```

See `skills/spatial/spatial-de/r_visualization/` for shape. The template
deliberately does not scaffold this directory — leave
`references/r_visualization.md` as-is until you actually add a renderer.

## Reference skills

When in doubt, read these end-to-end:

- `skills/singlecell/scrna/sc-de/` — multi-method DE, AnnData I/O,
  pseudobulk path, R renderers.
- `skills/spatial/spatial-de/` — same DE problem in the spatial modality,
  illustrates the cross-domain shape.
