---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: replace-me-skill
description: Load when copying this directory to bootstrap a new OmicsClaw v2 skill (rename, fill in skill.yaml, regenerate). Skip when an existing skill already covers the request.
version: 0.1.0
author: OmicsClaw
license: MIT
emoji: "🔬"
tags:
- template
- scaffold
- v2
---

<!--
Authoring checklist (delete this comment block before committing):

  1. Copy: `cp -r templates/skill skills/<domain>/<my-new-skill>`, then
     `mv replace_me.py <my_new_skill>.py` and rename the tests/ file.
  2. Fill skill.yaml (the machine contract — the SINGLE source of truth):
     id/name/domain/emoji, summary.load_when + skip_when, interface,
     runtime.entry, deps.python.  The header + `## Inputs & Outputs` block
     below are GENERATED from it — never hand-edit them.
  3. Write the narrative sections below (When to use / Flow / Gotchas /
     Key CLI / See also) and the three `references/*.md` stubs.
  4. Implement: replace the synthetic-CSV demo in the script with real I/O.
  5. Regenerate + verify:
       python scripts/generate_skill_md.py       skills/<domain>/<my-new-skill>
       python scripts/generate_parameters_md.py  skills/<domain>/<my-new-skill>
       python scripts/skill_lint.py              skills/<domain>/<my-new-skill>
       pytest tests/

Full usage notes, lint rules, and soft conventions live in
`templates/skill/README.md`.
-->

# REPLACE_SKILL_NAME

## When to use

<!--
One short paragraph (3-6 lines).  Mirror skill.yaml's summary.load_when /
skip_when and explicitly call out the closest adjacent skill so the agent
knows when to redirect.
-->

The user has `<input shape>` and wants `<output shape>`.  Pick this skill
when `<distinguishing condition>`.  For `<adjacent capability>` use
`<sibling-skill>` instead.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

<!--
This section is REGENERATED from skill.yaml's `interface` (inputs + outputs).
Do NOT hand-edit it — declare inputs/outputs in skill.yaml, then run
`python scripts/generate_skill_md.py <skill_dir>` to refresh it.  The block
below is the placeholder shape the generator will overwrite.
-->

**Outputs**

- `tables/replace_me.csv`
- `report.md`
- `result.json`

## Flow

<!--
3-7 numbered steps, present-tense, anchor each to a `<file>.py:LINE` if it
helps reviewers verify the contract.  Don't recapitulate idiomatic Python.
-->

1. Load input (`--input <file>`) or generate a demo (`--demo`).
2. Validate required columns / `obs[X]` keys; raise `ValueError(...)` early.
3. Run the chosen `--method` backend.
4. Write `tables/<name>.csv` (`<script>.py:<L>`) + `report.md` + `result.json`.

## Gotchas

<!--
Empirically the highest-leverage section.  Each bullet should:
  * State the trap in the lead sentence.
  * Anchor to a code line (`<file>.py:LINE`) or output filename — lint at
    `scripts/skill_lint.py::_check_gotchas_anchors` enforces this.
  * Explain WHY (the reason the trap exists), not just WHAT.

Skip obvious things — Python-101 advice or framework-standard behaviour.
The bar is "would the agent get this wrong without this instruction?".
-->

- _None yet — append as failure modes are reported._

## Key CLI

```bash
# Demo
python omicsclaw.py run REPLACE_SKILL_NAME --demo --output /tmp/REPLACE_SKILL_NAME_demo

# Real input
python omicsclaw.py run REPLACE_SKILL_NAME \
  --input <data.ext> --output results/ \
  --method <method-name>
```

## See also

- `references/parameters.md` — every CLI flag, per-method tunables
- `references/methodology.md` — the WHY behind the algorithm
- `references/output_contract.md` — `tables/X.csv` + `result.json` schema
- Adjacent skills: `<sibling-1>` (upstream), `<sibling-2>` (parallel), `<sibling-3>` (downstream)
