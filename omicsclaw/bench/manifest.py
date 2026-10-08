"""A benchmark campaign as a TOML manifest: cases, arms, models and budget.

A manifest names what is run and in which order. It holds no data: each
case's public files and its oracle live under a cases root given at run
time, and everything a campaign produces goes to an output directory given
the same way.

.. code-block:: toml

    name = "example"
    seed = 7          # shuffles the run order
    repeats = 2

    [budget]
    wall_clock_s = 600
    max_turns = 30    # optional
    kill_grace_s = 20 # optional

    [audit]
    patterns = ["pip install"]  # regular expressions, optional

    [[models]]
    id = "m1"
    provider = "deepseek"
    model = "deepseek-v4-flash"

    [[arms]]
    id = "oc"
    adapter = "omicsclaw"
    skills_dir = "../../skills"          # optional
    permission_rules = "rules.json"      # optional
    env = { OMICSCLAW_SKILL_ENV = "probe" }
    options = { python = "/opt/conda/envs/OmicsClaw/bin/python" }

    [[cases]]
    id = "sum-a"
    prompt = "Add the numbers in data/numbers.txt ..."
    deliverables = ["output/answer.json"]
    grader = "omicsclaw.bench.example:SumGrader"

Relative paths are resolved against the manifest's directory. An arm that
sets ``provider`` or ``model`` runs with that one model only; every other
arm runs once per entry of ``[[models]]``. An unknown key anywhere is an
error.
"""

from __future__ import annotations

import random
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

__all__ = [
    "Arm",
    "Budget",
    "Case",
    "Manifest",
    "ManifestError",
    "Model",
    "RunSpec",
    "load_manifest",
]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ManifestError(ValueError):
    """A manifest that cannot be read as written."""


@dataclass(frozen=True)
class Model:
    """One model a run is made with.

    :param id: Short name used in paths and result rows.
    :param provider: Provider name handed to the adapter; empty leaves the
        choice to the agent's own environment.
    :param model: Model name handed to the adapter; empty likewise.
    """

    id: str
    provider: str = ""
    model: str = ""


@dataclass(frozen=True)
class Arm:
    """One experimental condition.

    :param id: Short name used in paths and result rows.
    :param adapter: A built-in adapter name or ``module:attribute``.
    :param skills_dir: Skills root for the agent, or ``None`` for the
        adapter's default.
    :param env: Extra environment variables for the agent process.
    :param permission_rules: A permission rule file, or ``None``.
    :param options: Adapter-specific settings.
    :param model: The arm's own model; ``None`` runs every campaign model.
    """

    id: str
    adapter: str
    skills_dir: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    permission_rules: Path | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    model: Model | None = None


@dataclass(frozen=True)
class Case:
    """One task.

    :param id: Directory name under the cases root, which holds the case's
        ``public/`` and ``oracle/`` folders.
    :param prompt: The whole instruction given to the agent.
    :param deliverables: Workspace-relative files the agent must produce.
    :param grader: ``module:attribute`` naming the case's grader.
    """

    id: str
    prompt: str
    deliverables: tuple[str, ...]
    grader: str = ""


@dataclass(frozen=True)
class Budget:
    """Limits applied to every run.

    :param wall_clock_s: Seconds a run may take before it is stopped.
    :param kill_grace_s: Seconds between the polite stop and the forced one.
    :param max_turns: Model-call ceiling handed to adapters that support
        one, or ``None`` for the agent's default.
    """

    wall_clock_s: float
    kill_grace_s: float = 20.0
    max_turns: int | None = None


@dataclass(frozen=True)
class RunSpec:
    """One run: an arm, a model, a case and a repeat number (from 1)."""

    arm: Arm
    model: Model
    case: Case
    repeat: int

    @property
    def key(self) -> str:
        """``<arm>/<model>/<case>/r<repeat>``, also the run's relative path."""
        return f"{self.arm.id}/{self.model.id}/{self.case.id}/r{self.repeat}"

    def identity(self) -> dict[str, Any]:
        """The fields every result row carries to say which run it is."""
        return {
            "run": self.key,
            "arm": self.arm.id,
            "model_id": self.model.id,
            "provider": self.model.provider,
            "model": self.model.model,
            "case": self.case.id,
            "repeat": self.repeat,
        }


