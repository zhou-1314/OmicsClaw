"""Render `references/parameters.md` for both v1 and v2 skills (ADR 0037).

The CLI flags + per-method tuning hints render identically for both tracks —
only the parameter *source* differs:

- **v1** — the flat `parameters.yaml` sidecar: top-level `allowed_extra_flags`
  and `param_hints`.
- **v2** — `skill.yaml.interface.parameters`: `allowed_extra_flags` and `hints`
  (same shape as v1 `param_hints`, just renamed and nested).

`render_parameters_md` takes a dict carrying `allowed_extra_flags` plus the
hints (under `param_hints` for v1, `hints` for v2, selected by `source`) and
emits the provenance header for that track. The body bytes are identical
across tracks for the same skill — only the header file reference changes.

Lives in the package so that both consumers — `scripts/skill_lint.py` (which
diffs the rendered output against the on-disk reference) and
`omicsclaw/skill/scaffolder.py` (which writes the rendered output for
freshly-scaffolded skills) — can import it directly, without mutating
`sys.path` to reach into `scripts/`.

`scripts/generate_parameters_md.py` re-exports `render_parameters_md` and
owns the CLI wrapper.  The function below is the single source of truth.
"""

from __future__ import annotations


AUTOGEN_HEADER = (
    "<!-- AUTO-GENERATED from parameters.yaml — do not edit by hand. -->\n"
    "<!-- Regenerate: python scripts/generate_parameters_md.py <skill_dir> -->\n\n"
)

# v2 skills render from skill.yaml (parameters.yaml no longer exists), so the
# provenance header points at the new source of truth. Body bytes are unchanged.
AUTOGEN_HEADER_V2 = (
    "<!-- AUTO-GENERATED from skill.yaml — do not edit by hand. -->\n"
    "<!-- Regenerate: python scripts/generate_parameters_md.py <skill_dir> -->\n\n"
)


def render_parameters_md(sidecar: dict, *, source: str = "v1") -> str:
    """Render a parameter dict to the canonical markdown layout.

    `source` selects the track: ``"v1"`` reads `param_hints` and stamps the
    parameters.yaml header; ``"v2"`` reads `hints` and stamps the skill.yaml
    header. `allowed_extra_flags` is top-level in both.

    Empty / missing fields render as informative one-liners so a reader
    always knows whether the absence is intentional.
    """
    if source not in ("v1", "v2"):
        raise ValueError(f"source must be 'v1' or 'v2', got {source!r}")
    header = AUTOGEN_HEADER_V2 if source == "v2" else AUTOGEN_HEADER
    hints_key = "hints" if source == "v2" else "param_hints"
    lines: list[str] = [header, "# Parameters\n"]

    flags = sidecar.get("allowed_extra_flags", []) or []
    lines.append("## Allowed extra CLI flags\n")
    if flags:
        for flag in flags:
            lines.append(f"- `{flag}`")
    else:
        lines.append("_No extra flags beyond the standard `--input` / `--output` / `--demo` set._")
    lines.append("")

    lines.append("## Per-method parameter hints\n")
    hints: dict = sidecar.get(hints_key, {}) or {}
    if not hints:
        lines.append("_No method-specific tuning hints._\n")
        return "\n".join(lines).rstrip() + "\n"

    for method in sorted(hints):
        info = hints[method] or {}
        lines.append(f"### `{method}`\n")

        priority = info.get("priority")
        if priority:
            lines.append(f"**Tuning priority:** {priority}\n")

        # P5 (acquisition-plan.md §P5): a corpus-derived skill's hints carry a
        # per-param `source_refs` entry (quote/span/doc_ref, or {"todo": True})
        # — surfaced as a 3rd column when present. Gated per-method so every
        # existing skill (none of which have `source_refs` today) renders
        # byte-identical, since skill_lint._check_parameters_md_fresh diffs
        # this output against the on-disk reference.
        source_refs: dict = info.get("source_refs", {}) or {}
        has_source_refs = bool(source_refs)
        for label, key in (("Core parameters", "params"), ("Advanced parameters", "advanced_params")):
            params = info.get(key) or []
            if params:
                lines.append(f"**{label}:**")
                lines.append("")
                if has_source_refs:
                    lines.append("| name | default | source |")
                    lines.append("|---|---|---|")
                else:
                    lines.append("| name | default |")
                    lines.append("|---|---|")
                defaults = info.get("defaults", {}) or {}
                for p in params:
                    dval = defaults.get(p, "—")
                    if not has_source_refs:
                        lines.append(f"| `{p}` | `{dval}` |")
                        continue
                    ref = source_refs.get(p)
                    if not ref:
                        source_cell = "—"
                    elif ref.get("todo"):
                        source_cell = "TODO"
                    else:
                        quote = str(ref.get("quote", "")).replace("|", "\\|")
                        doc_ref = ref.get("doc_ref", "")
                        source_cell = f'"{quote}" ({doc_ref})' if quote else "—"
                    lines.append(f"| `{p}` | `{dval}` | {source_cell} |")
                lines.append("")

        requires = info.get("requires") or []
        if requires:
            lines.append("**Requires:**")
            for r in requires:
                lines.append(f"- `{r}`")
            lines.append("")

        tips = info.get("tips") or []
        if tips:
            lines.append("**Tips:**")
            for t in tips:
                lines.append(f"- {t}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
