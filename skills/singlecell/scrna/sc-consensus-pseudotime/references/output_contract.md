# sc-consensus-pseudotime — output contract

All artifacts land under `analysis://typed/<run_id>` (A path). `report.md`'s first
line is always `[A: Verified consensus]` (enforced; ADR 0010).

| File | Contents |
|---|---|
| `consensus_pseudotime.tsv` | `observation`, `consensus_pseudotime` ([0,1], re-ranked), `pseudotime_mad` (2·MAD, [0,1]), `range` (max−min of aligned ranks) |
| `member_agreement_spearman.csv` | symmetric pairwise Spearman matrix over the aligned members |
| `member_scores.csv` | per-member `composite` (= `agreement_mean`), `selected`, `selection_reason` |
| `selection_audit.json` | `top_k_candidates`, `voting_bcs`, `anchor`, `flipped_members`, `dropped_degenerate` |
| `plan.json` | members + `operator`, `alpha=1.0`, `beta=0.0`, `intrinsic_panel="none"`, `template="continuous"` |
| `report.md` | banner + agreement matrix + weak-agreement guard + dispersion summary |

The driver returns a `ContinuousConsensusRun` dataclass (the in-process contract);
downstream consumers program against it, not the filesystem.
