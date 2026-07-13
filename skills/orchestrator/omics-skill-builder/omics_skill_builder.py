#!/usr/bin/env python3
"""CLI wrapper for creating OmicsClaw skill scaffolds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from omicsclaw.skill.scaffolder import create_skill_scaffold  # noqa: E402

SKILL_NAME = "omics-skill-builder"
SKILL_VERSION = "0.5.1"


def parse_args() -> argparse.Namespace:
    if "--promote-from-latest" in sys.argv[1:]:
        raise SystemExit(
            "--promote-from-latest is disabled; provide the exact "
            "--source-analysis-dir to preserve run/session identity."
        )
    parser = argparse.ArgumentParser(description="Create an OmicsClaw skill scaffold.")
    parser.add_argument("--output", required=True, help="Directory for the scaffold report.")
    parser.add_argument("--request", help="Original user request describing the desired skill.")
    parser.add_argument("--skill-name", default="", help="Preferred hyphenated skill alias.")
    parser.add_argument(
        "--domain",
        default=None,
        choices=[
            "spatial",
            "singlecell",
            "genomics",
            "proteomics",
            "metabolomics",
            "bulkrna",
            "orchestrator",
        ],
        help=(
            "Target OmicsClaw domain.  Default: 'spatial' in --demo mode "
            "(the demo scaffold is intentionally a spatial CellCharter example); "
            "required otherwise."
        ),
    )
    parser.add_argument("--summary", default="", help="One-line skill summary.")
    parser.add_argument("--source-analysis-dir", default="", help="Promote a successful autonomous analysis output directory into the new skill.")
    parser.add_argument(
        "--from-paper", default="",
        help="Path to a text file with paper content; extracts sourced methodology "
        "candidates into the scaffold's hints (P5, mutually exclusive with "
        "--from-tool-docs / --source-analysis-dir).",
    )
    parser.add_argument(
        "--from-tool-docs", default="",
        help="Path to a text file with tool/methods documentation; same extraction "
        "pipeline as --from-paper.",
    )
    parser.add_argument(
        "--doc-ref", default="",
        help="DOI/URL/PMID for --from-paper/--from-tool-docs (falls back to the file name if omitted).",
    )
    parser.add_argument("--trigger-keyword", dest="trigger_keywords", action="append", default=[])
    parser.add_argument("--method", dest="methods", action="append", default=[])
    parser.add_argument("--input-format", dest="input_formats", action="append", default=[])
    parser.add_argument("--output-item", dest="primary_outputs", action="append", default=[])
    parser.add_argument("--no-tests", action="store_true", help="Do not generate a test stub.")
    parser.add_argument("--demo", action="store_true", help="Run a built-in scaffold example.")
    return parser.parse_args()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.from_paper and args.from_tool_docs:
        raise SystemExit("--from-paper and --from-tool-docs are mutually exclusive.")
    from_corpus = args.from_paper or args.from_tool_docs
    corpus_source_kind = "paper" if args.from_paper else ("tool_docs" if args.from_tool_docs else "paper")

    if args.demo:
        request = (
            "Create a reusable OmicsClaw skill for CellCharter-based spatial "
            "domain analysis on AnnData inputs."
        )
        skill_name = args.skill_name or "spatial-cellcharter-domains"
        domain = args.domain or "spatial"
        summary = args.summary or (
            "Spatial domain identification scaffold for CellCharter-based workflows."
        )
        trigger_keywords = args.trigger_keywords or ["cellcharter domains", "cellcharter spatial"]
        methods = args.methods or ["cellcharter"]
        input_formats = args.input_formats or ["AnnData with spatial coordinates"]
        primary_outputs = args.primary_outputs or ["processed.h5ad", "figures/domain_map.png"]
        source_analysis_dir = args.source_analysis_dir
    else:
        if not args.request:
            raise SystemExit("--request is required unless --demo is used.")
        if not args.domain:
            raise SystemExit("--domain is required unless --demo is used.")
        request = args.request
        skill_name = args.skill_name
        domain = args.domain
        summary = args.summary
        source_analysis_dir = args.source_analysis_dir
        trigger_keywords = args.trigger_keywords
        methods = args.methods
        input_formats = args.input_formats
        primary_outputs = args.primary_outputs

    result = create_skill_scaffold(
        request=request,
        domain=domain,
        skill_name=skill_name,
        summary=summary,
        source_analysis_dir=source_analysis_dir,
        input_formats=input_formats,
        primary_outputs=primary_outputs,
        methods=methods,
        trigger_keywords=trigger_keywords,
        create_tests=not args.no_tests,
        from_corpus=from_corpus,
        corpus_source_kind=corpus_source_kind,
        doc_ref=args.doc_ref,
    )

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    quarantined = bool(getattr(result, "quarantined", False))
    admission = "quarantined" if quarantined else "admitted"
    admission_note = (
        "The promoted code remains outside registry/routing until the required "
        "sandboxed demo gate or explicit human validation succeeds."
        if quarantined
        else "The scaffold is available under its governed lifecycle status."
    )

    scaffold_summary = f"""# Omics Skill Builder Scaffold

Created a new scaffolded OmicsClaw skill.

- Skill: `{result.skill_name}`
- Domain: `{result.domain}`
- Skill directory: `{result.skill_dir}`
- Admission: `{admission}`
- Registry refreshed: `{result.registry_refreshed}`

{admission_note}
"""

    report = """# Scaffold Report

The scaffold was created successfully.

Next steps:
1. Replace placeholder logic in the generated Python entrypoint.
2. Expand the generated test into real scientific validation.
3. Refine trigger keywords and parameter hints in the generated SKILL.md.
"""

    # The shared runner owns `output_dir/README.md`; this skill writes its
    # scaffold-specific summary under a non-clashing name.
    _write_text(output_dir / "SCAFFOLD_SUMMARY.md", scaffold_summary)
    _write_text(output_dir / "report.md", report)
    _write_text(
        output_dir / "reproducibility" / "commands.sh",
        (
            f"oc run omics-skill-builder --output {output_dir} "
            f"--request {json.dumps(request)} --domain {result.domain} "
            f"--skill-name {result.skill_name}\n"
        ),
    )
    (output_dir / "result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Created OmicsClaw skill scaffold at {result.skill_dir}")
    print(f"Summary written to {output_dir / 'result.json'}")


if __name__ == "__main__":
    main()
