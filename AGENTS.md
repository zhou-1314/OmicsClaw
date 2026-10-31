# AGENTS.md

Coding agents working on this repository. The analysis agent's runtime contract is `OMICSCLAW.md`. Deleted subsystems are recorded in `docs/FRAMEWORK-REBUILD.md` and `CHANGELOG.md`.

## Before a change

- Read `README.md` and `CHANGELOG.md`, then `SPEC.md` and the code the task touches.
- Reply in the user's language. Stay concise.
- For a non-trivial change, write a short plan, debug from the failing behaviour, and verify with a command or a file read before reporting done.
- Record an important decision or milestone at the top of `CHANGELOG.md`. Edit `README.md` only when a user-facing entry point, install step, or headline feature changes. What's New stays at most five sentences.
- Run the humanizer skill (`.claude/skills/humanizer/SKILL.md`) in embedded mode on prose you add: comments, docstrings, docs, plans, README text, commit messages, and PR descriptions. Leave code, commands, paths, and identifiers as they are. If the skill is unavailable, apply that file's patterns directly.
- Check a claim against the current tree. An older plan does not describe current behaviour.

## Layout

`omicsclaw/` is one-way. Every layer may import `schema`. `schema` imports no other layer. Read `docs/FRAMEWORK-REBUILD.md` before changing a boundary.

- `schema/`, `provider/`, `engine/`, `tools/`, `context/`: shared types, model adapters, the loop, tool dispatch, and prompt assembly.
- `omicsclaw/skills/`: discovers `SKILL.md` and registers `kernel.py`. Framework code reads skill files and does not import `skills.*`.
- `memory/`, `mcp/`, `planning/`, `permission/`, `sandbox/`, `hooks/`, `observability/`, `subagent/`: runtime services.
- `entry/`: CLI, Desktop, and channel surfaces, all built by `omicsclaw.entry.build_app`. `entry/ssh/` is the source-only SSH controller. Its limits are in `docs/engineering/2026-10-14-ssh-controller.md`.
- Top-level `skills/`: domain skills plus `skill-creator`. Author from `templates/skill/`.
- `omicsclaw/skillenv/deps_registry.py`: the literal backend map. Callers outside `skills/` read it with `ast.literal_eval`. No calls or lambdas in that module.

## Commands

`oc` takes a surface, not a subcommand. Deployment flags go before `--`. A surface flag may sit on either side. When a value looks like a flag: `oc cli -- --prompt --model`.

```bash
oc cli
oc desktop
oc channel --channels telegram
python -m pytest tests/<package> -p no:cacheprovider -q -o addopts=""
make test
make list
```

There is no `oc run`. A skill runs through the persistent Python tool after `use_skill`. From Python, `from omicsclaw.skills.sidecar import load_skill`.

Python is `>=3.11,<3.14` (`pyproject.toml`). On many machines `python3` is older. Use an interpreter in that range.

## Dependencies

- Core Python packages and PyPI-only extras live in `pyproject.toml`. `pip install -e .` is enough for `oc cli`. `pip install -e ".[channels]"` adds the messaging SDKs.
- R, bioinformatics CLIs, and the science stack live in `environment.yml`, installed by `bash 0_setup_env.sh`. This repository has no root `requirements.txt` entrypoint. GitHub-only R packages are installed inline by Tier 3 of that script.
- Optional analysis backends each have one literal entry in `DEPENDENCIES` (`omicsclaw/skillenv/deps_registry.py`), keyed by the PyPI name as spelled in the skill's `## Dependencies`. Names resolve by key, then PEP 503-normalised key, then `module`.
- Per-skill packages are that `SKILL.md`'s `## Dependencies` section. With `skill_env=install` and an approval, `install_skill_deps` installs only the names it was asked for, into an overlay, never the base environment. A `bash(pip install*)` rule does not allow this tool.
- `prompt-toolkit`, `fastapi`, and `uvicorn` come from `environment.yml`. Without the first, the REPL uses plain line input. Without the latter two, `oc desktop` exits 2 and names the `mamba` command.
- A full conda env may warn that `pygpcca==1.0.4` wants `jinja2==3.0.3` while the env keeps `jinja2>=3.1.5`. When the imports under test succeed, that single `pip check` line is metadata noise.
- `--env-supply true` mounts `manage_environments` (off by default) and can provision Python envs with uv after approval. It does not install R, Bioconductor, or bioinformatics CLIs.

## Skills

A skill is `SKILL.md` plus a definition-only `kernel.py`. `SKILL.md` is hand-edited and is the source of truth. `skills/<domain>/INDEX.md` is checked by `tests/skills/test_domain_index_is_current.py`. Regenerate it with `OMICSCLAW_WRITE_SKILL_INDEX=1 pytest tests/skills/test_domain_index_is_current.py`. The routing table in `OMICSCLAW.md` is maintained by hand. Update it when a skill is added or removed.

The loader reads four frontmatter keys. Any other key is ignored.

