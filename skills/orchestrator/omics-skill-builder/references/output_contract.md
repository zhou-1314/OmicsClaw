## Output Structure

```
output_directory/
├── SCAFFOLD_SUMMARY.md
├── report.md
├── result.json
└── reproducibility/
    └── commands.sh

skills/<domain>/<skill-name>/
└── (formally admitted skill — written only after the admission gate is earned)

skills/.quarantine/<domain>/<skill-name>/
└── (promoted code whose gate was skipped because the environment could not validate it)
```

## File contents

- `output_dir/SCAFFOLD_SUMMARY.md` — human-readable summary of what was generated. Written at `omics_skill_builder.py:129`.
- `output_dir/report.md` — Markdown report mirroring the scaffold + parameters. Written at `omics_skill_builder.py:130`.
- `output_dir/reproducibility/commands.sh` — replay command including the original `--request` and resolved `--domain`. Written at `omics_skill_builder.py:132-137`.
- `output_dir/result.json` — `summary` includes the new skill's domain, name, paths, and trigger keywords. Written at `omics_skill_builder.py:139-141`.
- `skills/<domain>/<skill-name>/` — the formally admitted skill directory (skill.yaml + SKILL.md + references/ + tests/).
- `skills/.quarantine/<domain>/<skill-name>/` — the alternative destination for an unvalidated promoted bundle; `references/quarantine.md` records the durable gate evidence and the registry does not discover this path.

## Notes

- The resulting skill directory is under `skills/`, NOT under `--output`. Its exact path and admission state are reported in `SCAFFOLD_SUMMARY.md`.
- Repeatable flags: `--trigger-keyword`, `--method`, `--input-format`, `--output-item` (each can be passed multiple times via `action="append"`).
- `--source-analysis-dir <path>` promotes one explicitly identified autonomous-analysis output. Global `--promote-from-latest` selection is disabled because it can cross session/run boundaries.
