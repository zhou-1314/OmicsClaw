# CLAUDE.md — OmicsClaw repository

`OMICSCLAW.md` is the runtime contract of the OmicsClaw analysis agent:
`omicsclaw/entry/assembly.py` puts it at the top of that agent's system
prompt, followed by `SAFETY_RULES` and `TOOL_GUIDANCE` from the same module.
Read it when a change affects what that agent is told, and when adding or
removing a skill (its routing table and counts there are kept by hand).

## Repository Maintenance Contract

When you are acting on repository maintenance, refactoring, or other
developer-facing tasks rather than end-user omics analysis:

1. Read `README.md` for what the project is and `CHANGELOG.md` for recent
   decisions and milestones.
2. Then read root `SPEC.md` and `AGENTS.md`.
3. Reply in the user's language and stay concise and execution-focused.
4. Use a concise plan and root-cause debugging for non-trivial repository
   changes. Test by Risk-Matched Verification (`SPEC.md`): choose the level
   from what the change can break, run that level's checks, and report what
   you ran and what you did not. Do not run the full suite by default.
5. When you make an important repository decision or complete a milestone,
   add an entry at the top of `CHANGELOG.md`. Change `README.md` only when a
   user-facing entry point, install step or headline feature changes; its
   What's New holds at most five items of one sentence each.
6. Run the `humanizer` skill (`.claude/skills/humanizer/SKILL.md`) in
   embedded mode over prose you write: code comments, docstrings, docs,
   plans, README text, commit messages and PR descriptions. Keep code,
   commands, paths and identifiers unchanged.

## Agent skills

### Issue tracker

Issues and PRDs live in GitHub Issues for `zhou-1314/OmicsClaw`. Use the `gh`
CLI (`gh issue create|view|list|comment|edit|close`), and infer the repository
from the current clone unless a command needs an explicit
`--repo zhou-1314/OmicsClaw`. "Publish to the issue tracker" means create an
issue; "fetch the relevant ticket" means read its body, labels and comments.

### Triage labels

Five canonical roles, each mapping to the GitHub label of the same name:

| Label | Meaning |
| --- | --- |
| `needs-triage` | A maintainer must evaluate the issue. |
| `needs-info` | More information is required from the reporter. |
| `ready-for-agent` | Fully specified and safe for an AFK agent. |
| `ready-for-human` | Requires human implementation or judgement. |
| `wontfix` | Will not be actioned. |

The first four may not exist yet. Create one only when a workflow first needs
to apply it; never create or mutate labels during read-only diagnosis.


=== SCOPE LIMITS (these bound what you PROPOSE, never what you look for) ===
Report anything that is actually wrong here — including a rare-looking case, if
this project actually produces it. Then keep the fix in scope:
1. This is not a security paper. Verification is welcome; over-defense is not.
   Unless this project states otherwise, assume a cooperating operator on their
   own machine; if it has a real adversary, it will say so and that scope wins.
2. Do not add hashes, checksums or fingerprints unless the hash replaces a
   materially more expensive operation AND its result changes what happens next.
3. No defensive scaffolding: no feature flags, migration frameworks, compat
   layers or wrappers for cases that do not occur here.
4. No corner-case obsession: exotic encodings, symlink races, RTL text and
   millisecond races are out of scope unless the case is reachable through this
   project's supported use — its documented inputs, its published interface, its
   real data. Reachable is enough; you do not need a reproduction. Constructible
   in principle is not enough.
5. Where judgement is needed, judge. Do not replace it with a scoring table, a
   checklist, or a re-verification loop over something already settled.
6. None of this overrides security, migration, verification or review that the
   user, this project's own conventions, or a higher-priority rule asked for.
   Those were requested; they are the work, not scope creep.
7. Deliverable text is not a defense transcript. State plainly what holds;
   collect caveats in one section (Limitations, Known Issues) instead of
   sprinkling a disclaimer into every paragraph; and never write instructions
   into the product — nor the process: "do not mention X" means X is absent
   (not "we do not address X"), and intermediate errors, abandoned approaches
   and revision history are not content either. Lead with the strongest
   result — a paper is a launch, not a progress report. An unfavorable
   number that is a tradeoff is explained as one; one that is not is stated
   plainly — neither is narrated as a defeat, and the number stays in the
   table.
8. Momentum is part of the deliverable. When several reasonable approaches
   exist, pick one and note the tradeoff — escalate to a question only when
   the options genuinely diverge or the choice is hard to reverse. A detail
   that does not block the goal gets recorded, not solved first. Stalling to
   avoid picking wrong is itself a wrong pick.
9. No generation or correction traces: the final deliverable (code, comments,
   docstrings, commit messages, PR descriptions, summaries) presents only the
   correct end result, as if it had been written that way from the start — no
   intermediate errors, no trial-and-error, no abandoned approaches, no "why we
   didn't do X". No AI-assistance markers either: no "Generated by /
   Co-authored-by: Claude/Codex" signatures, no model-voice transitions ("it is
   worth noting", "in summary", "firstly... secondly... finally"), no "here
   is..." openers. Match the wording and level of detail to the project's
   existing style.
Shapes already seen, for calibration. Examples, not a checklist — a real finding
is not dismissed by resembling one:
  H  hashing every row of two spreadsheets to answer what comparing cells answers
  H  writing checksum files that nothing ever reads
  E  hardening the accounts of an app that has no users and no deployment
  R  auditing your own patch all night while the feature stays unwritten
  R  a reviewer that returns a failing verdict on everything
  O  guards whose justification is the previous guard, not the requirement
And two that look like the above and are not. Report these:
  ✓  a digest that lets you skip re-reading a large file you already have
  ✓  a rare-looking input this project's own documentation example produces
Before running any check, answer: what specific failure would this detect, and
what would I do differently if it occurred? No answer means do not run it.
Say plainly when something is correct. Do not manufacture findings.
