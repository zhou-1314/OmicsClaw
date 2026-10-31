# SPDX-License-Identifier: Apache-2.0
"""Literature connector: PubMed (NCBI E-utilities), bioRxiv / medRxiv, OpenAlex.

Public APIs used:

* NCBI E-utilities ``esearch`` / ``esummary`` / ``efetch``
  (https://www.ncbi.nlm.nih.gov/books/NBK25501/). ``NCBI_API_KEY`` raises
  the allowed request rate from three to ten per second.
* bioRxiv / medRxiv ``details`` API (https://api.biorxiv.org/). It has no
  full-text search, so ``biorxiv_search`` filters a window of recent
  postings by keyword, or looks one preprint up by DOI.
* OpenAlex ``works`` (https://docs.openalex.org/). ``OPENALEX_MAILTO``
  joins the polite pool.
"""

from __future__ import annotations

import datetime as _dt
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any

from ._common import (
    ConnectorError,
    HttpClient,
    Tool,
    bounded_int,
    clip,
    env_value,
    require_str,
)

NAME = "literature"
INSTRUCTIONS = (
    "Biomedical literature lookup. Use pubmed_search to find PMIDs, then "
    "pubmed_fetch for abstracts; biorxiv_search for recent preprints; "
    "openalex_works for broad scholarly search with citation counts."
)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BIORXIV = "https://api.biorxiv.org"
OPENALEX = "https://api.openalex.org"


def ncbi_params(**params: Any) -> dict[str, Any]:
    """E-utilities parameters with the tool name and optional key/email."""
    params.setdefault("tool", "omicsclaw")
    key = env_value("NCBI_API_KEY")
    if key:
        params["api_key"] = key
    email = env_value("NCBI_EMAIL")
    if email:
        params["email"] = email
    return params


def ncbi_client() -> HttpClient:
    """A client spaced to NCBI's published rate limit."""
    interval = 0.11 if env_value("NCBI_API_KEY") else 0.34
    return HttpClient(min_interval_s={"eutils.ncbi.nlm.nih.gov": interval})


