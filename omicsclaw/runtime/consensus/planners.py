"""Member planners (ADR 0016 L3) — the one genuine per-flavour behaviour.

Each planner reproduces a v1 wrapper's ``_plan_members`` verbatim:

- ``ChairLLMPlanner`` ← ``consensus-domains`` (evaluation chair / ``--all``
  from ``param_hints`` / explicit ``--members``).
- ``SweepPlanner``    ← ``sc-consensus-clustering`` (resolution sweep /
  ``--all`` leiden+louvain / explicit ``--members``).

The ``--members`` (explicit) branch is shared via ``_explicit_members``,
parameterised by the member skill's method flag and whether a colon (i.e. at
least one param) is required.
"""

from __future__ import annotations

from typing import Any

from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.plan import load_param_hints, propose_members
from omicsclaw.runtime.consensus.source_registry import ConsensusSource

DEFAULT_RESOLUTIONS = "0.5,0.8,1.0,1.4,2.0"
DEFAULT_METHODS = "leiden"

#: Default integration backends fanned out by ``sc-consensus-integration``.
#: ``none`` is the unintegrated ``X_pca`` baseline; the rest are cheap CPU
#: methods. scVI is opt-in (``--include-scvi``) because it is GPU/stochastic.
DEFAULT_INTEGRATION_METHODS = ("none", "harmony", "scanorama")

#: Default pseudotime methods fanned out by ``sc-consensus-pseudotime`` (ADR 0031).
#: Each emits a SINGLE global pseudotime; multi-lineage methods (slingshot/
#: monocle3/cellrank) are deferred (they re-introduce branching topology).
DEFAULT_PSEUDOTIME_METHODS = ("dpt", "palantir", "via")


def _explicit_members(
    spec: str,
    *,
    skill_name: str,
    method_key: str,
    require_colon: bool,
) -> list[ConsensusMember]:
    """Parse ``--members`` (``method[:k=v;k=v],...``) into a member list.

    ``method_key`` is ``"method"`` (spatial-domains) or ``"cluster-method"``
    (sc-clustering). ``require_colon`` rejects a bare method (sc-clustering
    members are meaningless without a resolution).
    """
    out: list[ConsensusMember] = []
    seen: set[str] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            method, params_spec = token.split(":", 1)
            params: dict[str, str] = {method_key: method}
            for kv in params_spec.split(";"):
                if "=" not in kv:
                    continue
                k, v = kv.split("=", 1)
                params[k.strip()] = v.strip()
        else:
            if require_colon:
                raise SystemExit(
                    f"Invalid member '{token}'; expected '<method>:resolution=<float>'"
                )
            method = token
            params = {method_key: method}
        suffix = "_".join(
            f"{k}-{v}" for k, v in sorted(params.items()) if k != method_key
        )
        name = f"{method}_{suffix}" if suffix else method
        if name in seen:
            raise SystemExit(
                f"duplicate member name '{name}' in --members; pick distinct params"
            )
        seen.add(name)
        out.append(ConsensusMember(name=name, skill_name=skill_name, params=params))
    return out


def _members_from_sweep(
    skill_name: str, methods: list[str], resolutions: list[float]
) -> list[ConsensusMember]:
    """Cartesian product of methods × resolutions (sc-consensus-clustering)."""
    out: list[ConsensusMember] = []
    for method in methods:
        for r in resolutions:
            r_str = str(r)
            out.append(
                ConsensusMember(
                    name=f"{method}_resolution-{r_str}",
                    skill_name=skill_name,
                    params={"cluster-method": method, "resolution": r_str},
                )
            )
    return out


class ChairLLMPlanner:
    """consensus-domains planning: chair LLM / ``--all`` param_hints / explicit."""

    def propose(self, args: Any, *, source: ConsensusSource) -> list[ConsensusMember]:
        if args.members:
            return _explicit_members(
                args.members,
                skill_name=source.member_skill,
                method_key="method",
                require_colon=False,
            )
        params_yaml = source.param_hints_path
        if args.all:
            hints = load_param_hints(params_yaml)
            return [
                ConsensusMember(
                    name=method,
                    skill_name=source.member_skill,
                    params={"method": method},
                )
                for method in sorted(hints.keys())
            ]
        planned = propose_members(
            query=getattr(args, "query", "") or "",
            skill_name=source.member_skill,
            parameters_yaml_path=params_yaml,
            n=5,
            domain=source.domain or "spatial",
            allow_offline=True,
        )
        return [p.to_consensus_member(skill_name=source.member_skill) for p in planned]


class SweepPlanner:
    """sc-consensus-clustering planning: resolution sweep / ``--all`` / explicit."""

    def propose(self, args: Any, *, source: ConsensusSource) -> list[ConsensusMember]:
        if args.members:
            return _explicit_members(
                args.members,
                skill_name=source.member_skill,
                method_key="cluster-method",
                require_colon=True,
            )
        if args.all:
            methods = ["leiden", "louvain"]
            resolutions = [float(r) for r in DEFAULT_RESOLUTIONS.split(",")]
        else:
            methods = [m.strip() for m in args.cluster_methods.split(",") if m.strip()]
            resolutions = [float(r) for r in args.resolutions.split(",")]
        return _members_from_sweep(source.member_skill, methods, resolutions)


