"""/interpret slash command — Slice 10.B per ADR 0012 IMPLEMENTATION_PLAN.md.

Pure parser + /run argstring builder. The dispatch site in
interactive.py is intentionally a 3-liner that calls these helpers,
keeping the parsing logic test-friendly and surface-independent.

Usage::

    /interpret <typed_run_dir> [--tissue brain|immune|kidney|liver] [--no-llm] [--output <dir>]
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omicsclaw.routing.consensus_interpret_hint import is_typed_consensus_run


@dataclass(frozen=True)
class InterpretCommand:
    """Parsed /interpret arguments, ready to be converted to a /run argstring."""

    typed_run_dir: Path
    output_dir: Path
    tissue: str | None
    no_llm: bool


_USAGE = (
    "Usage: /interpret <typed_run_dir> "
    "[--tissue brain|immune|kidney|liver] [--no-llm] [--output <dir>]"
)


def parse_interpret_command(arg: str) -> InterpretCommand | str:
    """Parse ``arg`` into an :class:`InterpretCommand`.

    Returns a string on parse failure (the message to display to the
    user); never raises for user-input errors.
    """
    if not arg or not arg.strip():
        return f"missing typed_run_dir.\n{_USAGE}"

    tokens = arg.split()
    typed_run_dir_str = tokens[0]
    tissue: str | None = None
    no_llm = False
    output_dir_str: str | None = None

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--tissue" and i + 1 < len(tokens):
            tissue = tokens[i + 1]
            i += 2
        elif tok == "--no-llm":
            no_llm = True
            i += 1
        elif tok == "--output" and i + 1 < len(tokens):
            output_dir_str = tokens[i + 1]
            i += 2
        else:
            return f"unknown argument {tok!r}.\n{_USAGE}"

    typed_run_dir = Path(typed_run_dir_str).resolve()
    if not typed_run_dir.exists():
        return f"typed_run_dir does not exist: {typed_run_dir}\n{_USAGE}"
    if not is_typed_consensus_run(typed_run_dir):
        return (
            f"{typed_run_dir} is not a typed consensus run directory "
            f"(missing one of plan.json / consensus_labels.tsv / "
            f"member_scores.csv / cross_method_nmi.csv). "
            f"Did you run consensus-domains or sc-consensus-clustering first?"
        )

    output_dir = (
        Path(output_dir_str).resolve()
        if output_dir_str
        else (typed_run_dir.parent / f"{typed_run_dir.name}_interpreted").resolve()
    )

    return InterpretCommand(
        typed_run_dir=typed_run_dir,
        output_dir=output_dir,
        tissue=tissue,
        no_llm=no_llm,
    )


def to_run_command_string(cmd: InterpretCommand) -> str:
    """Convert to a /run argstring (skill name + flags).

    Suitable for handing to the existing /run dispatch:
    ``_handle_run(to_run_command_string(cmd))``.
    """
    parts: list[str] = [
        "consensus-interpret",
        "--input", str(cmd.typed_run_dir),
        "--output", str(cmd.output_dir),
    ]
    if cmd.no_llm:
        parts.append("--no-llm")
    if cmd.tissue:
        parts.extend(["--tissue", cmd.tissue])
    return " ".join(parts)