@dataclass(frozen=True)
class Manifest:
    """A parsed campaign manifest. See the module docstring for the format."""

    name: str
    path: Path
    seed: int
    repeats: int
    budget: Budget
    models: tuple[Model, ...]
    arms: tuple[Arm, ...]
    cases: tuple[Case, ...]
    audit_patterns: tuple[str, ...] = ()

    def models_for(self, arm: Arm) -> tuple[Model, ...]:
        """The models *arm* runs with: its own, or every campaign model."""
        return (arm.model,) if arm.model is not None else self.models

    def runs(self) -> tuple[RunSpec, ...]:
        """Every run of the campaign, in execution order.

        The order depends only on the manifest. Blocks of one case and one
        repeat are shuffled with :attr:`seed`; inside a block every arm and
        model appears once, also shuffled, so the arms of one case run next
        to each other and no arm always goes first.
        """
        rng = random.Random(self.seed)
        blocks = [
            (case, repeat)
            for case in self.cases
            for repeat in range(1, self.repeats + 1)
        ]
        rng.shuffle(blocks)
        ordered: list[RunSpec] = []
        for case, repeat in blocks:
            cells = [
                RunSpec(arm, model, case, repeat)
                for arm in self.arms
                for model in self.models_for(arm)
            ]
            rng.shuffle(cells)
            ordered.extend(cells)
        return tuple(ordered)


def load_manifest(path: Path | str) -> Manifest:
    """Read and validate a manifest file.

    :param path: The TOML file.
    :returns: The parsed manifest, with relative paths made absolute.
    :raises ManifestError: The file is missing, is not valid TOML, or holds
        an unknown key, a bad identifier, a duplicate, an unsafe deliverable
        path, or an arm with no model to run.
    """
    source = Path(path).expanduser().resolve()
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read {source}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{source} is not valid TOML: {exc}") from exc

    base = source.parent
    _known(raw, "manifest", {
        "name", "seed", "repeats", "budget", "audit", "models", "arms", "cases",
    })
    name = _identifier(raw.get("name", ""), "name")
    repeats = _integer(raw.get("repeats", 1), "repeats", minimum=1)
    seed = _integer(raw.get("seed", 0), "seed")
    budget = _budget(raw.get("budget"))
    audit = _table(raw.get("audit", {}), "audit")
    _known(audit, "audit", {"patterns"})
    patterns = _patterns(audit.get("patterns", []))

    models = tuple(_model(entry) for entry in _tables(raw.get("models", []), "models"))
    _unique([model.id for model in models], "model")
    arms = tuple(_arm(entry, base) for entry in _tables(raw.get("arms", []), "arms"))
    _unique([arm.id for arm in arms], "arm")
    cases = tuple(
        _case(entry, base) for entry in _tables(raw.get("cases", []), "cases")
    )
    _unique([case.id for case in cases], "case")

    if not arms:
        raise ManifestError("the manifest names no arm")
    if not cases:
        raise ManifestError("the manifest names no case")
    for arm in arms:
        if arm.model is None and not models:
            raise ManifestError(
                f"arm {arm.id!r} has no model: add [[models]] or set the "
                "arm's provider and model"
            )
    return Manifest(
        name=name,
        path=source,
        seed=seed,
        repeats=repeats,
        budget=budget,
        models=models,
        arms=arms,
        cases=cases,
        audit_patterns=patterns,
    )


# ---- parsing helpers -------------------------------------------------------