def build_tools(client: HttpClient | None = None) -> list[Tool]:
    http = client or ncbi_client()

    def pubmed_search(args: Mapping[str, Any]) -> dict[str, Any]:
        query = require_str(args, "query")
        limit = bounded_int(args, "max_results", 10, 1, 50)
        sort = args.get("sort") or "relevance"
        if sort not in ("relevance", "pub_date"):
            raise ConnectorError("'sort' must be 'relevance' or 'pub_date'")
        params = ncbi_params(
            db="pubmed", term=query, retmode="json", retmax=limit, sort=sort
        )
        for key in ("mindate", "maxdate"):
            if args.get(key):
                params[key] = str(args[key])
                params["datetype"] = "pdat"
        found = http.get_json(f"{EUTILS}/esearch.fcgi", params)
        result = found.get("esearchresult", {}) if isinstance(found, Mapping) else {}
        if "ERROR" in result:
            raise ConnectorError(str(result["ERROR"]))
        pmids = [str(p) for p in result.get("idlist", [])]
        records = _pubmed_summaries(http, pmids) if pmids else []
        return {
            "query": query,
            "total_count": int(result.get("count", 0) or 0),
            "pmids": pmids,
            "results": records,
        }

    def pubmed_fetch(args: Mapping[str, Any]) -> dict[str, Any]:
        raw = args.get("pmids")
        if isinstance(raw, str):
            pmids = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        elif isinstance(raw, list):
            pmids = [str(p).strip() for p in raw if str(p).strip()]
        else:
            raise ConnectorError("'pmids' must be a list of PubMed IDs")
        if not pmids or not all(p.isdigit() for p in pmids):
            raise ConnectorError("'pmids' must contain numeric PubMed IDs")
        pmids = pmids[:20]
        xml_text = http.get_text(
            f"{EUTILS}/efetch.fcgi",
            ncbi_params(db="pubmed", id=",".join(pmids), retmode="xml", rettype="abstract"),
        )
        articles = _parse_pubmed_xml(xml_text)
        if not articles:
            raise ConnectorError(f"no PubMed records found for {', '.join(pmids)}")
        return {"articles": articles}

    def biorxiv_search(args: Mapping[str, Any]) -> dict[str, Any]:
        server = args.get("server") or "biorxiv"
        if server not in ("biorxiv", "medrxiv"):
            raise ConnectorError("'server' must be 'biorxiv' or 'medrxiv'")
        doi = (args.get("doi") or "").strip() if isinstance(args.get("doi"), str) else ""
        if doi:
            data = http.get_json(f"{BIORXIV}/details/{server}/{doi}/na/json")
            items = data.get("collection", []) if isinstance(data, Mapping) else []
            if not items:
                raise ConnectorError(f"no {server} preprint with DOI {doi}")
            return {"server": server, "results": [_preprint(i) for i in items[-1:]]}
        query = require_str(args, "query")
        days = bounded_int(args, "days", 30, 1, 365)
        limit = bounded_int(args, "max_results", 10, 1, 50)
        category = args.get("category") if isinstance(args.get("category"), str) else ""
        end = _dt.date.today()
        start = end - _dt.timedelta(days=days)
        terms = [t.lower() for t in query.split() if t.strip()]
        hits: list[dict[str, Any]] = []
        scanned = 0
        cursor = 0
        for _ in range(10):  # at most 1000 postings scanned
            params = {"category": category.strip().replace(" ", "_")} if category else None
            data = http.get_json(
                f"{BIORXIV}/details/{server}/{start.isoformat()}/{end.isoformat()}/{cursor}/json",
                params,
            )
            items = data.get("collection", []) if isinstance(data, Mapping) else []
            if not items:
                break
            for item in items:
                scanned += 1
                haystack = f"{item.get('title', '')} {item.get('abstract', '')}".lower()
                if all(term in haystack for term in terms):
                    hits.append(_preprint(item))
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit or len(items) < 100:
                break
            cursor += len(items)
        return {
            "server": server,
            "query": query,
            "window": f"{start.isoformat()}..{end.isoformat()}",
            "postings_scanned": scanned,
            "results": hits,
            "note": "Keyword filter over postings in the window (newest pages first are not guaranteed).",
        }

    def openalex_works(args: Mapping[str, Any]) -> dict[str, Any]:
        query = require_str(args, "query")
        limit = bounded_int(args, "max_results", 10, 1, 50)
        filters = []
        if args.get("from_year"):
            filters.append(f"from_publication_date:{int(args['from_year'])}-01-01")
        if args.get("to_year"):
            filters.append(f"to_publication_date:{int(args['to_year'])}-12-31")
        params: dict[str, Any] = {
            "search": query,
            "per-page": limit,
            "select": (
                "id,doi,display_name,publication_year,cited_by_count,type,"
                "primary_location,authorships,abstract_inverted_index,open_access"
            ),
        }
        if filters:
            params["filter"] = ",".join(filters)
        mailto = env_value("OPENALEX_MAILTO")
        if mailto:
            params["mailto"] = mailto
        data = http.get_json(f"{OPENALEX}/works", params)
        works = data.get("results", []) if isinstance(data, Mapping) else []
        meta = data.get("meta", {}) if isinstance(data, Mapping) else {}
        return {
            "query": query,
            "total_count": meta.get("count"),
            "results": [_openalex_work(w) for w in works],
        }

    return [
        Tool(
            "pubmed_search",
            "Search PubMed and return matching PMIDs with title, journal, date and first authors.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PubMed query, e.g. 'TP53 AND spatial transcriptomics'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "sort": {"type": "string", "enum": ["relevance", "pub_date"], "default": "relevance"},
                    "mindate": {"type": "string", "description": "Earliest publication date, YYYY or YYYY/MM/DD."},
                    "maxdate": {"type": "string", "description": "Latest publication date, YYYY or YYYY/MM/DD."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            pubmed_search,
        ),
        Tool(
            "pubmed_fetch",
            "Fetch PubMed records (title, abstract, journal, year, authors, DOI) for up to 20 PMIDs.",
            {
                "type": "object",
                "properties": {
                    "pmids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                        "description": "PubMed IDs.",
                    }
                },
                "required": ["pmids"],
                "additionalProperties": False,
            },
            pubmed_fetch,
        ),
        Tool(
            "biorxiv_search",
            "Find bioRxiv / medRxiv preprints: by DOI, or by keywords over postings from the last N days.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords that must all appear in title or abstract."},
                    "doi": {"type": "string", "description": "Look up one preprint by DOI instead of searching."},
                    "server": {"type": "string", "enum": ["biorxiv", "medrxiv"], "default": "biorxiv"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                    "category": {"type": "string", "description": "Subject category, e.g. 'bioinformatics'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "additionalProperties": False,
            },
            biorxiv_search,
        ),
        Tool(
            "openalex_works",
            "Search scholarly works in OpenAlex with year filters; returns DOI, venue, citations and abstract.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "from_year": {"type": "integer"},
                    "to_year": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            openalex_works,
        ),
    ]


