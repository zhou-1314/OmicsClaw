# Triage labels

Engineering skills use five canonical triage roles. Each role maps directly to
the corresponding GitHub label.

| Canonical role | GitHub label | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | A maintainer must evaluate the issue. |
| `needs-info` | `needs-info` | More information is required from the reporter. |
| `ready-for-agent` | `ready-for-agent` | Fully specified and safe for an AFK agent. |
| `ready-for-human` | `ready-for-human` | Requires human implementation or judgement. |
| `wontfix` | `wontfix` | Will not be actioned. |

The first four labels may not exist yet. Create them only when a workflow first
needs to apply them; do not create or mutate labels during read-only diagnosis.
