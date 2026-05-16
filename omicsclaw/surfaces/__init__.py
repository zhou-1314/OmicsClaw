"""OmicsClaw ingress umbrella — the three Surfaces that face users.

Per ``docs/CONTEXT.md`` §"Surfaces", a Surface is a user-facing entry
point. All three Surfaces iterate the typed event stream from
``runtime.agent.dispatcher.dispatch`` (per ADR 0006); the only
differences are how they accept input (IM message / HTTP request /
terminal) and how they render the events back to the user.

Subpackages:
    channels/  — Channel Surface (10 IM platform adapters + ChannelManager)
    desktop/   — Desktop Surface (FastAPI server for Electron/Next.js frontends)
    cli/       — CLI Surface (interactive REPL, Textual TUI, setup wizard, console entry)

See docs/adr/0005-surfaces-umbrella-for-ingress.md for the boundary
rationale.
"""
