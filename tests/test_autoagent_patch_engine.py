"""Tests for omicsclaw.autoagent.patch_engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omicsclaw.autoagent.edit_surface import EditSurface, build_spatial_domains_surface
from omicsclaw.autoagent.patch_engine import (
    FileDiff,
    Hunk,
    PatchPlan,
    apply_patch,
    backup_files,
    parse_patch_response,
    revert_files,
    validate_patch,
)


def _write_spatial_domains_fixture(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "spatial" / "spatial-domains"
    skill_dir.mkdir(parents=True, exist_ok=True)
    lib_dir = tmp_path / "skills" / "spatial" / "_lib"
    lib_dir.mkdir(parents=True, exist_ok=True)

    (skill_dir / "SKILL.md").write_text("### CellCharter\n", encoding="utf-8")
    (skill_dir / "spatial_domains.py").write_text(
        "def main():\n    summary = dispatch_method(args.method, adata)\n",
        encoding="utf-8",
    )
    (lib_dir / "domains.py").write_text(
        """def identify_domains_leiden(adata):
    marker = "leiden"
    return marker


def identify_domains_stagate(adata):
    marker = "stagate"
    return marker


def identify_domains_cellcharter(adata):
    marker = "cellcharter"
    return marker


def _cluster_fixed_k():
    return "fixed"


def _cluster_auto_k():
    return "auto"


def dispatch_method(method, adata):
    return method
""",
        encoding="utf-8",
    )


def _write_v2_skill_md(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "skills" / "test"
    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    target.write_text(
        """---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
name: test-skill
version: 1.0.0
---

# test-skill

## When to use

Use this workflow for test data.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.h5ad`

**Outputs**

- `result.json`

## Flow

1. Load the input.
2. Run the analysis.

## Gotchas

- Check input quality before interpreting results.
""",
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# Parse tests
# ---------------------------------------------------------------------------


class TestParsePatchResponse:
    def test_parse_basic(self):
        response = json.dumps({
            "patch_plan": {
                "target_files": ["skills/test/SKILL.md"],
                "description": "Update default threshold",
                "expected_improvements": ["Better QC"],
                "rollback_conditions": ["Retention < 5%"],
            },
            "diffs": [
                {
                    "file": "skills/test/SKILL.md",
                    "hunks": [
                        {
                            "old_code": "min_genes: 200",
                            "new_code": "min_genes: 300",
                        }
                    ],
                }
            ],
            "reasoning": "Higher threshold filters noise.",
        })
        plan = parse_patch_response(response)
        assert not plan.converged
        assert plan.target_files == ["skills/test/SKILL.md"]
        assert len(plan.diffs) == 1
        assert plan.diffs[0].hunks[0].old_code == "min_genes: 200"
        assert plan.reasoning == "Higher threshold filters noise."

    def test_parse_with_markdown_fences(self):
        response = "Here is my patch:\n```json\n" + json.dumps({
            "patch_plan": {"target_files": ["a.py"]},
            "diffs": [{"file": "a.py", "hunks": [
                {"old_code": "x = 1", "new_code": "x = 2"}
            ]}],
            "reasoning": "test",
        }) + "\n```"
        plan = parse_patch_response(response)
        assert len(plan.diffs) == 1

    def test_parse_convergence(self):
        response = json.dumps({
            "converged": True,
            "reasoning": "No further improvements possible.",
        })
        plan = parse_patch_response(response)
        assert plan.converged is True
        assert "No further" in plan.reasoning

    def test_parse_invalid_json(self):
        with pytest.raises(ValueError):
            parse_patch_response("this is not json at all")

    def test_parse_skips_noop_hunks(self):
        response = json.dumps({
            "patch_plan": {"target_files": ["a.py"]},
            "diffs": [{"file": "a.py", "hunks": [
                {"old_code": "same", "new_code": "same"},  # no-op
                {"old_code": "old", "new_code": "new"},  # real
            ]}],
            "reasoning": "test",
        })
        plan = parse_patch_response(response)
        assert len(plan.diffs[0].hunks) == 1
        assert plan.diffs[0].hunks[0].old_code == "old"

    def test_patch_plan_properties(self):
        plan = PatchPlan(
            target_files=["a.py", "b.py"],
            diffs=[
                FileDiff("a.py", [Hunk("x", "y"), Hunk("a", "b")]),
                FileDiff("b.py", [Hunk("c", "d")]),
            ]
        )
        assert plan.n_hunks == 3
        assert "2 file(s)" in plan.diff_summary
        assert "3 hunk(s)" in plan.diff_summary

    def test_to_dict(self):
        plan = PatchPlan(
            target_files=["a.py"],
            description="test",
            diffs=[FileDiff("a.py", [Hunk("old", "new")])],
            reasoning="why",
        )
        d = plan.to_dict()
        assert d["target_files"] == ["a.py"]
        assert d["diffs"][0]["hunks"][0]["old_code"] == "old"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidatePatch:
    def test_rejects_exact_frontmatter_hunk(self, tmp_path):
        _write_v2_skill_md(tmp_path)
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("version: 1.0.0", "version: 2.0.0")
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "YAML frontmatter" in result.error_summary

    def test_rejects_legacy_v1_frontmatter_hunk(self, tmp_path):
        skill_dir = tmp_path / "skills" / "legacy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: legacy-skill
