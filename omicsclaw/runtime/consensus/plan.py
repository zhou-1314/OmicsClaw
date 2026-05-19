"""Evaluation chair — member-selection planner.

The chair reads a skill's ``parameters.yaml`` ``param_hints`` block plus
optional data features (e.g. ``n_obs``, ``technology``, ``has_spatial``)
and chooses N ``(method, params)`` combinations to fan out.

Two operating modes:

- **LLM mode** (default when ``LLM_API_KEY`` is set): the chair LLM gets
  the skill hints + data features + user query and returns a JSON plan.
- **Deterministic fallback** (offline / no API key / parse failure): pick
  the first ``n`` methods sorted lexicographically, each with their
  default parameter values from ``param_hints``.

The fallback is documented in ADR 0010 §"Operational defaults" — it is
intentional, not a workaround.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from omicsclaw.runtime.consensus.member import ConsensusMember

logger = logging.getLogger(__name__)


_PLAN_PROMPT = """You are an expert in {domain} omics analysis acting as the
evaluation chair for a multi-method consensus run. Pick {n} promising
``(method, params)`` combinations to fan out in parallel.

User query: {query}

Skill: {skill}
Available methods (from skill's parameters.yaml param_hints):
{methods_block}

Data features (may be empty if the caller does not know):
{features_block}

Constraints:
- Return exactly {n} entries.
- Each entry: {{"method": "<method>", "params": {{"<flag>": "<value>", ...}}, "rationale": "<one line>"}}.
- Cover methods of different algorithmic families when possible (graph-based, GNN-based,
  autoencoder-based, classical Louvain/Leiden, ...) to maximise consensus signal.
- Use defaults from param_hints unless data features suggest a different value.

Respond with ONLY a JSON object: {{"members": [ ... ]}}.
"""


@dataclass(frozen=True)
class PlannedMember:
    method: str
    params: dict[str, str]
    rationale: str

    def to_consensus_member(self, *, skill_name: str) -> ConsensusMember:
        """Build a ``ConsensusMember`` ready for fan-out.

        Embeds method + ordered param tags into the unique member name so two
        leiden members with different resolutions get distinct subdirs. The
        source skill's ``MemberArtifactReader`` (registered in
        ``source_registry.py``) is what knows how to read this member's
        outputs — the member itself does not.
        """
        suffix = "_".join(f"{k}-{v}" for k, v in sorted(self.params.items()) if k != "method")
        name = f"{self.method}_{suffix}" if suffix else self.method
        params = {"method": self.method, **self.params}
        return ConsensusMember(name=name, skill_name=skill_name, params=params)


def load_param_hints(parameters_yaml_path: Path) -> dict[str, Any]:
    """Read ``param_hints`` from a skill's ``parameters.yaml``.

    Returns ``{}`` if the file or the block is missing — the planner then
    falls back to the user-supplied ``--members`` list (callers must enforce).
    """
    if not parameters_yaml_path.exists():
        return {}
    data = yaml.safe_load(parameters_yaml_path.read_text()) or {}
    hints = data.get("param_hints") or {}
    if not isinstance(hints, dict):
        return {}
    return hints


def _deterministic_plan(
    param_hints: Mapping[str, Any], n: int
) -> list[PlannedMember]:
    """Fallback planner: pick first N methods lexicographically with their defaults."""
    chosen_methods = sorted(param_hints.keys())[:n]
    members: list[PlannedMember] = []
    for method in chosen_methods:
        defaults = (param_hints[method] or {}).get("defaults") or {}
        params: dict[str, str] = {
            key.replace("_", "-"): str(value) for key, value in defaults.items()
        }
        members.append(
            PlannedMember(
                method=method,
                params=params,
                rationale="deterministic fallback (default params)",
            )
        )
    return members


def _parse_llm_plan(content: str, available_methods: set[str]) -> list[PlannedMember]:
    """Extract a member list from LLM output. Raises ``ValueError`` on bad JSON."""
    parsed = json.loads(content)
    raw_members = parsed.get("members")
    if not isinstance(raw_members, list):
        raise ValueError("LLM plan missing 'members' list")
    out: list[PlannedMember] = []
    for entry in raw_members:
        if not isinstance(entry, dict):
            continue
        method = str(entry.get("method", "")).strip()
        if method not in available_methods:
            logger.warning("plan: dropping unknown method %r", method)
            continue
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            continue
        out.append(
            PlannedMember(
                method=method,
                params={str(k): str(v) for k, v in params.items()},
                rationale=str(entry.get("rationale", "")),
            )
        )
    return out


def _call_chair_llm(prompt: str, timeout: float = 30.0) -> str | None:
    """Best-effort chair LLM call; ``None`` on failure so callers fall back."""
    from omicsclaw.providers.chat_completion import call_chat_completion

    return call_chat_completion(prompt, timeout=timeout)


def propose_members(
    *,
    query: str,
    skill_name: str,
    parameters_yaml_path: Path,
    n: int = 5,
    data_features: Mapping[str, object] | None = None,
    domain: str = "spatial",
    allow_offline: bool = True,
    chair_llm: Any = None,
) -> list[PlannedMember]:
    """Produce a list of planned members for a fan-out.

    Parameters
    ----------
    query :
        User's natural-language query (or empty string for a script-driven
        run; the chair still falls back to deterministic ordering).
    skill_name :
        Skill alias to fan out.
    parameters_yaml_path :
        Path to the skill's ``parameters.yaml``; ``param_hints`` is read.
    n :
        Number of members to return (default 5 per ADR 0010).
    data_features :
        Optional structured features (n_obs, technology, has_spatial, ...)
        passed verbatim into the LLM prompt.
    domain :
        Coarse domain label for prompt framing (``spatial`` / ``singlecell``
        / ``genomics`` / ...).
    allow_offline :
        When ``True`` (default), missing LLM API key falls back deterministically
        without raising. Set ``False`` in CI / tests that explicitly require LLM.
    chair_llm :
        Optional callable ``(prompt: str) -> str | None`` injected for tests
        so we never hit a live API. When ``None`` we use ``_call_chair_llm``.
    """
    hints = load_param_hints(parameters_yaml_path)
    if not hints:
        return []

    if chair_llm is None:
        chair_llm = _call_chair_llm

    available = set(hints.keys())
    methods_block = "\n".join(
        f"- {m}: defaults={(hints[m] or {}).get('defaults', {})}; "
        f"params={(hints[m] or {}).get('params', [])}"
        for m in sorted(available)
    )
    features_block = (
        json.dumps(dict(data_features), default=str) if data_features else "(none)"
    )
    prompt = _PLAN_PROMPT.format(
        domain=domain,
        n=n,
        query=query or "(none)",
        skill=skill_name,
        methods_block=methods_block,
        features_block=features_block,
    )

    content = chair_llm(prompt)
    if content:
        try:
            members = _parse_llm_plan(content, available)
            if len(members) >= 2:
                return members[:n]
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("plan: invalid LLM JSON (%s); falling back", exc)

    if allow_offline:
        return _deterministic_plan(hints, n)
    raise RuntimeError(
        "LLM plan unavailable and allow_offline=False. "
        "Set LLM_API_KEY or call propose_members(allow_offline=True)."
    )
