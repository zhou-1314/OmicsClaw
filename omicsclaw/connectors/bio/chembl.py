# SPDX-License-Identifier: Apache-2.0
"""ChEMBL connector over the ChEMBL web services.

API reference: https://chembl.gitbook.io/chembl-interface-documentation/web-services
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._common import ConnectorError, HttpClient, Tool, bounded_int, clip, require_str

NAME = "chembl"
INSTRUCTIONS = (
    "ChEMBL bioactivity data. chembl_molecule resolves a compound by ChEMBL ID "
    "or name; chembl_target_search finds targets; chembl_activities lists "
    "measured activities for a target and/or molecule."
)

BASE = "https://www.ebi.ac.uk/chembl/api/data"
_CHEMBL_ID = re.compile(r"^CHEMBL\d+$", re.IGNORECASE)


def build_tools(client: HttpClient | None = None) -> list[Tool]:
    http = client or HttpClient(timeout_s=45.0)

    def chembl_molecule(args: Mapping[str, Any]) -> dict[str, Any]:
        ident = require_str(args, "query")
        if _CHEMBL_ID.match(ident):
            return {"results": [_molecule(http.get_json(f"{BASE}/molecule/{ident.upper()}.json"))]}
        limit = bounded_int(args, "max_results", 5, 1, 20)
        data = http.get_json(f"{BASE}/molecule/search.json", {"q": ident, "limit": limit})
        molecules = data.get("molecules", []) if isinstance(data, Mapping) else []
        if not molecules:
            raise ConnectorError(f"no ChEMBL molecule matches {ident!r}")
        return {"results": [_molecule(m) for m in molecules]}

    def chembl_target_search(args: Mapping[str, Any]) -> dict[str, Any]:
        query = require_str(args, "query")
        limit = bounded_int(args, "max_results", 10, 1, 50)
        params: dict[str, Any] = {"q": query, "limit": limit}
        data = http.get_json(f"{BASE}/target/search.json", params)
        targets = data.get("targets", []) if isinstance(data, Mapping) else []
        organism = args.get("organism")
        results = []
        for target in targets:
            if organism and str(organism).lower() not in str(target.get("organism", "")).lower():
                continue
            results.append(
                {
                    "target_chembl_id": target.get("target_chembl_id"),
                    "pref_name": target.get("pref_name"),
                    "organism": target.get("organism"),
                    "target_type": target.get("target_type"),
                    "accessions": [
                        c.get("accession")
                        for c in target.get("target_components", [])
                        if c.get("accession")
                    ][:5],
                }
            )
        return {"query": query, "results": results}

    def chembl_activities(args: Mapping[str, Any]) -> dict[str, Any]:
        target = args.get("target_chembl_id")
        molecule = args.get("molecule_chembl_id")
        if not target and not molecule:
            raise ConnectorError("give 'target_chembl_id' and/or 'molecule_chembl_id'")
        for value, key in ((target, "target_chembl_id"), (molecule, "molecule_chembl_id")):
            if value and not _CHEMBL_ID.match(str(value)):
                raise ConnectorError(f"'{key}' must look like CHEMBL1234")
        limit = bounded_int(args, "max_results", 20, 1, 100)
        params: dict[str, Any] = {
            "limit": limit,
            "target_chembl_id": str(target).upper() if target else None,
            "molecule_chembl_id": str(molecule).upper() if molecule else None,
            "standard_type": args.get("standard_type") or None,
        }
        if args.get("pchembl_only", True):
            params["pchembl_value__isnull"] = "false"
        data = http.get_json(f"{BASE}/activity.json", params)
        rows = data.get("activities", []) if isinstance(data, Mapping) else []
        meta = data.get("page_meta", {}) if isinstance(data, Mapping) else {}
        return {
            "total_count": meta.get("total_count"),
            "results": [
                {
                    "molecule_chembl_id": r.get("molecule_chembl_id"),
                    "molecule_name": r.get("molecule_pref_name"),
                    "target_chembl_id": r.get("target_chembl_id"),
                    "target_name": r.get("target_pref_name"),
                    "type": r.get("standard_type"),
                    "relation": r.get("standard_relation"),
                    "value": r.get("standard_value"),
                    "units": r.get("standard_units"),
                    "pchembl_value": r.get("pchembl_value"),
                    "assay_chembl_id": r.get("assay_chembl_id"),
                    "assay_description": clip(r.get("assay_description"), 200),
                    "document_year": r.get("document_year"),
                }
                for r in rows
            ],
        }

    return [
        Tool(
            "chembl_molecule",
            "Look up a ChEMBL compound by ChEMBL ID (e.g. CHEMBL25) or name: phase, structure, properties, synonyms.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "ChEMBL ID or compound name."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            chembl_molecule,
        ),
        Tool(
            "chembl_target_search",
            "Search ChEMBL targets by name or gene symbol; returns target ChEMBL IDs, organism and UniProt accessions.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "organism": {"type": "string", "description": "Keep only targets from this organism, e.g. 'Homo sapiens'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            chembl_target_search,
        ),
        Tool(
            "chembl_activities",
            "List measured bioactivities (IC50, Ki, ...) for a ChEMBL target and/or molecule.",
            {
                "type": "object",
                "properties": {
                    "target_chembl_id": {"type": "string"},
                    "molecule_chembl_id": {"type": "string"},
                    "standard_type": {"type": "string", "description": "e.g. IC50, Ki, EC50."},
                    "pchembl_only": {"type": "boolean", "default": True},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "additionalProperties": False,
            },
            chembl_activities,
        ),
    ]


def _molecule(m: Any) -> dict[str, Any]:
    if not isinstance(m, Mapping):
        raise ConnectorError("ChEMBL returned an unexpected molecule record")
    props = m.get("molecule_properties") or {}
    structures = m.get("molecule_structures") or {}
    synonyms = sorted({s.get("molecule_synonym") for s in m.get("molecule_synonyms", []) if s.get("molecule_synonym")})
    return {
        "molecule_chembl_id": m.get("molecule_chembl_id"),
        "pref_name": m.get("pref_name"),
        "molecule_type": m.get("molecule_type"),
        "max_phase": m.get("max_phase"),
        "first_approval": m.get("first_approval"),
        "formula": props.get("full_molformula"),
        "molecular_weight": props.get("full_mwt"),
        "alogp": props.get("alogp"),
        "canonical_smiles": structures.get("canonical_smiles"),
        "inchi_key": structures.get("standard_inchi_key"),
        "synonyms": synonyms[:10],
    }