description: Legacy runtime metadata.
---

# legacy-skill

## Flow

Run the legacy workflow.
""",
            encoding="utf-8",
        )
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/legacy/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/legacy/SKILL.md"],
            diffs=[
                FileDiff("skills/legacy/SKILL.md", [
                    Hunk(
                        "description: Legacy runtime metadata.",
                        "description: Replaced runtime metadata.",
                    )
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "YAML frontmatter" in result.error_summary

    def test_rejects_exact_generated_io_hunk(self, tmp_path):
        _write_v2_skill_md(tmp_path)
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("- File types: `.h5ad`", "- File types: `.csv`")
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "AUTO-GENERATED Inputs & Outputs" in result.error_summary

    def test_rejects_whitespace_normalized_frontmatter_hunk(self, tmp_path):
        _write_v2_skill_md(tmp_path)
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("  version:    1.0.0  ", "version: 2.0.0")
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "YAML frontmatter" in result.error_summary

    def test_rejects_hunk_spanning_generated_io_from_narrative_to_narrative(
        self,
        tmp_path,
    ):
        target = _write_v2_skill_md(tmp_path)
        content = target.read_text(encoding="utf-8")
        start = content.index("Use this workflow for test data.")
        end = content.index("2. Run the analysis.") + len("2. Run the analysis.")
        old_code = content[start:end]
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk(
                        old_code,
                        old_code.replace(
                            "Use this workflow for test data.",
                            "Use this improved workflow for test data.",
                        ),
                    )
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "AUTO-GENERATED Inputs & Outputs" in result.error_summary

    def test_rejects_hunk_that_breaks_generated_io_heading_boundary(self, tmp_path):
        _write_v2_skill_md(tmp_path)
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk(
                        "Use this workflow for test data.\n\n",
                        "Use this workflow for test data.",
                    )
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "protected SKILL.md structure" in result.error_summary

    def test_valid_patch(self, tmp_path):
        # Create file
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("min_genes: 200\nother: stuff")

        surface = EditSurface(
            max_level=1, project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("min_genes: 200", "min_genes: 300")
                ])
            ],
        )
        result = validate_patch(patch, surface)
        assert result.valid is True

    def test_rejects_ambiguous_whitespace_normalized_hunk(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text(
            """def first():
    value   = 1
    return value


def second():
    value =   1
    return value
