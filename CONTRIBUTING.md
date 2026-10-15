# Contributing to OmicsClaw

We welcome contributions from anyone working in multi-omics analysis, bioinformatics, computational biology, or related fields.

---

## Add a skill

Copy `templates/skill/` into the appropriate domain, then edit `SKILL.md` and
`kernel.py`. The skill loader indexes `name`, `description`, `trigger` and
`tags`; names must be unique. A description states when to load the skill and
when to use an adjacent skill instead.

The body contains these headings:

- `## 1. Method Decision Matrix & When to Use`
- `## 2. How to Run (Idiomatic Upstream Patterns)`
- `## 3. Output Format & Verification Contract`
- `## 4. Gotchas & Troubleshooting`
- `## Dependencies`

Document upstream Python/R patterns, input assumptions, expected outputs and
scientific checks. The dependency section contains one comma-separated line
of backquoted package names. Keep the document under 200 lines.

`kernel.py` contains fewer than 80 lines, explicit `__all__` exports and
function docstrings. Keep imports inside functions and use literal defaults.
Module scope contains definitions and literals; annotations must not execute
code. The runtime validates the sidecar before registering its helpers for
the session's persistent Python kernel.

The former `_api.py`/CLI/SDK workflow has been retired. New skills do not need
those files. The complete template contract is in
[`templates/skill/README.md`](templates/skill/README.md).

## Verify a contribution

