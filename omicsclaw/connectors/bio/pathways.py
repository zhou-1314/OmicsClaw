# SPDX-License-Identifier: Apache-2.0
"""Pathways & interactions connector: Reactome Content Service and STRING.

Public APIs used:

* Reactome Content Service (https://reactome.org/ContentService/).
  Pathways are looked up by UniProt accession; a gene symbol is first
  resolved to its reviewed human UniProt entry.
* STRING API (https://string-db.org/help/api/).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._common import ConnectorError, HttpClient, Tool, bounded_int, require_str
from .proteins import resolve_accession

NAME = "pathways"
INSTRUCTIONS = (
    "Biological pathways and interaction networks. reactome_pathways_for_gene "
    "lists curated Reactome pathways containing a gene's product; "
    "string_network returns STRING protein–protein interactions."
)

REACTOME = "https://reactome.org/ContentService"
STRING = "https://string-db.org/api"

_REACTOME_SPECIES = {9606: "9606", 10090: "10090", 10116: "10116"}


def build_tools(client: HttpClient | None = None) -> list[Tool]:
    http = client or HttpClient(min_interval_s={"string-db.org": 1.0})

    def reactome_pathways_for_gene(args: Mapping[str, Any]) -> dict[str, Any]:
        gene = require_str(args, "gene")
        taxon = int(args.get("organism_id") or 9606)
        if taxon not in _REACTOME_SPECIES:
            raise ConnectorError(f"'organism_id' must be one of {sorted(_REACTOME_SPECIES)}")
        accession = resolve_accession(http, gene, taxon)
        try:
            rows = http.get_json(
                f"{REACTOME}/data/mapping/UniProt/{accession}/pathways",
                {"species": _REACTOME_SPECIES[taxon]},
            )
        except ConnectorError as exc:
            if "404" in str(exc):
                raise ConnectorError(f"Reactome has no pathways for {accession}") from None
            raise
        rows = rows if isinstance(rows, list) else []
        limit = bounded_int(args, "max_results", 50, 1, 200)
        return {
            "gene": gene,
            "uniprot_accession": accession,
            "pathway_count": len(rows),
            "pathways": [
                {
                    "st_id": r.get("stId"),
                    "name": r.get("displayName"),
                    "is_in_disease": r.get("isInDisease"),
                    "species": r.get("speciesName"),
                    "url": f"https://reactome.org/content/detail/{r.get('stId')}",
                }
                for r in rows[:limit]
            ],
        }

    def string_network(args: Mapping[str, Any]) -> dict[str, Any]:
        raw = args.get("identifiers")
        if isinstance(raw, str):
            identifiers = [i.strip() for i in raw.replace(",", " ").split() if i.strip()]
        elif isinstance(raw, list):
            identifiers = [str(i).strip() for i in raw if str(i).strip()]
        else:
            raise ConnectorError("'identifiers' must be a list of gene or protein names")
        if not identifiers:
            raise ConnectorError("'identifiers' must not be empty")
        identifiers = identifiers[:50]
        species = int(args.get("species") or 9606)
        score = bounded_int(args, "required_score", 400, 0, 1000)
        partners = bounded_int(args, "add_partners", 10 if len(identifiers) == 1 else 0, 0, 50)
        params: dict[str, Any] = {
            "identifiers": "\r".join(identifiers),
            "species": species,
            "required_score": score,
            "caller_identity": "omicsclaw",
        }
        if partners:
            params["add_nodes"] = partners
        edges = http.get_json(f"{STRING}/json/network", params)
        if isinstance(edges, Mapping) and edges.get("Error"):
            raise ConnectorError(str(edges.get("ErrorMessage") or edges["Error"]))
        edges = edges if isinstance(edges, list) else []
        seen: set[tuple[str, str]] = set()
        interactions = []
        for e in edges:
            pair = tuple(sorted((str(e.get("preferredName_A")), str(e.get("preferredName_B")))))
            if pair in seen:
                continue
            seen.add(pair)  # type: ignore[arg-type]
            interactions.append(
                {
                    "a": e.get("preferredName_A"),
                    "b": e.get("preferredName_B"),
                    "score": e.get("score"),
                    "experimental": e.get("escore"),
                    "database": e.get("dscore"),
                    "textmining": e.get("tscore"),
                }
            )
        interactions.sort(key=lambda item: item["score"] or 0, reverse=True)
        nodes = sorted({n for i in interactions for n in (i["a"], i["b"]) if n})
        return {
            "query": identifiers,
            "species": species,
            "required_score": score,
            "node_count": len(nodes),
            "nodes": nodes,
            "interactions": interactions[:200],
        }

    return [
        Tool(
            "reactome_pathways_for_gene",
            "Reactome pathways that contain a gene's product (gene symbol or UniProt accession).",
            {
                "type": "object",
                "properties": {
                    "gene": {"type": "string", "description": "Gene symbol (TP53) or UniProt accession (P04637)."},
                    "organism_id": {"type": "integer", "enum": sorted(_REACTOME_SPECIES), "default": 9606},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                "required": ["gene"],
                "additionalProperties": False,
            },
            reactome_pathways_for_gene,
        ),
        Tool(
            "string_network",
            "STRING protein–protein interaction network for one or more proteins, with combined and evidence scores.",
            {
                "type": "object",
                "properties": {
                    "identifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 50,
                        "description": "Gene or protein names, e.g. ['TP53', 'MDM2'].",
                    },
                    "species": {"type": "integer", "default": 9606},
                    "required_score": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 400},
                    "add_partners": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 50,
                        "description": "Extra interaction partners to add (default 10 for a single protein).",
                    },
                },
                "required": ["identifiers"],
                "additionalProperties": False,
            },
            string_network,
        ),
    ]