""",
            encoding="utf-8",
        )
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff(
                    "skills/test/test.py",
                    [Hunk("value = 1\nreturn value", "return 1")],
                )
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "Ambiguous whitespace-normalized hunk" in result.error_summary
        assert "appears 2 times" in result.error_summary

    def test_rejects_diff_target_missing_from_target_files(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=[],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "target_files is missing canonical diff target" in result.error_summary

    def test_rejects_target_files_entry_without_diff(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "a.py").write_text("a = 1\n", encoding="utf-8")
        (skill_dir / "b.py").write_text("b = 2\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/a.py", "skills/test/b.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/a.py", "skills/test/b.py"],
            diffs=[
                FileDiff("skills/test/a.py", [Hunk("a = 1", "a = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "target_files has canonical target without diff" in result.error_summary
        assert "skills/test/b.py" in result.error_summary

    def test_rejects_duplicate_target_files_entries(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py", "skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "target_files contains duplicate canonical target" in result.error_summary
        assert "skills/test/test.py" in result.error_summary

    def test_rejects_target_files_aliases_for_same_canonical_target(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=[
                "skills/test/test.py",
                "skills/test/../test/test.py",
            ],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "target_files contains duplicate canonical target" in result.error_summary

    def test_matches_target_files_and_diffs_by_canonical_path(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/../test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is True

    def test_rejects_non_list_target_files(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files="skills/test/test.py",  # type: ignore[arg-type]
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "target_files must be a list of path strings" in result.error_summary

    def test_rejects_non_string_target_files_item(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=[123],  # type: ignore[list-item]
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "target_files[0] must be a non-empty path string" in result.error_summary

    @pytest.mark.parametrize(
        "invalid_target",
        ["", " skills/test/test.py", "skills/test/test.py "],
        ids=["empty", "leading-whitespace", "trailing-whitespace"],
    )
    def test_rejects_invalid_target_files_path_string(
        self,
        tmp_path,
        invalid_target,
    ):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=[invalid_target],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "target_files[0] must be a non-empty path string" in result.error_summary

    def test_rejects_non_string_diff_file_without_raising(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff(123, [Hunk("x = 1", "x = 10")]),  # type: ignore[arg-type]
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "diffs[0].file must be a non-empty path string" in result.error_summary

    def test_rejects_duplicate_file_diffs_for_same_target(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
                FileDiff("skills/test/test.py", [Hunk("y = 2", "y = 20")]),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "duplicate canonical target" in result.error_summary
        assert "skills/test/test.py" in result.error_summary

    def test_rejects_file_diffs_that_normalize_to_same_target(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
                FileDiff(
                    "skills/test/../test/test.py",
                    [Hunk("y = 2", "y = 20")],
                ),
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "duplicate canonical target" in result.error_summary
        assert "skills/test/test.py" in result.error_summary

    def test_outside_surface(self, tmp_path):
        surface = EditSurface(
            max_level=1, project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["omicsclaw/runtime/tool_executor.py"],
            diffs=[
                FileDiff("omicsclaw/runtime/tool_executor.py", [
                    Hunk("old", "new")
                ])
            ],
        )
        result = validate_patch(patch, surface)
        assert result.valid is False
        assert "frozen" in result.errors[0]

    def test_old_code_not_found(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("actual content here")

        surface = EditSurface(
            max_level=1, project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("nonexistent code", "new code")
                ])
            ],
        )
        result = validate_patch(patch, surface)
        assert result.valid is False
        assert "not found" in result.errors[0]

    def test_empty_diffs(self, tmp_path):
        surface = EditSurface(max_level=1, project_root=tmp_path)
        patch = PatchPlan(
            target_files="not-a-list",  # type: ignore[arg-type]
            diffs=[],
        )
        result = validate_patch(patch, surface)
        assert result.valid is False
        assert result.errors == ["Patch contains no diffs."]

    def test_rejects_project_escape(self, tmp_path):
        patch = PatchPlan(
            target_files=["../outside.py"],
            diffs=[
                FileDiff("../outside.py", [Hunk("old", "new")])
            ],
        )
        surface = EditSurface(max_level=4, project_root=tmp_path)
        result = validate_patch(patch, surface)
        assert result.valid is False
        assert "escapes project root" in result.errors[0]

    def test_rejects_non_target_method_patch_for_spatial_domains(self, tmp_path):
        _write_spatial_domains_fixture(tmp_path)
        surface = build_spatial_domains_surface(tmp_path, method="cellcharter")
        patch = PatchPlan(
            target_files=["skills/spatial/_lib/domains.py"],
            diffs=[
                FileDiff("skills/spatial/_lib/domains.py", [
                    Hunk('marker = "stagate"', 'marker = "stagate_v2"')
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is False
        assert "non-target method code" in result.errors[0]
        assert "cellcharter" in result.errors[0]

    def test_allows_target_helper_patch_for_spatial_domains(self, tmp_path):
        _write_spatial_domains_fixture(tmp_path)
        surface = build_spatial_domains_surface(tmp_path, method="cellcharter")
        patch = PatchPlan(
            target_files=["skills/spatial/_lib/domains.py"],
            diffs=[
                FileDiff("skills/spatial/_lib/domains.py", [
                    Hunk('return "fixed"', 'return "fixed_v2"')
                ])
            ],
        )

        result = validate_patch(patch, surface)

        assert result.valid is True


# ---------------------------------------------------------------------------
# Apply / revert tests
# ---------------------------------------------------------------------------


class TestApplyPatch:
    @pytest.mark.parametrize(
        ("old_code", "new_code"),
        [
            ("1. Load the input.", "1. Validate and load the input."),
            (
                "- Check input quality before interpreting results.",
                "- Check input quality and batch balance before interpreting results.",
            ),
        ],
        ids=["flow", "gotchas"],
    )
    def test_allows_skill_md_narrative_hunks(self, tmp_path, old_code, new_code):
        target = _write_v2_skill_md(tmp_path)
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [Hunk(old_code, new_code)])
            ],
        )

        validation = validate_patch(patch, surface)
        modified = apply_patch(patch, surface)

        assert validation.valid is True
        assert modified == ["skills/test/SKILL.md"]
        assert new_code in target.read_text(encoding="utf-8")

    def test_apply_rejects_frontmatter_without_prior_validation(self, tmp_path):
        target = _write_v2_skill_md(tmp_path)
        original = target.read_text(encoding="utf-8")
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("name: test-skill", "name: replaced-skill")
                ])
            ],
        )

        with pytest.raises(ValueError, match="YAML frontmatter"):
            apply_patch(patch, surface)

        assert target.read_text(encoding="utf-8") == original

    def test_apply_rejects_generated_io_without_prior_validation(self, tmp_path):
        target = _write_v2_skill_md(tmp_path)
        original = target.read_text(encoding="utf-8")
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("- `result.json`", "- `unverified.json`")
                ])
            ],
        )

        with pytest.raises(ValueError, match="AUTO-GENERATED Inputs & Outputs"):
            apply_patch(patch, surface)

        assert target.read_text(encoding="utf-8") == original

    def test_apply_rejects_normalized_generated_io_without_prior_validation(
        self,
        tmp_path,
    ):
        target = _write_v2_skill_md(tmp_path)
        original = target.read_text(encoding="utf-8")
        surface = EditSurface(
            max_level=1,
            project_root=tmp_path,
            explicit_files=["skills/test/SKILL.md"],
        )
        patch = PatchPlan(
            target_files=["skills/test/SKILL.md"],
            diffs=[
                FileDiff("skills/test/SKILL.md", [
                    Hunk("  -   File types:   `.h5ad`  ", "- File types: `.csv`")
                ])
            ],
        )

        with pytest.raises(ValueError, match="AUTO-GENERATED Inputs & Outputs"):
            apply_patch(patch, surface)

        assert target.read_text(encoding="utf-8") == original

    def test_apply_single_hunk(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "test.py"
        target.write_text(
            "def foo():\n    return 200\n"
        )
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [
                    Hunk("return 200", "return 300")
                ])
            ],
        )
        modified = apply_patch(patch, surface)
        assert modified == ["skills/test/test.py"]
        assert "return 300" in target.read_text()

    def test_apply_normalizes_native_crlf_to_deterministic_lf_bytes(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "test.py"
        target.write_bytes(b"threshold = 1\r\n")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff(
                    "skills/test/test.py",
                    [Hunk("threshold = 1", "threshold = 2")],
                )
            ],
        )

        modified = apply_patch(patch, surface)

        assert modified == ["skills/test/test.py"]
        assert target.read_bytes() == b"threshold = 2\n"

    def test_apply_rejects_ambiguous_whitespace_normalized_hunk_without_writing(
        self,
        tmp_path,
    ):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "test.py"
        original = """def first():
    value   = 1
    return value