| Key | Required | Used by |
|---|---|---|
| `name` | yes | prompt index, `search_skills`, `use_skill` |
| `description` | yes | prompt index; this is what routing sees |
| `trigger` | no | `/skills` and `search_skills`. It never auto-fires a skill |
| `tags` | no | `/skills` and `search_skills` |

A header missing `name` or `description` is skipped. The reason is data on `SkillIndex.skipped`, not a log line. `trigger` may be a comma-separated scalar or a block sequence.

- A description says when to load the skill and when to use a neighbour, in one load sentence and one skip sentence. Only `name` and `description` are injected on every turn.
- Names are unique.
- The body has the four sections in `templates/skill/SKILL.md`, plus `## Dependencies`.
- `kernel.py` stays under 80 lines, sets `__all__`, documents public functions, and imports inside function bodies. The sidecar gate rejects executable module scope, decorators, dynamic defaults, and executable annotations. `use_skill` registers the helpers in the persistent Python kernel.
- A scientific claim needs a behaviour test. Importing the module is not acceptance.
- Skills are not slash commands. `/skills` lists the index.
- Contract tests and the authoring notes: `tests/skills/test_claude_science_contract.py`, `templates/skill/README.md`.
- To add a skill, copy `templates/skill/` into the domain and follow `CONTRIBUTING.md`. Run `python -m pytest tests/skills -q -p no:cacheprovider` and the behaviour tests you added.

Leave `skill.yaml`, generated catalogues, and `oc run` retired.

## Surfaces

Three surfaces share one `AgentApp`: `oc cli` (`omicsclaw/entry/cli/`), `oc desktop` (`omicsclaw/entry/desktop/`), and `oc channel` (`omicsclaw/entry/channel/`). Their behaviour is defined in `tests/entry/`.

- Desktop `*_SCHEMA_VERSION` values are a contract with the separate OmicsClaw-App repository. Changing one is a cross-repository milestone. This repository owns backend policy, execution, persistence, file mutation, and the HTTP contracts. Do not add React here, and do not move backend policy into the App. Paths and versions are in `omicsclaw/entry/desktop/wire_contract.py`.
- Every channel requires an owner allowlist and refuses to start without it. A sender outside the list produces no turn. Prefer one platform per process. `--channels` is an explicit list, and one bad credential stops every channel named on it. Per-platform variables are section 11 of `.env.example`. Feishu also requires `FEISHU_BOT_OPEN_ID`.
- `--permission-mode read-only` refuses every `task` call. A sub-agent is not read-only.
- Sessions live in `<workspace>/.omicsclaw/memory.db`, one file per workspace. `SqliteSessionStore.list` filters nothing, so a multi-user surface must not share that file and offer `/resume`. `--memory false` keeps conversations in process memory only.
- MCP servers come from `<workspace>/.mcp.json`, connect before the tool registry is built, and are named `mcp__{server}__{tool}`. In the default permission mode every MCP call asks for approval. Stdio servers inherit a minimal environment. There is no `oc mcp add`.
- `LLM_PROVIDER=custom` must honour `LLM_BASE_URL`, `OMICSCLAW_MODEL`, and `LLM_API_KEY`. An explicit `--provider` or `--model` wins. A malformed custom endpoint must say what is wrong.

REPL cards, grants (`y` once, `s` for the tool for the rest of the conversation, `a` for an exact allow rule), and slash commands live in `omicsclaw/entry/cli/` and `tests/entry/`. Read that code when changing them. Two rules are easy to get wrong from the outside:

- `s` includes sub-agents, and `/resume` restores those grants. It does not answer a card the gate marks `ask_every_time`: a dangerous-command pattern, an explicit `ask` rule, or a write under `.omicsclaw/` or to any `.env`. Those paths are asked in every mode except `bypass-all`.
- `s` on `bash` matches `--permission-mode auto-approve`: later `bash` calls skip the card unless a dangerous-command pattern matches. The sandbox is the boundary. An MCP tool whose argument is not named `command` gets no danger check, so `s` on it allows the tool.

## Safety

- No data upload. Network fetches go through `omicsclaw/tools/_websafety.py`.
- File tools resolve paths in `omicsclaw/tools/_workspace.py`. An escape is refused by comparing path components. A credential path is refused even inside the workspace.
- `omicsclaw/permission/` decides a tool call from `<workspace>/.omicsclaw/settings.json` and `--permission-mode`. `require_approval` fails closed.
- A parameter in a skill traces to that `SKILL.md` or a cited tool.

Sandbox backends and egress: `docs/core-features/sandbox.md`. Domain input contracts: `docs/engineering/domain-input-contracts.md`.

## Tests

Follow Risk-Matched Verification in `SPEC.md`. Pick the level from what the change can break. Do not run the full suite by default.

```bash
python -m pytest tests/entry -p no:cacheprovider -q -o addopts=""
```

Level 4 is everything `.github/workflows/eval.yml` runs. That workflow is `Eval CI`, on every pull request and push to `main`.

- Historical totals are not a baseline.
- Record the date, revision including local changes, interpreter, command, and result. Put generated reports under `build/` or outside the checkout.
- Other sessions write this tree. Before treating a red suite as yours, check `git status` for files you did not touch.
