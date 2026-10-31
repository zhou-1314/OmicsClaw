# SPDX-License-Identifier: Apache-2.0
"""Expression connector over the GTEx Portal API v2.

API reference: https://gtexportal.org/api/v2/redoc
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._common import ConnectorError, HttpClient, Tool, bounded_int, require_str

NAME = "expression"
INSTRUCTIONS = (
    "Normal-tissue gene expression. gtex_gene_expression returns median TPM "
    "per GTEx tissue for a gene symbol or GENCODE ID, highest first."
)

BASE = "https://gtexportal.org/api/v2"
_GENCODE = re.compile(r"^ENSG\d+\.\d+$", re.IGNORECASE)
_DATASETS = ("gtex_v8", "gtex_v10")


def build_tools(client: HttpClient | None = None) -> list[Tool]:
    http = client or HttpClient()

    def gtex_gene_expression(args: Mapping[str, Any]) -> dict[str, Any]:
        gene = require_str(args, "gene")
        dataset = args.get("dataset") or "gtex_v8"
        if dataset not in _DATASETS:
            raise ConnectorError(f"'dataset' must be one of {list(_DATASETS)}")
        limit = bounded_int(args, "max_tissues", 60, 1, 60)
        if _GENCODE.match(gene):
            gencode_id, symbol = gene, None
        else:
            ref = http.get_json(f"{BASE}/reference/gene", {"geneId": gene})
            rows = ref.get("data", []) if isinstance(ref, Mapping) else []
            exact = [r for r in rows if str(r.get("geneSymbol", "")).upper() == gene.upper()]
            row = (exact or rows or [None])[0]
            if not row:
                raise ConnectorError(f"GTEx does not know gene {gene!r}")
            gencode_id, symbol = row.get("gencodeId"), row.get("geneSymbol")
        data = http.get_json(
            f"{BASE}/expression/medianGeneExpression",
            {"gencodeId": gencode_id, "datasetId": dataset},
        )
        rows = data.get("data", []) if isinstance(data, Mapping) else []
        if not rows:
            raise ConnectorError(f"no GTEx expression for {gencode_id} in {dataset}")
        tissues = sorted(
            (
                {
                    "tissue": r.get("tissueSiteDetailId"),
                    "median_tpm": r.get("median"),
                    "unit": r.get("unit"),
                }
                for r in rows
            ),
            key=lambda item: item["median_tpm"] or 0,
            reverse=True,
        )
        return {
            "gene": symbol or rows[0].get("geneSymbol"),
            "gencode_id": gencode_id,
            "dataset": dataset,
            "tissues": tissues[:limit],
        }

    return [
        Tool(
            "gtex_gene_expression",
            "Median expression (TPM) of a gene across GTEx normal tissues, sorted highest first.",
            {
                "type": "object",
                "properties": {
                    "gene": {"type": "string", "description": "Gene symbol (TP53) or versioned GENCODE ID."},
                    "dataset": {"type": "string", "enum": list(_DATASETS), "default": "gtex_v8"},
                    "max_tissues": {"type": "integer", "minimum": 1, "maximum": 60, "default": 60},
                },
                "required": ["gene"],
                "additionalProperties": False,
            },
            gtex_gene_expression,
        )
    ]
