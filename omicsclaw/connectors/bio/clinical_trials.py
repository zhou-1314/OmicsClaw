# SPDX-License-Identifier: Apache-2.0
"""Clinical trials connector over the ClinicalTrials.gov API v2.

API reference: https://clinicaltrials.gov/data-api/api
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._common import ConnectorError, HttpClient, Tool, bounded_int, clip, require_str

NAME = "clinical-trials"
INSTRUCTIONS = (
    "Registered clinical studies. trials_search finds studies by condition, "
    "intervention or free text; trial_get reads one study by NCT number."
)

BASE = "https://clinicaltrials.gov/api/v2"
_NCT = re.compile(r"^NCT\d{8}$", re.IGNORECASE)
_STATUSES = (
    "RECRUITING",
    "NOT_YET_RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "COMPLETED",
    "SUSPENDED",
    "TERMINATED",
    "WITHDRAWN",
)


def build_tools(client: HttpClient | None = None) -> list[Tool]:
    http = client or HttpClient()

    def trials_search(args: Mapping[str, Any]) -> dict[str, Any]:
        query = args.get("query") if isinstance(args.get("query"), str) else ""
        condition = args.get("condition") if isinstance(args.get("condition"), str) else ""
        intervention = args.get("intervention") if isinstance(args.get("intervention"), str) else ""
        if not (query.strip() or condition.strip() or intervention.strip()):
            raise ConnectorError("give at least one of 'query', 'condition' or 'intervention'")
        limit = bounded_int(args, "max_results", 10, 1, 50)
        params: dict[str, Any] = {
            "format": "json",
            "pageSize": limit,
            "countTotal": "true",
            "query.term": query.strip(),
            "query.cond": condition.strip(),
            "query.intr": intervention.strip(),
        }
        status = args.get("status")
        if status:
            statuses = [s.strip().upper() for s in str(status).split(",") if s.strip()]
            bad = [s for s in statuses if s not in _STATUSES]
            if bad:
                raise ConnectorError(f"unknown status {bad}; use one of {list(_STATUSES)}")
            params["filter.overallStatus"] = ",".join(statuses)
        data = http.get_json(f"{BASE}/studies", params)
        studies = data.get("studies", []) if isinstance(data, Mapping) else []
        return {
            "total_count": data.get("totalCount") if isinstance(data, Mapping) else None,
            "results": [_summary(s) for s in studies],
        }

    def trial_get(args: Mapping[str, Any]) -> dict[str, Any]:
        nct = require_str(args, "nct_id").upper()
        if not _NCT.match(nct):
            raise ConnectorError("'nct_id' must look like NCT01234567")
        study = http.get_json(f"{BASE}/studies/{nct}", {"format": "json"})
        if not isinstance(study, Mapping) or "protocolSection" not in study:
            raise ConnectorError(f"no study {nct}")
        return _detail(study)

    return [
        Tool(
            "trials_search",
            "Search ClinicalTrials.gov by free text, condition and/or intervention, optionally filtered by status.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text terms."},
                    "condition": {"type": "string", "description": "Condition or disease, e.g. 'glioblastoma'."},
                    "intervention": {"type": "string", "description": "Drug, device or procedure."},
                    "status": {"type": "string", "description": "Comma-separated overall statuses, e.g. 'RECRUITING'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "additionalProperties": False,
            },
            trials_search,
        ),
        Tool(
            "trial_get",
            "Read one ClinicalTrials.gov study: titles, status, phases, conditions, interventions, eligibility, outcomes.",
            {
                "type": "object",
                "properties": {"nct_id": {"type": "string", "description": "NCT number, e.g. NCT04368728."}},
                "required": ["nct_id"],
                "additionalProperties": False,
            },
            trial_get,
        ),
    ]


def _section(study: Mapping[str, Any], *path: str) -> Any:
    node: Any = study.get("protocolSection", {})
    for key in path:
        node = node.get(key, {}) if isinstance(node, Mapping) else {}
    return node


def _summary(study: Mapping[str, Any]) -> dict[str, Any]:
    ident = _section(study, "identificationModule")
    status = _section(study, "statusModule")
    design = _section(study, "designModule")
    return {
        "nct_id": ident.get("nctId"),
        "title": clip(ident.get("briefTitle"), 300),
        "status": status.get("overallStatus"),
        "phases": design.get("phases", []),
        "conditions": _section(study, "conditionsModule").get("conditions", [])[:8],
        "interventions": [
            i.get("name") for i in _section(study, "armsInterventionsModule").get("interventions", [])[:8]
        ],
        "start_date": (status.get("startDateStruct") or {}).get("date"),
        "sponsor": (_section(study, "sponsorCollaboratorsModule").get("leadSponsor") or {}).get("name"),
    }


def _detail(study: Mapping[str, Any]) -> dict[str, Any]:
    ident = _section(study, "identificationModule")
    status = _section(study, "statusModule")
    design = _section(study, "designModule")
    eligibility = _section(study, "eligibilityModule")
    outcomes = _section(study, "outcomesModule")
    locations = _section(study, "contactsLocationsModule").get("locations", [])
    return {
        **_summary(study),
        "official_title": clip(ident.get("officialTitle"), 500),
        "study_type": design.get("studyType"),
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "completion_date": (status.get("completionDateStruct") or {}).get("date"),
        "brief_summary": clip(_section(study, "descriptionModule").get("briefSummary"), 2000),
        "eligibility": {
            "sex": eligibility.get("sex"),
            "minimum_age": eligibility.get("minimumAge"),
            "maximum_age": eligibility.get("maximumAge"),
            "criteria": clip(eligibility.get("eligibilityCriteria"), 2500),
        },
        "primary_outcomes": [
            clip(o.get("measure"), 300) for o in outcomes.get("primaryOutcomes", [])[:5]
        ],
        "location_count": len(locations),
        "has_results": bool(study.get("hasResults")),
    }
