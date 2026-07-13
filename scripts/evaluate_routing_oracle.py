#!/usr/bin/env python3
"""Evaluate the deterministic 8-domain routing oracle.

Exit codes:
  0  oracle valid and every metric meets its threshold
  1  quality threshold failure
  2  invalid oracle or evaluator setup error
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ORACLE = REPO_ROOT / "tests" / "fixtures" / "routing_oracle" / "v1.json"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omicsclaw.skill.routing_oracle import (  # noqa: E402
    evaluate_routing_oracle,
    load_routing_oracle,
)


def _human_report(payload: dict) -> str:
    lines = [
        f"Routing oracle {payload['oracle_version']}: "
        + ("PASS" if payload["passed"] else "FAIL"),
        "",
        "Metrics:",
    ]
    thresholds = payload["thresholds"]
    for name, value in payload["metrics"].items():
        comparator = "<=" if name == "hallucinated_alias_rate" else ">="
        lines.append(
            f"- {name}: {value:.3f} (required {comparator} {thresholds[name]:.3f})"
        )
    lines.extend(["", "Per domain:"])
    for domain, metrics in payload["per_domain"].items():
        lines.append(
            f"- {domain}: top1={metrics['precision_at_1']:.3f}, "
            f"top3={metrics['top3_recall']:.3f}, "
            f"domain={metrics['domain_accuracy']:.3f}, "
            f"decision={metrics['decision_accuracy']:.3f}"
        )
    failures = payload["validation_errors"] + payload["threshold_failures"]
    failed_cases = [case for case in payload["cases"] if not case["passed"]]
    if failures or failed_cases:
        lines.extend(["", "Failures:"])
        lines.extend(f"- {failure}" for failure in failures)
        for case in failed_cases:
            lines.append(
                f"- {case['id']}: expected={list(case['expected_skills']) or ['no_skill']} "
                f"observed={case['observed_skill'] or 'no_skill'} "
                f"domain={case['observed_domain'] or 'none'} "
                f"coverage={case['observed_coverage'] or 'none'}"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    parser.add_argument("--output", type=Path, help="Also write the JSON report to this path")
    args = parser.parse_args(argv)

    try:
        oracle = load_routing_oracle(args.oracle)
        report = evaluate_routing_oracle(oracle)
    except Exception as exc:  # noqa: BLE001 - CLI setup errors map to exit 2
        print(f"routing oracle setup error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    payload = report.to_dict()
    rendered_json = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered_json, encoding="utf-8")
    if args.json:
        sys.stdout.write(rendered_json)
    else:
        sys.stdout.write(_human_report(payload))

    if report.validation_errors:
        return 2
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
