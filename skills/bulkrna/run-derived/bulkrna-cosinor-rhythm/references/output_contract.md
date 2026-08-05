## Output Structure

```
output_directory/
├── report.md
├── result.json
└── reproducibility/
    └── commands.sh
```

## File contents

<!--
List ONLY the files the script actually writes.  PR-eval-2 added a lint
check that fails when a non-framework path mentioned here does not appear
in the script.  Framework files (report.md, result.json, commands.sh,
processed.h5ad, …) are exempt — they are written by the common report
helper.
-->

- `report.md` — Markdown summary written by the common report helper.
- `result.json` — standardised result envelope (`summary` + `data` keys).
- `reproducibility/commands.sh` — replay log for the run.

## Notes

Replace these placeholders with the script's actual writes
(e.g. `tables/<name>.csv`, `figures/<name>.png`) before relying on the
contract.  Downstream skills that read this output should be linked from
SKILL.md's `## See also` section.