def second():
    value =   1
    return value
"""
        target.write_text(original, encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff(
                    "skills/test/../test/test.py",
                    [Hunk("value = 1\nreturn value", "return 1")],
                )
            ],
        )

        with pytest.raises(
            ValueError,
            match="Ambiguous whitespace-normalized hunk: old_code appears 2 times",
        ):
            apply_patch(patch, surface)

        assert target.read_text(encoding="utf-8") == original

    def test_apply_uses_unique_whitespace_normalized_fallback(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "test.py"
        target.write_text(
            "def run():\n    value   = 1\n    return value\n",
            encoding="utf-8",
        )
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff(
                    "skills/test/test.py",
                    [
                        Hunk(
                            "value = 1\nreturn value",
                            "    value = 2\n    return value\n",
                        )
                    ],
                )
            ],
        )

        validation = validate_patch(patch, surface)
        modified = apply_patch(patch, surface)

        assert validation.valid is True
        assert modified == ["skills/test/test.py"]
        assert target.read_text(encoding="utf-8") == (
            "def run():\n    value = 2\n    return value\n"
        )

    def test_apply_prefers_one_exact_match_over_normalized_equivalent(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "test.py"
        target.write_text("value = 1\nvalue   = 1\n", encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff(
                    "skills/test/test.py",
                    [Hunk("value = 1", "value = 2")],
                )
            ],
        )

        validation = validate_patch(patch, surface)
        modified = apply_patch(patch, surface)

        assert validation.valid is True
        assert modified == ["skills/test/test.py"]
        assert target.read_text(encoding="utf-8") == "value = 2\nvalue   = 1\n"

    def test_apply_multiple_hunks(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "test.py"
        target.write_text(
            "x = 1\ny = 2\nz = 3\n"
        )
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [
                    Hunk("x = 1", "x = 10"),
                    Hunk("z = 3", "z = 30"),
                ])
            ],
        )
        modified = apply_patch(patch, surface)
        assert modified == ["skills/test/test.py"]
        content = target.read_text()
        assert "x = 10" in content
        assert "y = 2" in content
        assert "z = 30" in content

    def test_apply_multiple_files(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "a.py").write_text("a = 1\n")
        (skill_dir / "b.py").write_text("b = 2\n")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/a.py", "skills/test/b.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/a.py", "skills/test/b.py"],
            diffs=[
                FileDiff("skills/test/a.py", [Hunk("a = 1", "a = 10")]),
                FileDiff("skills/test/b.py", [Hunk("b = 2", "b = 20")]),
            ],
        )
        modified = apply_patch(patch, surface)
        assert set(modified) == {"skills/test/a.py", "skills/test/b.py"}

    def test_apply_rejects_duplicate_canonical_file_diffs_before_writing(
        self,
        tmp_path,
    ):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        target = skill_dir / "test.py"
        original = "x = 1\ny = 2\n"
        target.write_text(original, encoding="utf-8")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("x = 1", "x = 10")]),
                FileDiff(
                    "skills/test/../test/test.py",
                    [Hunk("y = 2", "y = 20")],
                ),
            ],
        )

        with pytest.raises(ValueError, match="duplicate canonical target"):
            apply_patch(patch, surface)

        assert target.read_text(encoding="utf-8") == original

    def test_apply_hunk_not_found(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("real content\n")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/test.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("nonexistent", "new")])
            ],
        )
        with pytest.raises(ValueError, match="not found"):
            apply_patch(patch, surface)

    def test_apply_rejects_paths_outside_surface(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "test.py").write_text("value = 1\n")
        surface = EditSurface(
            max_level=2,
            project_root=tmp_path,
            explicit_files=["skills/test/allowed.py"],
        )
        patch = PatchPlan(
            target_files=["skills/test/test.py"],
            diffs=[
                FileDiff("skills/test/test.py", [Hunk("value = 1", "value = 2")])
            ],
        )
        with pytest.raises(PermissionError, match="explicit editable file list"):
            apply_patch(patch, surface)

    def test_apply_rejects_project_escape(self, tmp_path):
        outside = tmp_path.parent / "outside.py"
        outside.write_text("value = 1\n")
        surface = EditSurface(max_level=4, project_root=tmp_path)
        patch = PatchPlan(
            target_files=["../outside.py"],
            diffs=[
                FileDiff("../outside.py", [Hunk("value = 1", "value = 2")])
            ],
        )
        with pytest.raises(ValueError, match="escapes project root"):
            apply_patch(patch, surface)
        assert outside.read_text() == "value = 1\n"


class TestBackupRevert:
    def test_backup_and_revert(self, tmp_path):
        src_dir = tmp_path / "src"
        backup_dir = tmp_path / "backup"
        src_dir.mkdir()

        (src_dir / "test.py").write_text("original content")

        # Backup
        backup_files(["test.py"], src_dir, backup_dir)
        assert (backup_dir / "test.py").exists()

        # Modify
        (src_dir / "test.py").write_text("modified content")

        # Revert
        revert_files(["test.py"], src_dir, backup_dir)
        assert (src_dir / "test.py").read_text() == "original content"
