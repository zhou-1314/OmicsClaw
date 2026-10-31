# SPDX-License-Identifier: Apache-2.0
"""Genomes & variants connector: Ensembl REST, gnomAD GraphQL, ClinVar.

Public APIs used:

* Ensembl REST ``lookup`` (https://rest.ensembl.org/).
* gnomAD browser GraphQL API (https://gnomad.broadinstitute.org/api).
* ClinVar through NCBI E-utilities ``esearch`` / ``esummary``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._common import ConnectorError, HttpClient, Tool, bounded_int, clip, require_str
from .literature import EUTILS, ncbi_params

NAME = "genomics"
INSTRUCTIONS = (
    "Gene and variant reference data. ensembl_lookup resolves a gene symbol or "
    "Ensembl ID; gnomad_variant reports population frequencies for a variant "
    "(chrom-pos-ref-alt on GRCh38, or an rsID); clinvar_search finds clinical "
    "variant classifications."
)

ENSEMBL = "https://rest.ensembl.org"
GNOMAD = "https://gnomad.broadinstitute.org/api"
_ENSEMBL_ID = re.compile(r"^ENS[A-Z]*[GTPE]\d+(\.\d+)?$", re.IGNORECASE)
_VARIANT_ID = re.compile(r"^(?:chr)?([0-9XYM]{1,2})[-:](\d+)[-:]([ACGTN]+)[-:>]([ACGTN]+)$", re.IGNORECASE)
_RSID = re.compile(r"^rs\d+$", re.IGNORECASE)
_DATASETS = ("gnomad_r4", "gnomad_r3", "gnomad_r2_1")

_GNOMAD_FIELDS = """
    variant_id
    rsids
    chrom
    pos
    ref
    alt
    exome { ac an af homozygote_count }
    genome { ac an af homozygote_count }
    transcript_consequences {
      gene_symbol
      transcript_id
      major_consequence
      hgvsc
      hgvsp
      is_canonical
    }