def _known(table: Mapping[str, Any], where: str, allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ManifestError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _table(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{where} must be a table")
    return value


def _tables(value: Any, where: str) -> Sequence[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
        raise ManifestError(f"{where} must be an array of tables")
    return value


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ManifestError(
            f"{where}: {value!r} is not an identifier (letters, digits, "
            "'.', '_' and '-', starting with a letter or digit)"
        )
    return value


def _integer(value: Any, where: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{where} must be an integer")
    if minimum is not None and value < minimum:
        raise ManifestError(f"{where} must be at least {minimum}")
    return value


def _positive(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ManifestError(f"{where} must be a positive number")
    return float(value)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{where} must be a string")
    return value


def _unique(ids: Sequence[str], kind: str) -> None:
    seen: set[str] = set()
    for item in ids:
        if item in seen:
            raise ManifestError(f"duplicate {kind} id {item!r}")
        seen.add(item)


def _budget(value: Any) -> Budget:
    table = _table(value if value is not None else {}, "budget")
    _known(table, "budget", {"wall_clock_s", "kill_grace_s", "max_turns"})
    if "wall_clock_s" not in table:
        raise ManifestError("budget.wall_clock_s is required")
    max_turns = table.get("max_turns")
    return Budget(
        wall_clock_s=_positive(table["wall_clock_s"], "budget.wall_clock_s"),
        kill_grace_s=_positive(table.get("kill_grace_s", 20.0), "budget.kill_grace_s"),
        max_turns=(
            None
            if max_turns is None
            else _integer(max_turns, "budget.max_turns", minimum=1)
        ),
    )


def _patterns(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError("audit.patterns must be an array of strings")
    for pattern in value:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ManifestError(
                f"audit.patterns: {pattern!r} is not a regular expression: {exc}"
            ) from exc
    return tuple(value)


def _model(entry: Mapping[str, Any]) -> Model:
    _known(entry, "models", {"id", "provider", "model"})
    return Model(
        id=_identifier(entry.get("id", ""), "models.id"),
        provider=_text(entry.get("provider", ""), "models.provider"),
        model=_text(entry.get("model", ""), "models.model"),
    )


def _arm(entry: Mapping[str, Any], base: Path) -> Arm:
    _known(entry, "arms", {
        "id", "adapter", "provider", "model", "model_id", "skills_dir", "env",
        "permission_rules", "options",
    })
    arm_id = _identifier(entry.get("id", ""), "arms.id")
    where = f"arm {arm_id!r}"
    adapter = _text(entry.get("adapter", ""), f"{where}: adapter")
    if not adapter:
        raise ManifestError(f"{where}: adapter is required")
    env = _table(entry.get("env", {}), f"{where}: env")
    for key, value in env.items():
        if not isinstance(value, str):
            raise ManifestError(f"{where}: env.{key} must be a string")
    pinned: Model | None = None
    if "provider" in entry or "model" in entry:
        model_name = _text(entry.get("model", ""), f"{where}: model")
        pinned = Model(
            id=_identifier(
                entry.get("model_id") or _slug(model_name) or "default",
                f"{where}: model_id",
            ),
            provider=_text(entry.get("provider", ""), f"{where}: provider"),
            model=model_name,
        )
    elif "model_id" in entry:
        raise ManifestError(f"{where}: model_id needs provider or model")
    return Arm(
        id=arm_id,
        adapter=adapter,
        skills_dir=_path(entry.get("skills_dir"), base, f"{where}: skills_dir"),
        env=dict(env),
        permission_rules=_path(
            entry.get("permission_rules"), base, f"{where}: permission_rules"
        ),
        options=dict(_table(entry.get("options", {}), f"{where}: options")),
        model=pinned,
    )


def _case(entry: Mapping[str, Any], base: Path) -> Case:
    _known(entry, "cases", {"id", "prompt", "prompt_file", "deliverables", "grader"})
    case_id = _identifier(entry.get("id", ""), "cases.id")
    where = f"case {case_id!r}"
    if ("prompt" in entry) == ("prompt_file" in entry):
        raise ManifestError(f"{where}: give exactly one of prompt and prompt_file")
    if "prompt" in entry:
        prompt = _text(entry["prompt"], f"{where}: prompt")
    else:
        prompt_path = _path(entry["prompt_file"], base, f"{where}: prompt_file")
        try:
            prompt = prompt_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        except OSError as exc:
            raise ManifestError(f"{where}: cannot read {prompt_path}: {exc}") from exc
    if not prompt.strip():
        raise ManifestError(f"{where}: the prompt is empty")
    deliverables = entry.get("deliverables", [])
    if not isinstance(deliverables, list) or not deliverables:
        raise ManifestError(f"{where}: deliverables must be a non-empty array")
    return Case(
        id=case_id,
        prompt=prompt,
        deliverables=tuple(_relative(item, where) for item in deliverables),
        grader=_text(entry.get("grader", ""), f"{where}: grader"),
    )


def _path(value: Any, base: Path, where: str) -> Path | None:
    if value is None:
        return None
    text = _text(value, where)
    if not text:
        raise ManifestError(f"{where} is empty")
    path = Path(text).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _relative(value: Any, where: str) -> str:
    """A deliverable path that stays inside the workspace."""
    text = _text(value, f"{where}: deliverables")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ManifestError(
            f"{where}: deliverable {text!r} must be a relative path inside "
            "the workspace"
        )
    return path.as_posix()


def _slug(text: str) -> str:
    """*text* reduced to identifier characters, for a model's default id."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