class IntegrationRepSweepPlanner:
    """sc-consensus-integration planning: one member per integration backend.

    The member axis is the **batch-correction representation** (mirroring how
    ``ChairLLMPlanner`` fans out genuinely different spatial-domain algorithms),
    at a single **fixed** resolution so member cluster counts stay comparable
    for the categorical operator (the k-divergence guard, ADR 0029). Each member
    runs ``sc-integrate-cluster --method <m>`` and is named by the method
    (``none`` → ``unintegrated``).

    Method selection: ``--integration-methods`` (or ``--members``) as a comma
    list, else :data:`DEFAULT_INTEGRATION_METHODS` (+ ``scvi`` when
    ``--include-scvi``).
    """

    def propose(self, args: Any, *, source: ConsensusSource) -> list[ConsensusMember]:
        spec = args.members or getattr(args, "integration_methods", None)
        if spec:
            methods = [m.strip() for m in spec.split(",") if m.strip()]
            # Integration members are plain method names: resolution is FIXED for
            # every member (member-count comparability / k-divergence guard,
            # ADR 0029), so per-member ``method:param=...`` specs are
            # contradictory. Reject them early with a clear message rather than
            # forwarding an invalid ``--method`` that fails during fan-out.
            parameterized = [m for m in methods if ":" in m]
            if parameterized:
                raise SystemExit(
                    "integration consensus members are plain method names "
                    "(none/harmony/scanorama/scvi); per-member params are not "
                    "supported (resolution is fixed via --resolution). Got: "
                    f"{', '.join(parameterized)}"
                )
        else:
            methods = list(DEFAULT_INTEGRATION_METHODS)
            # ``--all`` selects every available backend (the default set + the
            # GPU/stochastic ``scvi`` member); ``--include-scvi`` adds only scvi.
            if getattr(args, "all", False) or getattr(args, "include_scvi", False):
                methods.append("scvi")

        resolution = str(getattr(args, "resolution", None) or 1.0)
        batch_key = getattr(args, "batch_key", None) or "batch"
        out: list[ConsensusMember] = []
        seen: set[str] = set()
        for method in methods:
            name = "unintegrated" if method == "none" else method
            if name in seen:
                raise SystemExit(f"duplicate integration method '{method}' in member spec")
            seen.add(name)
            out.append(
                ConsensusMember(
                    name=name,
                    skill_name=source.member_skill,
                    params={
                        "cluster-method": "leiden",
                        "method": method,
                        "resolution": resolution,
                        "batch-key": batch_key,
                    },
                )
            )
        return out


class PseudotimeMethodPlanner:
    """sc-consensus-pseudotime planning: one member per pseudotime method, shared root.

    The member axis is the **pseudotime method** (mirroring how the integration
    planner fans out genuinely different integration backends). v1 members are the
    single-global-pseudotime methods (``dpt``/``palantir``/``via``); multi-lineage
    methods are deferred. All members share **one user-specified root**
    (``--root-cluster`` / ``--root-cell``), **required** so direction is pinned
    (ADR 0031 §3) — enforced HERE in the flavour, not the member skill (which does
    not hard-require a root). Method selection: ``--pseudotime-methods`` (or
    ``--members``) as a comma list, else :data:`DEFAULT_PSEUDOTIME_METHODS`.
    """

    def propose(self, args: Any, *, source: ConsensusSource) -> list[ConsensusMember]:
        spec = args.members or getattr(args, "pseudotime_methods", None)
        if spec:
            methods = [m.strip() for m in spec.split(",") if m.strip()]
            parameterized = [m for m in methods if ":" in m]
            if parameterized:
                raise SystemExit(
                    "pseudotime consensus members are plain method names "
                    "(dpt/palantir/via); per-member params are not supported. "
                    f"Got: {', '.join(parameterized)}"
                )
        else:
            methods = list(DEFAULT_PSEUDOTIME_METHODS)

        # v1 member whitelist (ADR 0031 §3): only single-global-pseudotime methods.
        # Multi-lineage methods (slingshot_r/monocle3_r/cellrank) are deferred — they
        # re-introduce branching topology — and unknown methods are rejected before
        # fan-out rather than failing mid-run. Applies to --members and --pseudotime-methods.
        unknown = [m for m in methods if m not in DEFAULT_PSEUDOTIME_METHODS]
        if unknown:
            raise SystemExit(
                "sc-consensus-pseudotime v1 supports only the single-global-pseudotime "
                f"methods {list(DEFAULT_PSEUDOTIME_METHODS)}; got unsupported "
                f"{unknown}. Multi-lineage methods (slingshot/monocle3/cellrank) are "
                "deferred (ADR 0031 §3)."
            )

        root_cluster = getattr(args, "root_cluster", None)
        root_cell = getattr(args, "root_cell", None)
        if not root_cluster and not root_cell:
            raise SystemExit(
                "sc-consensus-pseudotime requires a shared root: pass "
                "--root-cluster <name> or --root-cell <id> (ADR 0031: a shared root "
                "pins pseudotime direction so the consensus is well-posed)."
            )

        out: list[ConsensusMember] = []
        seen: set[str] = set()
        for method in methods:
            if method in seen:
                raise SystemExit(f"duplicate pseudotime method '{method}' in member spec")
            seen.add(method)
            params: dict[str, str] = {"method": method}
            if root_cluster:
                params["root-cluster"] = str(root_cluster)
            if root_cell:
                params["root-cell"] = str(root_cell)
            out.append(
                ConsensusMember(name=method, skill_name=source.member_skill, params=params)
            )
        return out