Use Python 3.11 or later. Follow the change-based verification levels in
[`SPEC.md`](SPEC.md#testing-risk-matched-verification). The framework suite is
under `tests/`; skill structure and isolated loading are checked with:

```bash
python -m pytest tests/skills -q -p no:cacheprovider
```

Structural validation does not establish scientific correctness. Add
behavioral tests for changed calculations, using explicit inputs and expected
scientific invariants. Regenerate the domain index when metadata changes:

```bash
OMICSCLAW_WRITE_SKILL_INDEX=1 python -m pytest tests/skills/test_domain_index_is_current.py
```

Keep generated reports under `build/` or outside the checkout. Record the
date, revision (including local changes), interpreter and command with each
verification. Remove reports for retired implementations; preserve fixtures
and intentional golden files.

## Code and pull requests

Install the framework into an existing Python 3.11+ environment for ordinary
development. `0_setup_env.sh` is the optional full R/CLI/science installation;
Python environment provisioning does not replace that stack. Its smoke script
checks imports and sidecar contracts rather than invoking retired skill CLIs.

Use type hints, `pathlib` and the existing package boundaries. Framework code
must not import skill modules. Skill helpers keep optional imports local and
must not import the framework. Keep credentials out of source, test data and
commit messages.

Describe the problem, resulting behavior and validation in a pull request.
For larger changes, include review order, mechanical changes and relevant
limits. Update `CHANGELOG.md` for meaningful milestones and apply the
repository's humanizer instructions to prose.

## Supported Domains

For an always-current count, run `make list` — it reports what
`omicsclaw/skills/` actually indexed, and how many files it found and
refused. The routing table in [`OMICSCLAW.md`](OMICSCLAW.md) is maintained by hand.

| Domain | Directory |
|--------|-----------|
| Spatial Transcriptomics | `skills/spatial/` |
| Single-Cell Omics | `skills/singlecell/` |
| Genomics | `skills/genomics/` |
| Proteomics | `skills/proteomics/` |
| Metabolomics | `skills/metabolomics/` |
| Bulk RNA-seq | `skills/bulkrna/` |
| Literature | `skills/literature/` |

### Keeping skill-derived docs in sync

`SKILL.md` is the single source of truth. The generators that used to
write headers, catalogues and routing tables from a `skill.yaml` machine
contract were deleted with the old skill system, along with `skill.yaml`
itself — a generator nobody runs is how the domain indexes silently
rotted, so the one derived document left is guarded by a test instead:

```bash
# Check nothing drifted, and that the tree loads with nothing skipped
pytest tests/skills

# Regenerate skills/<domain>/INDEX.md after adding or editing a skill
OMICSCLAW_WRITE_SKILL_INDEX=1 pytest tests/skills/test_domain_index_is_current.py
# or: make skill-index
```

The routing table in `OMICSCLAW.md` is updated by hand; verify a count with
`find skills/<domain> -name SKILL.md | wc -l`.

### Keeping `## Dependencies` complete

A skill's `## Dependencies` section should list every Python package its
helpers and documented upstream code need, including optional backends.

These used to be a `requires:` frontmatter key, generated and checked by
`scripts/audit_skill_requires.py`. That script, the key and the `skill.yaml`
it read were deleted with the old skill system. The list is hand-maintained;
`tests/skillenv/test_skill_requires_are_declared.py` checks declared backends against lazy imports in `kernel.py`. With
`skill_env=install`, it also limits which packages the approval-gated
`install_skill_deps` tool may install into an overlay.

**When you add an algorithm/backend to a skill:**
1. Register it in `DEPENDENCIES` in `omicsclaw/skillenv/deps_registry.py`, keyed by its PyPI
   name as spelled in `## Dependencies`, with a pure-literal value:
   `module` (import name, or R package name), `kind` (`"pip"`, `"git"` or
   `"r"`), `install` (for `pip`, exactly `pip install <key> [<also>…]`) and
   `description`, plus optional `also` (extra PyPI names installed together)
   and `alt_env` (a conda env that also counts as available). It is the
   single source of truth for backend name mapping and is read by AST, so no
   calls or lambdas; see AGENTS.md.
2. Add it to the right `pyproject.toml` extra or `environment.yml` Tier 4.
3. Add it to your `SKILL.md`'s `## Dependencies` line.

### Keeping the prompt index small

Only `name` and `description` reach the system prompt, once per skill, on
every turn; the full bodies are fetched only when needed.
That ratio is the whole point of the design, and a description that grows
into a paragraph spends context on every conversation whether or not your
skill is used.

Keep it to one load/skip sentence pair. `make list` shows the compact
per-domain rendering; `python -c "from omicsclaw.skills import load_skills;
print(len(load_skills('skills').summary()))"` gives the index's exact size
if you want to see what an edit cost.

The old `measure_routing_tokens.py` / `check_routing_budget.py` pair that
pinned a ceiling here was deleted: it measured the retired bot tool registry
and its own helper module was already missing.


## For AI Agents Contributing Skills

AI coding agents should follow the same workflow, plus:

1. For complex repository tasks, read [`README.md`](README.md) for project context and [`CHANGELOG.md`](CHANGELOG.md) for recent decisions
2. Read [`SPEC.md`](SPEC.md) for the repository maintenance and AI development contract
3. Read [`AGENTS.md`](AGENTS.md) for project structure and conventions
4. Read the target skill's `SKILL.md` before modifying code
5. Use a concise plan, root-cause debugging, focused tests, and verification evidence for non-trivial repository changes.
6. Use `make list` to verify skills load correctly, and check `i.skipped` is empty
7. Test at the level [`SPEC.md`](SPEC.md#testing-risk-matched-verification) sets under Risk-Matched Verification. A change to one skill needs that skill's tests and its example step; the full suite is for level 4 changes and runs in CI on every pull request
8. Add an entry at the top of [`CHANGELOG.md`](CHANGELOG.md) if the work introduces an important decision, milestone, or lasting contributor workflow change; change `README.md` only when a user-facing entry point, install step or headline feature changes, and keep its What's New to at most five items of one sentence each

## Skill Ideas We Need

**Spatial Transcriptomics:** 3D tissue reconstruction, multi-slice alignment

**Single-Cell:** Multi-modal integration (RNA + ATAC + protein), rare cell type detection

**Genomics:** Long-read variant calling, population genetics analysis

**Proteomics:** DIA-NN integration, PTM site prediction

**Metabolomics:** Compound identification, flux balance analysis

**Multi-Omics:** Cross-omics integration, multi-view factor analysis

## Questions?

Open an issue on [GitHub](https://github.com/TianGzlab/OmicsClaw/issues) or check the documentation.
The experimental SSH controller uses the shared application and manager
implementations. Keep protocol, model bridge and independent environment
adapters at that boundary; do not duplicate the agent or job state machines.
See `docs/engineering/2026-10-14-ssh-controller.md` for the profile and
dependency update contract.
