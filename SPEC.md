# SPEC.md — OmicsClaw Repository Maintenance and Development Contract

This file captures the repository-level working contract for AI coding agents
and human contributors doing maintenance or development work in OmicsClaw.

## Agent Behavior

- Respond in the language the user uses, typically Chinese or English.
- Stay concise, practical, and execution-focused.
- Before any complex maintenance, refactor, or feature task, read `README.md`
  first to understand project context and prior decisions. Then read
  `AGENTS.md`, this `SPEC.md`, and any directly relevant docs or `SKILL.md`
  files.
- Verify claims from the codebase and current docs before acting on them.
- When you make an important decision or complete a meaningful milestone,
  update `README.md` while preserving its existing structure.

## File Conventions

- Treat the root `README.md` as the repository's living memory for goals,
  milestones, architecture changes, and contributor-facing workflow rules.
- Store durable design notes, completion summaries, and architecture records
  under the relevant `docs/` topic area.
- Keep local agent workflow notes outside the repository or under ignored
  paths.
- Use date prefixes on long-lived documents so they sort chronologically.
- Prefer extending existing docs and code paths over introducing new top-level
  files, helper scripts, or fallback branches without a concrete need.

## Development Workflow

OmicsClaw does not ship local agent workflow playbooks as tracked project
documentation. Keep such notes outside the repository or under ignored paths.

Typical workflow chaining:

1. If the task is multi-step or ambiguous, use the planning playbook first.
2. If behavior changes, use TDD unless the task is clearly exempt.
3. If something fails, switch to systematic debugging before proposing fixes.
4. Before claiming success, use completion verification.
5. For substantial or risky changes, use code review before merge or push.
6. When wrapping up branch work, use the branch-finish playbook.

Additional rules:

- Do not overengineer.
- Do not add fallback paths or backward-compatibility shims unless the user,
  public API, or repository contract requires them.
- Prefer the smallest clear change that solves the current problem.
- Verify the affected behavior before declaring work complete, at the level
  the next section sets.
- Treat planning, debugging discipline, and completion verification as
  required process guardrails for non-trivial changes.

## Testing: Risk-Matched Verification

How much you verify depends on what the change can break. Pick one of the
four levels below before you start and run that level's checks. A full local
run is required only at level 4.

| Level | The change | What to run |
| --- | --- | --- |
| 1. No behavior change | Prose only: docs, plans, comments, `CHANGELOG.md` | No test run. Check that every path, command and identifier the text names exists. If a test reads the file you edited (`.env.example`, a SKILL.md `## API` section, the skill index), run that test. |
| 2. Local | Code inside one package or one skill, with no change to what other code imports or to what the agent is shown | The tests you added or changed, plus that package's test directory. For a skill: its own tests and its example step. |
| 3. Shared | Code that several layers import (`skills/_sdk/`, `omicsclaw/entry/assembly.py`, configuration options, the tool table); prompt or contract text (`OMICSCLAW.md`, `SAFETY_RULES`, tool descriptions); moved or deleted files | The test directory of every layer the change reaches, the layering guards (`tests/*/test_*layer*.py`, `tests/sdk/test_boundary.py`, `tests/sdk/test_public_surface.py`), the top-level `tests/test_*.py`, and the scripted evals in `tests/evals`. Re-record `tests/entry/golden/` on purpose and read the diff. |
| 4. Contract, safety, release | The Desktop wire contract, the permission gate, the sandbox, provider adapters, dependency pins, CI workflows; merging to `main`, pushing, publishing a release | Everything `Eval CI` runs, plus `Desktop compatibility` when the Desktop surface is involved. Open a pull request and let the workflows run, or run the same selection locally from `.github/workflows/eval.yml`. A release also runs `make test-all`. |

At every level:

- Judge by what can break. A one-line edit to the permission gate is level 4.
- When two levels fit, take the higher one. Move up a level when a focused
  run fails in code you did not touch, or when the diff grows past the level
  you chose.
- Take a broad baseline at most once, before the change. After each edit,
  rerun what the edit touched.
- `Eval CI` runs on every pull request to `main` and on every push to it.
  Local verification at levels 1 to 3 does not repeat it.
- The tests do not cover a real model, real data, the sandbox or a messaging
  platform. If the change depends on one of these, run it for real or list
  it as unverified.
- Report the level you chose, the commands you ran with their results, and
  what you did not verify. A check you did not run is reported as not run.

## Repository Maintenance

- Do not commit local agent workflow notes; keep them under ignored paths.
- If the work changes contributor expectations, agent entrypoints, or project
  structure, reflect that in `README.md`, `AGENTS.md`, and `CONTRIBUTING.md`
  together rather than leaving instructions split-brain.
