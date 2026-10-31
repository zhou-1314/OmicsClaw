# SPDX-License-Identifier: Apache-2.0
"""Proteins connector over the UniProt REST API.

API reference: https://www.uniprot.org/help/api
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._common import ConnectorError, HttpClient, Tool, bounded_int, clip, require_str

NAME = "proteins"
INSTRUCTIONS = (
    "UniProtKB protein knowledge. uniprot_search finds entries by gene, protein "
    "name or UniProt query syntax; uniprot_entry reads one accession."
)

BASE = "https://rest.uniprot.org/uniprotkb"
ACCESSION = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-\d+)?$",
    re.IGNORECASE,
)


def resolve_accession(http: HttpClient, gene: str, organism_id: int = 9606) -> str:
    """The reviewed UniProt accession for gene symbol *gene* in *organism_id*."""
    if ACCESSION.match(gene):
        return gene.upper()
    data = http.get_json(
        f"{BASE}/search",
        {
            "query": f"gene_exact:{gene} AND organism_id:{organism_id} AND reviewed:true",
            "fields": "accession",
            "format": "json",
            "size": 1,
        },
    )
    results = data.get("results", []) if isinstance(data, Mapping) else []
    if not results:
        raise ConnectorError(f"no reviewed UniProt entry for gene {gene!r} in taxon {organism_id}")
    return str(results[0]["primaryAccession"])


def build_tools(client: HttpClient | None = None) -> list[Tool]:
    http = client or HttpClient()

    def uniprot_search(args: Mapping[str, Any]) -> dict[str, Any]:
        query = require_str(args, "query")
        limit = bounded_int(args, "max_results", 10, 1, 50)
        clauses = [f"({query})"]
        if args.get("organism_id"):
            clauses.append(f"organism_id:{int(args['organism_id'])}")
        if args.get("reviewed", True):
            clauses.append("reviewed:true")
        data = http.get_json(
            f"{BASE}/search",
            {
                "query": " AND ".join(clauses),
                "fields": "accession,id,protein_name,gene_names,organism_name,length,reviewed",
                "format": "json",
                "size": limit,
            },
        )
        rows = data.get("results", []) if isinstance(data, Mapping) else []
        return {"query": query, "results": [_brief(r) for r in rows]}

    def uniprot_entry(args: Mapping[str, Any]) -> dict[str, Any]:
        accession = require_str(args, "accession")
        if not ACCESSION.match(accession):
            raise ConnectorError("'accession' must be a UniProt accession such as P04637")
        entry = http.get_json(f"{BASE}/{accession.upper()}.json")
        if not isinstance(entry, Mapping) or "primaryAccession" not in entry:
            raise ConnectorError(f"no UniProt entry {accession}")
        result = _brief(entry)
        comments = entry.get("comments", [])
        result["function"] = clip(
            " ".join(
                t.get("value", "")
                for c in comments
                if c.get("commentType") == "FUNCTION"
                for t in c.get("texts", [])
            ),
            2000,
        )
        result["subcellular_location"] = sorted(
            {
                loc.get("location", {}).get("value")
                for c in comments
                if c.get("commentType") == "SUBCELLULAR LOCATION"
                for loc in c.get("subcellularLocations", [])
                if loc.get("location", {}).get("value")
            }
        )
        result["keywords"] = [k.get("name") for k in entry.get("keywords", [])][:20]
        refs = entry.get("uniProtKBCrossReferences", [])
        result["pdb_ids"] = [r.get("id") for r in refs if r.get("database") == "PDB"][:15]
        if args.get("include_sequence"):
            result["sequence"] = (entry.get("sequence") or {}).get("value")
        return result

    return [
        Tool(
            "uniprot_search",
            "Search UniProtKB (gene symbol, protein name or UniProt query syntax); reviewed entries by default.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "e.g. 'gene:TP53' or 'kinase AND cell cycle'."},
                    "organism_id": {"type": "integer", "description": "NCBI taxon, e.g. 9606."},
                    "reviewed": {"type": "boolean", "default": True},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            uniprot_search,
        ),
        Tool(
            "uniprot_entry",
            "Read a UniProtKB entry: names, genes, organism, length, function, location, keywords, PDB IDs.",
            {
                "type": "object",
                "properties": {
                    "accession": {"type": "string", "description": "UniProt accession, e.g. P04637."},
                    "include_sequence": {"type": "boolean", "default": False},
                },
                "required": ["accession"],
                "additionalProperties": False,
            },
            uniprot_entry,
        ),
    ]


def _brief(entry: Mapping[str, Any]) -> dict[str, Any]:
    description = entry.get("proteinDescription") or {}
    recommended = (description.get("recommendedName") or {}).get("fullName", {}).get("value")
    if not recommended:
        submitted = description.get("submissionNames") or [{}]
        recommended = (submitted[0].get("fullName") or {}).get("value")
    genes = [
        (g.get("geneName") or {}).get("value")
        for g in entry.get("genes", [])
        if (g.get("geneName") or {}).get("value")
    ]
    return {
        "accession": entry.get("primaryAccession"),
        "entry_name": entry.get("uniProtkbId"),
        "protein_name": recommended,
        "genes": genes,
        "organism": (entry.get("organism") or {}).get("scientificName"),
        "length": (entry.get("sequence") or {}).get("length"),
        "reviewed": str(entry.get("entryType", "")).startswith("UniProtKB reviewed"),
    }