"""


def build_tools(client: HttpClient | None = None) -> list[Tool]:
    from .literature import ncbi_client

    http = client or ncbi_client()

    def ensembl_lookup(args: Mapping[str, Any]) -> dict[str, Any]:
        ident = require_str(args, "query")
        species = (args.get("species") or "homo_sapiens").strip().lower().replace(" ", "_")
        expand = 1 if args.get("expand") else 0
        params = {"content-type": "application/json", "expand": expand}
        if _ENSEMBL_ID.match(ident):
            data = http.get_json(f"{ENSEMBL}/lookup/id/{ident.split('.')[0]}", params)
        else:
            data = http.get_json(f"{ENSEMBL}/lookup/symbol/{species}/{ident}", params)
        if not isinstance(data, Mapping) or "id" not in data:
            raise ConnectorError(f"Ensembl has no record for {ident!r}")
        result = {
            "id": data.get("id"),
            "display_name": data.get("display_name"),
            "object_type": data.get("object_type"),
            "biotype": data.get("biotype"),
            "description": clip(data.get("description"), 300),
            "species": data.get("species"),
            "assembly": data.get("assembly_name"),
            "location": f"{data.get('seq_region_name')}:{data.get('start')}-{data.get('end')}:{data.get('strand')}",
            "canonical_transcript": data.get("canonical_transcript"),
            "version": data.get("version"),
        }
        transcripts = data.get("Transcript")
        if isinstance(transcripts, list):
            result["transcript_count"] = len(transcripts)
            result["transcripts"] = [
                {
                    "id": t.get("id"),
                    "biotype": t.get("biotype"),
                    "is_canonical": bool(t.get("is_canonical")),
                    "length": (t.get("end", 0) - t.get("start", 0) + 1) if t.get("end") else None,
                }
                for t in transcripts[:25]
            ]
        return result

    def gnomad_variant(args: Mapping[str, Any]) -> dict[str, Any]:
        ident = require_str(args, "variant")
        dataset = args.get("dataset") or "gnomad_r4"
        if dataset not in _DATASETS:
            raise ConnectorError(f"'dataset' must be one of {list(_DATASETS)}")
        if _RSID.match(ident):
            selector, variables = "variant(rsid: $id, dataset: $dataset)", {"id": ident.lower()}
        else:
            match = _VARIANT_ID.match(ident)
            if not match:
                raise ConnectorError("'variant' must be chrom-pos-ref-alt (e.g. 17-7675088-C-T) or an rsID")
            chrom, pos, ref, alt = match.groups()
            selector = "variant(variantId: $id, dataset: $dataset)"
            variables = {"id": f"{chrom.upper()}-{pos}-{ref.upper()}-{alt.upper()}"}
        query = (
            "query OmicsClawVariant($id: String!, $dataset: DatasetId!) {"
            f" {selector} {{ {_GNOMAD_FIELDS} }} }}"
        )
        answer = http.post_json(GNOMAD, {"query": query, "variables": {**variables, "dataset": dataset}})
        errors = answer.get("errors") if isinstance(answer, Mapping) else None
        variant = ((answer or {}).get("data") or {}).get("variant") if isinstance(answer, Mapping) else None
        if errors and not variant and _RSID.match(ident):
            candidates = _rsid_candidates(http, ident.lower(), dataset)
            if candidates:
                return {
                    "dataset": dataset,
                    "rsid": ident.lower(),
                    "candidates": candidates,
                    "note": "This rsID maps to several variants; call again with one variant ID.",
                }
        if errors and not variant:
            raise ConnectorError("; ".join(clip(e.get("message"), 200) for e in errors[:3]))
        if not variant:
            raise ConnectorError(f"{ident} is not in {dataset}")
        consequences = variant.get("transcript_consequences") or []
        canonical = [c for c in consequences if c.get("is_canonical")] or consequences[:3]
        return {
            "dataset": dataset,
            "variant_id": variant.get("variant_id"),
            "rsids": variant.get("rsids"),
            "exome": _freq(variant.get("exome")),
            "genome": _freq(variant.get("genome")),
            "consequences": canonical[:5],
        }

    def clinvar_search(args: Mapping[str, Any]) -> dict[str, Any]:
        query = require_str(args, "query")
        limit = bounded_int(args, "max_results", 10, 1, 50)
        found = http.get_json(
            f"{EUTILS}/esearch.fcgi",
            ncbi_params(db="clinvar", term=query, retmode="json", retmax=limit),
        )
        result = found.get("esearchresult", {}) if isinstance(found, Mapping) else {}
        ids = [str(i) for i in result.get("idlist", [])]
        records: list[dict[str, Any]] = []
        if ids:
            summary = http.get_json(
                f"{EUTILS}/esummary.fcgi",
                ncbi_params(db="clinvar", id=",".join(ids), retmode="json"),
            )
            body = summary.get("result", {}) if isinstance(summary, Mapping) else {}
            for uid in ids:
                item = body.get(uid)
                if not isinstance(item, Mapping):
                    continue
                classification = (
                    item.get("germline_classification")
                    or item.get("clinical_significance")
                    or {}
                )
                records.append(
                    {
                        "uid": uid,
                        "accession": item.get("accession"),
                        "title": clip(item.get("title"), 300),
                        "classification": classification.get("description"),
                        "review_status": classification.get("review_status"),
                        "last_evaluated": classification.get("last_evaluated"),
                        "genes": [g.get("symbol") for g in item.get("genes", []) if g.get("symbol")],
                        "molecular_consequences": item.get("molecular_consequence_list", [])[:5],
                        "conditions": [
                            t.get("trait_name")
                            for t in (classification.get("trait_set") or [])
                            if isinstance(t, Mapping) and t.get("trait_name")
                        ][:5],
                    }
                )
        return {
            "query": query,
            "total_count": int(result.get("count", 0) or 0),
            "results": records,
        }

    return [
        Tool(
            "ensembl_lookup",
            "Resolve a gene symbol or Ensembl ID to its Ensembl record: stable ID, biotype, location, canonical transcript.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gene symbol (BRCA2) or Ensembl ID (ENSG00000139618)."},
                    "species": {"type": "string", "default": "homo_sapiens"},
                    "expand": {"type": "boolean", "default": False, "description": "Include transcripts."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            ensembl_lookup,
        ),
        Tool(
            "gnomad_variant",
            "Population allele frequencies and canonical consequences for a variant from gnomAD (GRCh38 for v3/v4).",
            {
                "type": "object",
                "properties": {
                    "variant": {"type": "string", "description": "chrom-pos-ref-alt, e.g. 17-7675088-C-T, or an rsID."},
                    "dataset": {"type": "string", "enum": list(_DATASETS), "default": "gnomad_r4"},
                },
                "required": ["variant"],
                "additionalProperties": False,
            },
            gnomad_variant,
        ),
        Tool(
            "clinvar_search",
            "Search ClinVar (e.g. 'BRCA1[gene] AND pathogenic[clinsig]') and return classifications and conditions.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            clinvar_search,
        ),
    ]


def _rsid_candidates(http: HttpClient, rsid: str, dataset: str) -> list[str]:
    query = (
        "query OmicsClawVariantSearch($q: String!, $dataset: DatasetId!) {"
        " variant_search(query: $q, dataset: $dataset) { variant_id } }"
    )
    try:
        answer = http.post_json(GNOMAD, {"query": query, "variables": {"q": rsid, "dataset": dataset}})
    except ConnectorError:
        return []
    rows = ((answer or {}).get("data") or {}).get("variant_search") if isinstance(answer, Mapping) else None
    return [r.get("variant_id") for r in rows or [] if isinstance(r, Mapping) and r.get("variant_id")][:20]


def _freq(block: Any) -> dict[str, Any] | None:
    if not isinstance(block, Mapping):
        return None
    return {
        "ac": block.get("ac"),
        "an": block.get("an"),
        "af": block.get("af"),
        "homozygotes": block.get("homozygote_count"),
    }
