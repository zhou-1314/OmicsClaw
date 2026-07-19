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
_GOTCHAS_HEADER_RE = re.compile(r"(?m)^## Gotchas[ \t]*$")
_NEXT_SECTION_RE = re.compile(r"(?m)^## ")
_EMPTY_GOTCHA_RE = re.compile(
    r"(?mi)^\s*-\s+_(?:none yet|no gotchas yet|no gotchas surfaced)\b[^\n]*\n?"
)


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
    content = inp.preconditions.content
    anndata = out.anndata

    has_inputs = bool(
        inp.modalities or inp.file_types or ds.requires_preprocessed or ds.obs or ds.obsm
        or inp.preconditions.env or inp.preconditions.config
        or inp.path_kinds != ["file"] or inp.artifacts
    )
    has_outputs = bool(
        out.files or out.result_json.required_keys or out.artifacts or out.method_scopes
        or (anndata and (anndata.saves_h5ad or anndata.processing_state
                         or anndata.obs or anndata.obsm or anndata.var
                         or anndata.layers or anndata.uns))
    )
    if not has_inputs and not has_outputs:
        return None

    lines: list[str] = [_IO_HEADER, "", IO_MARKER, ""]

    if has_inputs:
        lines.append("**Inputs**")
        lines.append("")
        if inp.path_kinds != ["file"]:
            lines.append(
                "- Input kinds: " + ", ".join(f"`{kind}`" for kind in inp.path_kinds)
            )
        if inp.modalities:
            lines.append(f"- Modalities: {', '.join(inp.modalities)}")
        if inp.file_types:
            lines.append("- File types: " + ", ".join(f"`.{ft}`" for ft in inp.file_types))
        for artifact in inp.artifacts:
            formats = ", ".join(f"`{value}`" for value in artifact.formats) or "any format"
            lines.append(f"- Accepts artifact `{artifact.kind}` ({formats})")
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
        if content and content.tabular:
            bits: list[str] = []
            if content.tabular.min_columns is not None:
                bits.append(f"at least {content.tabular.min_columns} columns")
            if content.tabular.required_columns:
                bits.append(
                    "required: "
                    + ", ".join(
                        f"`{column}`" for column in content.tabular.required_columns
                    )
                )
            lines.append("- Tabular structure: " + "; ".join(bits))
        if content and content.vcf:
            bits = []
            if content.vcf.require_fileformat_header:
                bits.append("`##fileformat`")
            if content.vcf.required_columns:
                bits.append(
                    "columns: "
                    + ", ".join(
                        f"`{column}`" for column in content.vcf.required_columns
                    )
                )
            if content.vcf.required_info_ids:
                bits.append(
                    "INFO ids: "
                    + ", ".join(f"`{name}`" for name in content.vcf.required_info_ids)
                )
            if content.vcf.required_format_ids:
                bits.append(
                    "FORMAT ids: "
                    + ", ".join(f"`{name}`" for name in content.vcf.required_format_ids)
                )
            if content.vcf.min_samples is not None:
                bits.append(f"at least {content.vcf.min_samples} samples")
            lines.append("- VCF structure: " + "; ".join(bits))
        if content and content.fastq:
            bits = []
            if content.fastq.require_valid_record:
                bits.append("valid first record")
            if content.fastq.pairing != "any":
                bits.append(f"`{content.fastq.pairing}` layout")
            lines.append("- FASTQ structure: " + "; ".join(bits))
        if content and content.directory:
            lines.append(
                "- Directory layouts (any): "
                + ", ".join(
                    f"`{signature}`"
                    for signature in content.directory.any_of_signatures
                )
            )
        lines.append("")

    if has_outputs:
        lines.append("**Outputs**")
        lines.append("")
        if out.files:
            for f in out.files:
                lines.append(f"- `{f}`")
        for artifact in out.artifacts:
            lines.append(
                f"- Produces artifact `{artifact.kind}` as `{artifact.path}` "
                f"(`{artifact.format}`)"
            )
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
        if anndata and anndata.processing_state:
            lines.append(
                "- AnnData processing state after success: "
                f"`{anndata.processing_state}`"
            )
        if out.result_json.required_keys:
            lines.append(
                "- `result.json` keys: " + ", ".join(f"`{k}`" for k in out.result_json.required_keys)
            )
        for scope in out.method_scopes:
            methods = " or ".join(f"`{method}`" for method in scope.methods)
            lines.append(f"- When `--method` is {methods}:")
            if scope.files:
                lines.append(
                    "  - Additional files: "
                    + ", ".join(f"`{path}`" for path in scope.files)
                )
            if scope.anndata:
                scoped_bits: list[str] = []
                for collection in ("obs", "obsm", "var", "layers", "uns"):
                    values = getattr(scope.anndata, collection)
                    if values:
                        scoped_bits.append(
                            f"`{collection}`: "
                            + ", ".join(f"`{value}`" for value in values)
                        )
                if scoped_bits:
                    lines.append("  - AnnData additionally guarantees " + "; ".join(scoped_bits))
            for artifact in scope.artifacts:
                lines.append(
                    f"  - Produces artifact `{artifact.kind}` as `{artifact.path}` "
                    f"(`{artifact.format}`)"
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


def append_gotcha_entry(existing_text: str, bullet: str) -> str:
    """Append one fixed one-line bullet to the narrative ``Gotchas`` section.

    The caller owns semantic validation.  This renderer only accepts a
    canonical bullet, removes the scaffold placeholder, refuses duplicates,
    and leaves every byte outside the Gotchas section untouched.
    """
    if (
        not bullet.startswith("- **")
        or "\n" in bullet
        or "\r" in bullet
        or not bullet.strip()
    ):
        raise ValueError("Gotcha writeback requires one canonical bullet")
    matches = list(_GOTCHAS_HEADER_RE.finditer(existing_text))
    if len(matches) != 1:
        raise ValueError("SKILL.md must contain exactly one ## Gotchas section")
    heading = matches[0]
    next_heading = _NEXT_SECTION_RE.search(existing_text, heading.end())
    section_end = next_heading.start() if next_heading is not None else len(existing_text)
    body = existing_text[heading.end():section_end]
    if bullet in body.splitlines():
        raise ValueError("Gotcha entry already exists")
    preserved = _EMPTY_GOTCHA_RE.sub("", body).strip("\n")
    new_body = "\n\n"
    if preserved:
        new_body += preserved + "\n\n"
    new_body += bullet + "\n"
    if next_heading is not None:
        new_body += "\n"
    return existing_text[: heading.end()] + new_body + existing_text[section_end:]
