"""A toy suite for exercising the harness: add up a column of integers.

Each case gives the agent ``data/numbers.txt`` and asks for the sum in
``output/answer.json`` as ``{"sum": <integer>}``. The oracle is the sum.
The suite tests the harness and measures nothing about an agent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from .grade import Control, Grade, Submission

__all__ = ["ANSWER", "CASES", "SumGrader", "write_cases"]

ANSWER = "output/answer.json"
"""The deliverable every case of this suite declares."""

CASES: Mapping[str, tuple[int, ...]] = {
    "sum-a": (3, 14, 15, 92, 65),
    "sum-b": (-7, 20, 0, 1000, -13, 6),
}
"""The numbers of each case, by case id."""


def write_cases(root: Path) -> list[Path]:
    """Create the suite's case directories under *root*.

    Each case gets ``public/data/numbers.txt``, one integer per line, and
    ``oracle/truth.json``. Existing files are overwritten.

    :returns: The case directories, in case-id order.
    """
    written = []
    for case_id, numbers in sorted(CASES.items()):
        case = root / case_id
        public, oracle = case / "public" / "data", case / "oracle"
        public.mkdir(parents=True, exist_ok=True)
        oracle.mkdir(parents=True, exist_ok=True)
        (public / "numbers.txt").write_text(
            "".join(f"{number}\n" for number in numbers), encoding="utf-8"
        )
        (oracle / "truth.json").write_text(
            json.dumps({"sum": sum(numbers)}) + "\n", encoding="utf-8"
        )
        written.append(case)
    return written


class SumGrader:
    """Passes a submission whose ``sum`` equals the oracle's."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        """Compare ``output/answer.json`` with ``truth.json``.

        Anything but a JSON object with an integer ``sum`` fails with a
        score of ``0.0``. Neither ``detail`` nor ``metrics`` says what the
        sum is or how far off an answer was: grades are written under the
        output root, where a later attempt at the same case could read
        them.
        """
        truth = json.loads((oracle / "truth.json").read_text(encoding="utf-8"))["sum"]
        try:
            answer = json.loads(
                submission.deliverables[ANSWER].read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return Grade(
                False, 0.0, {"well_formed": False}, "the answer is not JSON"
            )
        value = answer.get("sum") if isinstance(answer, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            return Grade(
                False, 0.0, {"well_formed": False},
                "'sum' is missing or not an integer",
            )
        correct = value == truth
        return Grade(
            correct,
            1.0 if correct else 0.0,
            metrics={"well_formed": True},
            detail="" if correct else "the sum is wrong",
        )

    def controls(self) -> Sequence[Control]:
        """One correct answer and three wrong ones."""
        oracle = {"truth.json": '{"sum": 10}\n'}
        return (
            Control("correct", True, {ANSWER: '{"sum": 10}\n'}, oracle),
            Control("wrong-sum", False, {ANSWER: '{"sum": 11}\n'}, oracle),
            Control("not-json", False, {ANSWER: "ten\n"}, oracle),
            Control("sum-as-text", False, {ANSWER: '{"sum": "10"}\n'}, oracle),
        )
