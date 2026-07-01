"""Generate the v2 narrative ``SKILL.md`` one-way from ``skill.yaml`` (ADR 0037).

ADR 0037 §Decision demotes ``SKILL.md`` to a pure narrative methodology card:

- its **frontmatter header is generated** from ``skill.yaml`` (so identity /
  description / version / tags / requires can never drift from the machine
  contract), and
- the hand-written ``## Inputs & Outputs`` fact section is **replaced by a
  generated read-only summary** rendered from ``interface`` (otherwise the
  output contract keeps drifting from the script).

Everything else — ``When to use`` / ``Flow`` / ``Gotchas`` / ``Key CLI`` /
``See also`` — is hand-written narrative and is preserved verbatim. The
transform is idempotent: re-running regenerates the same header + I/O block and
leaves the narrative untouched.
"""

from __future__ import annotations

import re

import yaml

from .lazy_metadata import LazySkillMetadata
from .schema import SkillManifest

# Marker on the generated I/O block so it is unmistakably machine-owned and the
# generator can find + replace it on every run.
IO_MARKER = (
    "<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. "
    "Regenerate: python scripts/generate_skill_md.py <skill_dir> -->"
)
_FRONTMATTER_NOTE = (
    "# AUTO-GENERATED header from skill.yaml — do not edit by hand.\n"
    "# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>"
)

_SECTION_RE = re.compile(r"(?m)^(## .*)$")
_IO_HEADER = "## Inputs & Outputs"
_WHEN_HEADER = "## When to use"


# ── frontmatter ──────────────────────────────────────────────────────────────
def render_frontmatter(manifest: SkillManifest) -> str:
    """Render the generated YAML frontmatter body (between the ``---`` fences).

    Identity is sourced entirely from ``skill.yaml``; the description is the same
    reconstructed 'Load when… / Skip when…' string the catalog uses, so all
    derived surfaces stay byte-consistent.
    """
    fm: dict = {
        "name": manifest.name,
        "description": LazySkillMetadata._reconstruct_description(manifest.summary),
        "version": manifest.version,
    }
    if manifest.author:
        fm["author"] = manifest.author
    if manifest.license:
        fm["license"] = manifest.license
    if manifest.emoji:
        fm["emoji"] = manifest.emoji
    if manifest.summary.tags:
        fm["tags"] = list(manifest.summary.tags)
    if manifest.deps.python:
        fm["requires"] = list(manifest.deps.python)
    dumped = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
    ).rstrip()
    return _FRONTMATTER_NOTE + "\n" + dumped


# ── generated Inputs & Outputs summary ───────────────────────────────────────
def render_io_section(manifest: SkillManifest) -> str | None:
    """Render the generated ``## Inputs & Outputs`` section, or None if empty.

    Returns the full section text (header + marker + body). None when the
    interface carries no inputs/outputs facts (nothing to summarise).
    """
    iface = manifest.interface
    inp = iface.inputs
    out = iface.outputs
    ds = inp.preconditions.data_shape
    anndata = out.anndata

    has_inputs = bool(
        inp.modalities or inp.file_types or ds.requires_preprocessed or ds.obs or ds.obsm
        or inp.preconditions.env or inp.preconditions.config
    )
    has_outputs = bool(
        out.files or out.result_json.required_keys
        or (anndata and (anndata.saves_h5ad or anndata.obs or anndata.obsm or anndata.var
                         or anndata.layers or anndata.uns))
    )
    if not has_inputs and not has_outputs:
        return None

    lines: list[str] = [_IO_HEADER, "", IO_MARKER, ""]

    if has_inputs:
        lines.append("**Inputs**")
        lines.append("")
        if inp.modalities:
            lines.append(f"- Modalities: {', '.join(inp.modalities)}")
        if inp.file_types:
            lines.append("- File types: " + ", ".join(f"`.{ft}`" for ft in inp.file_types))
        if ds.requires_preprocessed:
            lines.append("- Requires a preprocessed AnnData (`X` normalised, PCA/neighbours present)")
        if ds.obsm:
            lines.append("- Expects `obsm`: " + ", ".join(f"`{k}`" for k in ds.obsm))
        if ds.obs:
            lines.append("- Expects `obs`: " + ", ".join(f"`{k}`" for k in ds.obs))
        if inp.preconditions.env:
            lines.append("- Env vars: " + ", ".join(f"`{e}`" for e in inp.preconditions.env))
        if inp.preconditions.config:
            lines.append("- Config: " + ", ".join(f"`{c}`" for c in inp.preconditions.config))
        lines.append("")

    if has_outputs:
        lines.append("**Outputs**")
        lines.append("")
        if out.files:
            for f in out.files:
                lines.append(f"- `{f}`")
        if anndata and anndata.saves_h5ad:
            schema_bits: list[str] = []
            if anndata.obs:
                schema_bits.append("`obs`: " + ", ".join(f"`{k}`" for k in anndata.obs))
            if anndata.obsm:
                schema_bits.append("`obsm`: " + ", ".join(f"`{k}`" for k in anndata.obsm))
            if anndata.var:
                schema_bits.append("`var`: " + ", ".join(f"`{k}`" for k in anndata.var))
            if anndata.layers:
                schema_bits.append("`layers`: " + ", ".join(f"`{k}`" for k in anndata.layers))
            if anndata.uns:
                schema_bits.append("`uns`: " + ", ".join(f"`{k}`" for k in anndata.uns))
            suffix = (" — adds " + "; ".join(schema_bits)) if schema_bits else ""
            lines.append(f"- Processed AnnData (`saves_h5ad`){suffix}")
        if out.result_json.required_keys:
            lines.append(
                "- `result.json` keys: " + ", ".join(f"`{k}`" for k in out.result_json.required_keys)
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── body assembly ────────────────────────────────────────────────────────────
def _split_body(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a SKILL.md body into (preamble, [(header, content), ...])."""
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return body, []
    preamble = body[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        header = m.group(1).rstrip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((header, body[start:end]))
    return preamble, sections


def render_skill_md(manifest: SkillManifest, existing_text: str) -> str:
    """Return the regenerated SKILL.md: generated header + generated I/O summary
    + preserved narrative sections.

    The hand-written ``## Inputs & Outputs`` section (if any) is dropped and the
    generated summary inserted in its canonical slot (right after ``When to
    use``). Narrative section bodies are preserved verbatim; only inter-section
    spacing is normalised.
    """
    if existing_text.startswith("---"):
        parts = existing_text.split("---", 2)
        body = parts[2] if len(parts) >= 3 else existing_text
    else:
        body = existing_text

    preamble, sections = _split_body(body)
    # Drop any existing Inputs & Outputs (hand-written or previously generated).
    sections = [(h, c) for (h, c) in sections if not h.startswith(_IO_HEADER)]

    io_section = render_io_section(manifest)
    if io_section is not None:
        idx = next((i for i, (h, _) in enumerate(sections) if h.startswith(_WHEN_HEADER)), -1)
        # io_section already contains its own header; store with a sentinel header
        # so reassembly emits it once.
        insert_at = idx + 1 if idx >= 0 else 0
        sections.insert(insert_at, (None, io_section))

    out = f"---\n{render_frontmatter(manifest)}\n---\n"
    pre = preamble.strip("\n")
    if pre:
        out += "\n" + pre + "\n"
    for header, content in sections:
        if header is None:
            # Pre-rendered, self-contained section (the generated I/O block).
            out += "\n" + content.rstrip("\n") + "\n"
        else:
            out += "\n" + header + "\n\n" + content.strip("\n") + "\n"
    return out
