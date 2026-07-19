# Issue tracker: GitHub

Issues and PRDs for this repository live in GitHub Issues for
`zhou-1314/OmicsClaw`. Use the `gh` CLI for issue operations.

## Conventions

- Create: `gh issue create --title "..." --body "..."`.
- Read: `gh issue view <number> --comments`.
- List: use `gh issue list` with explicit state, label, and JSON filters.
- Comment: `gh issue comment <number> --body "..."`.
- Apply or remove labels with `gh issue edit`.
- Close with `gh issue close <number> --comment "..."`.

Infer the repository from the current clone unless a command needs an explicit
`--repo zhou-1314/OmicsClaw` argument.

## Skill vocabulary

- "Publish to the issue tracker" means create a GitHub issue.
- "Fetch the relevant ticket" means read the issue body, labels, and comments.