def _pubmed_summaries(http: HttpClient, pmids: list[str]) -> list[dict[str, Any]]:
    data = http.get_json(
        f"{EUTILS}/esummary.fcgi",
        ncbi_params(db="pubmed", id=",".join(pmids), retmode="json"),
    )
    result = data.get("result", {}) if isinstance(data, Mapping) else {}
    records = []
    for pmid in pmids:
        item = result.get(pmid)
        if not isinstance(item, Mapping):
            continue
        doi = next(
            (a.get("value") for a in item.get("articleids", []) if a.get("idtype") == "doi"),
            None,
        )
        authors = [a.get("name") for a in item.get("authors", [])[:3] if a.get("name")]
        records.append(
            {
                "pmid": pmid,
                "title": clip(item.get("title"), 400),
                "journal": item.get("fulljournalname") or item.get("source"),
                "pubdate": item.get("pubdate"),
                "authors": authors,
                "doi": doi,
            }
        )
    return records


def _parse_pubmed_xml(text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ConnectorError(f"PubMed returned unreadable XML: {exc}") from None
    articles = []
    for node in root.iter("PubmedArticle"):
        citation = node.find("MedlineCitation")
        if citation is None:
            continue
        article = citation.find("Article")
        pmid = (citation.findtext("PMID") or "").strip()
        title = "".join(article.find("ArticleTitle").itertext()) if article is not None and article.find("ArticleTitle") is not None else ""
        parts = []
        if article is not None:
            for block in article.findall("Abstract/AbstractText"):
                label = block.get("Label")
                body = "".join(block.itertext()).strip()
                parts.append(f"{label}: {body}" if label else body)
        authors = []
        if article is not None:
            for author in article.findall("AuthorList/Author")[:6]:
                last = author.findtext("LastName") or author.findtext("CollectiveName") or ""
                initials = author.findtext("Initials") or ""
                if last:
                    authors.append(f"{last} {initials}".strip())
        year = (
            (article.findtext("Journal/JournalIssue/PubDate/Year") if article is not None else None)
            or (article.findtext("Journal/JournalIssue/PubDate/MedlineDate") if article is not None else None)
        )
        doi = None
        for aid in node.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()
        articles.append(
            {
                "pmid": pmid,
                "title": clip(title, 500),
                "journal": article.findtext("Journal/Title") if article is not None else None,
                "year": year,
                "authors": authors,
                "doi": doi,
                "abstract": clip("\n".join(parts), 4000),
            }
        )
    return articles


def _preprint(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "doi": item.get("doi"),
        "title": clip(item.get("title"), 400),
        "authors": clip(item.get("authors"), 200),
        "date": item.get("date"),
        "version": item.get("version"),
        "category": item.get("category"),
        "published": item.get("published") if item.get("published") not in (None, "NA") else None,
        "abstract": clip(item.get("abstract"), 1500),
    }


def _openalex_work(work: Mapping[str, Any]) -> dict[str, Any]:
    location = work.get("primary_location") or {}
    source = location.get("source") or {} if isinstance(location, Mapping) else {}
    authors = [
        (a.get("author") or {}).get("display_name")
        for a in (work.get("authorships") or [])[:5]
    ]
    return {
        "id": work.get("id"),
        "doi": work.get("doi"),
        "title": clip(work.get("display_name"), 400),
        "year": work.get("publication_year"),
        "type": work.get("type"),
        "venue": source.get("display_name") if isinstance(source, Mapping) else None,
        "cited_by_count": work.get("cited_by_count"),
        "open_access": (work.get("open_access") or {}).get("is_oa"),
        "authors": [a for a in authors if a],
        "abstract": clip(_inverted_abstract(work.get("abstract_inverted_index")), 1500),
    }


def _inverted_abstract(index: Any) -> str:
    if not isinstance(index, Mapping):
        return ""
    positions: list[tuple[int, str]] = []
    for word, spots in index.items():
        if isinstance(spots, list):
            positions.extend((int(p), str(word)) for p in spots if isinstance(p, int))
    return " ".join(word for _, word in sorted(positions))
