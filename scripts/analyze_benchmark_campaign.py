#!/usr/bin/env python3
"""Analyze one pre-registered Skill lifecycle Benchmark Campaign."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from omicsclaw.skill.benchmark_campaign import (  # noqa: E402
    analyze_campaign,
    load_campaign_records,
    load_campaign_spec,
    render_campaign_markdown,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze a fixed case x condition x repeat benchmark matrix with a "
            "strict denominator."
        )
    )
    parser.add_argument("spec", type=Path, help="Campaign JSON specification")
    parser.add_argument("results", type=Path, help="Campaign result JSONL")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec = load_campaign_spec(args.spec)
    records = load_campaign_records(args.results)
    summary = analyze_campaign(spec, records)
    json_text = json.dumps(
        summary,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    markdown_text = render_campaign_markdown(summary)

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json_text, encoding="utf-8")
    if args.markdown_output is not None:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_text, encoding="utf-8")
    if args.json_output is None and args.markdown_output is None:
        print(json_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
