# Local LLM deployment is a Provider + runbook, not a Skill

**Status:** accepted (2026-06-05)

A user wanted to run OmicsClaw with **no API-key cost** by deploying Gemma 4
locally, and proposed importing the `gemma4-local-deploy` resource from
`majiayu000/spellbook` as a new OmicsClaw **skill**. That resource is a
**macOS / Apple-Metal `llama.cpp` runbook** that serves an OpenAI-compatible
endpoint at `127.0.0.1:8080` (Ollama is its secondary path). It is a deployment
runbook for the *model itself*, not an omics analysis.

This is a category mismatch against OmicsClaw's vocabulary. A **Skill** is an
omics analysis the LLM *routes to and calls as a tool* (`spatial-preprocess`,
`sc-de`, …). A local LLM is the agent's **brain**, which OmicsClaw already
models as a **Provider**. The LLM can never meaningfully "call deploy-gemma" as
an analysis step.

The provider plumbing already exists (verified 2026-06-05):

- `omicsclaw/providers/registry.py` ships an `ollama` preset
  (`http://localhost:11434/v1`, `:100`) and a `custom` OpenAI-compatible preset
  (`:101`); the curated Ollama model list (`:205-229`) already includes
  `gemma4:e4b` / `gemma4:26b`.
- `omicsclaw/providers/patches.py` classifies `gemma4` as **tool-capable**
  (`:164`) and `gemma3` / `gemma2` / `gemma` as **tool-incapable**
  (`:167-192`), and discovers installed tags via `discover_ollama_models`.
- `omicsclaw/surfaces/desktop/server.py` resolves the provider from env at
  startup (`:214`, `:387`), accepts a per-request `provider_config` override
  (`:555`, `:1211-1251`), and serves live Ollama discovery via
  `/providers/options` → `discover_ollama_models_async` (`:4440`).

## Decision

Local-LLM deployment is delivered as **(1) provider configuration + (2) an
operational runbook**, never a `skills/` entry.

1. **Serving path = Ollama (recommended).** Cross-platform, CUDA-accelerated,
   and already wired into the provider registry and the desktop discovery
   endpoint. The spellbook's macOS/Metal `llama.cpp` path is documented only as
   an advanced alternative reached through the existing `custom`
   OpenAI-compatible provider — not the default.
2. **Model must be Gemma 4.** `gemma4:*` is tool-capable and can drive the
   skill-routing agent loop; **`gemma3:12b` is excluded** because it cannot
   (it would break tool routing). The runbook presents a resource → model
   table (`e4b` / `12b` / `26b` / `31b`) so users pick by available VRAM.
   `gemma4:12b` is added to the curated registry list so the desktop model
   picker offers it before the user pulls anything.
3. **Topology: provider config resolves server-side.** In the supported
   laptop-App + remote-backend topology, the backend (which makes the LLM
   calls) runs on the same host as Ollama, so `base_url=http://localhost:11434/v1`
   correctly targets the server's Ollama and Ollama stays bound to `127.0.0.1`
   — no external exposure required.

## Considered alternative (rejected)

**Author it as a `skills/.../SKILL.md`.** Rejected: a deployment runbook is not
an omics analysis. It would pollute `skills/catalog.json` and the routing table,
and the LLM could never route to it — the Skill abstraction is for analyses the
model invokes, not for standing up the model.

## Consequences

- Runbook lives at `docs/engineering/local-llm-gemma.mdx` (next to
  `remote-execution.mdx`), with a pointer from the App repo's
  `docs/LOCAL_SETUP_GUIDE.md` / `LOCAL_SETUP_GUIDE_zh.md`.
- `omicsclaw/providers/registry.py` gains `gemma4:12b` (and optionally
  `gemma4:e2b` / `gemma4:31b`) in the curated Ollama list.
- **Expectation set explicitly in the runbook:** local small models drive
  OmicsClaw's multi-turn, multi-tool agent loop **less reliably** than frontier
  APIs (Claude/GPT/DeepSeek) — occasional missed tool calls or wrong arguments
  on complex multi-omics routing. This is the cost of zero API spend.
- No change to `skills/`, the catalog, or the routing table.
