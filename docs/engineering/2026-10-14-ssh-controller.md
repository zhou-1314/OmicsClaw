# SSH controller feasibility slice

The Desktop migration uses a finite Python process behind system OpenSSH. The Mac runs Node and holds model credentials. The process receives a nonsecret model snapshot, builds the existing `AgentApp`, attaches the existing session registry and projects its events through the existing Desktop projector. It opens no product HTTP port. This is a Stage 1 implementation; the released Desktop still uses its existing modes until later migration stages pass.

## Shared behavior

`build_app` remains the single registry, permission, hook, budget, memory and subagent composition root. A deployment can supply model limits and disable Notebook services. `WorkspaceKernelBinding` forwards an explicitly selected science interpreter to the existing kernel manager. CLI defaults remain unchanged.

`JobsManager(register_default_runners=False)` provides the existing job store, run projections, approval broker and events without constructing `SkillApiRunner` or a fallback kernel. `ExternalScriptRunner` demonstrates the runner seam with an independent science process. Its Stage 1 lifetime is a foreground child of the job task. Durable background submission and reconciliation are Stage 3 work; a new process alone does not make a job survive controller shutdown.

The controller adds admission receipts to `DurableTurnEventLog` and uses them in `SessionRegistry`. A receipt binds a persisted authority/workspace identity, session, source request, canonical payload hash and turn before scheduling. The shared memory database owns two additive tables: `turn_ingress` records admission and `turn_authority_identity` supplies a singleton identity across controller restarts. Neither stores terminal state, a retry decision or a second queue. Receipts remain for the workspace lifetime; removing them requires an explicit archival/migration policy. A copied database retains its provenance identity until migration deliberately assigns a new origin.

Bootstrap initializes that metadata under the workspace fence, closes its temporary database connection, then `build_app` opens the single business connection used by the application. The hello identity and the configured session identity must agree. A headless profile without durable admission refuses activation. Permission selection is included in the admission hash and applied after receipt checking, so a rejected duplicate cannot change an approval profile. After restart, a repeated admission cannot start another turn. Existing turn events and session stores remain the sources of progress and history.

## Model bridge

Both provider adapters accept a public lazy client factory and preserve it through `bind`. The controller factory constructs real vendor SDK clients through their public `http_client` argument. A shared restricted transport carries HTTP status and response bytes over reverse RPC. The Mac chooses the endpoint and credentials from its own settings, enforces model and output limits, refuses remote identity headers and URLs, and cancels in-flight fetches on EOF. Redirects are refused. Only one layer owns SDK retries.

The controller profile locks OpenAI 2.33.0, Anthropic 0.125.0 and httpx 0.28.1. Anthropic 1.13.0 was evaluated: it moved to httpx2 and removed the public `temperature` argument used by the current adapter. A small public transport type adapter passed the HTTP construction boundary but did not preserve that API contract. The controller therefore locks the existing codec-compatible SDK until an upgrade passes the same real SDK tests and the full provider suite. This does not change the CLI's dependency declarations. No SDK private implementation or global HTTP alias is modified.

The existing Anthropic adapter drops returned thinking signatures and does not replay thinking blocks. This migration does not claim to fix that behavior. Vendor fixtures exercise actual SDK streaming, usage and close behavior; they are distinct from a live external-model credential check.

The [official SDK migration note](https://github.com/anthropics/anthropic-sdk-python/blob/main/MIGRATION.md) documents the public httpx2 boundary and the process-wide effects of its alias helper. The alternative is an explicit second public HTTP transport type backed by the same RPC bytes; it also needs the current adapter's request-codec change. Reevaluate that small upgrade at each controller dependency release, run both real SDK fixtures and the provider suite, measure the locked environment, and review any behavior or budget change. The selected pin avoids changing every CLI deployment's minimum dependencies merely to supply this component.

## Component and environment boundary

`scripts/build_controller.py` follows internal imports and package initializers, rejects old HTTP server, CLI, channels, launch, legacy surfaces and scientific package imports, then produces a deterministic source ZIP with file hashes. Skill Markdown supplies the catalogue. Algorithms, domain libraries and scientific SDK source belong in a separate science payload. The complete OmicsClaw wheel and its console scripts are not inputs to the controller package.

Base has the model SDKs and YAML parser. Notebook adds the Jupyter client and its transport dependencies. Existing MCP uses the project's stdlib transports and adds no MCP SDK dependency. Neither profile installs ipykernel, NumPy, pandas, AnnData or Scanpy into the control environment. The science interpreter supplies kernels and algorithms.

The first measured Linux x86_64 profiles used an independent CPython 3.11.17 distribution. Dependency locks include hashes. Frozen limits are in `omicsclaw/entry/ssh/profiles/budgets.json`; growth must be investigated rather than changing the limits automatically. Cache, interpreter, code and environments must be reported separately, including logical file sizes so hard links do not hide profile costs. Other architectures require separate validation.

## Lifecycle and authority

Stage 1 selects EOF shutdown. A kernel-held workspace file lock prevents another controller writer and releases on process death. No daemon, attach broker, systemd or tmux process is required. Existing session, approval and Notebook ownership logic remains in charge of those resources. The controller lock adds workspace ownership; it does not replace their state machines.

EOF closes model capabilities, stops admission, cancels observations and drains shared state. Reconnection starts a new process and inspects receipts and durable events before any new turn. In-flight model output, approval or uncertain tool work is not replayed automatically. Background science jobs, persistent kernel rehydration, leases and process-identity-aware cleanup require later stages.

Mac SSH profiles, model settings, credentials, local drafts and file permissions are Mac authority. Confirmed messages, turns, jobs, plans, usage and artifact metadata belong to the selected controller workspace. Science files and kernel memory belong to their compute target. Mac views will become rebuildable projections with stable source identity and cursor; the current UI cache has not yet been migrated.

Multi-target work will use a restricted Mac SSH transport broker injected into the existing remote executor seams. Target aliases and roots must match the user's configured capabilities. The controller does not receive Mac private keys or forwarded SSH agents. The first slice uses one controller and one workspace; this does not remove the existing multi-target migration requirement.

## Remaining stage boundaries

Desktop route transport migration, production component supply, all skill form and agent paths, persistent external jobs, Slurm, Notebook restart recovery, MCP credential mapping, data migration, signed release tests and deletion of old modes remain required. Stage 1 evidence establishes the shared-core and SSH seams. It is not a full Desktop acceptance result.

## Frozen scope, estimates and review decision

The named inventory is in `2026-10-14-ssh-controller-inventory.json`. It covers eleven work packages, every provider caller, currently fixed store roots, new schema ownership and full capability profiles. App `docs/ssh-controller-route-authority.json` records 88 actual route files with concrete request/query reads, response expressions and links to shared schemas. Dynamic or streaming responses retain their source links; this inventory establishes planned ownership and records pending migration. It is not a claim that these routes already run over SSH.

The planning intervals are 57–114 person-days for the same-core candidate and 98–196 for a TypeScript control migration with the same science, SSH, recovery and data scope. These are engineering estimates based on the named seams, not observed implementation times. They overlap. The candidate saves the control-language port and its continuing CLI/IM parity work, while both choices still need substantial transport and recovery testing. Exceeding a work package upper bound, adding a resident attach broker, duplicating a business state machine or widening a provider rewrite requires another scope review.

Measured Linux sizes use logical file bytes: independent Python 82.63 MiB, Base environment 25.03 MiB and the representative Notebook client environment 26.21 MiB. The old same-function full-wheel/HTTP environment measured 47.11 MiB on the same Python. Its idle process used 55.75 MiB RSS and started in 1.57 s; the representative Notebook control process used 50.56 MiB and started/executed a cell in 1.29 s. Those different workloads do not prove a universal latency improvement. The Mac/OpenSSH SDK fixture exchange previously peaked at 67.54 MiB control RSS.

The existing selected science environment measured 18,602,924,558 logical bytes. It is user compute infrastructure reused for a representative worker probe, not a fresh minimal science deployment. Installed control, interpreter, component archive/extracted source, bounded cache, scientific environments, extension processes and growing user state/data must stay visible separately. Hard links do not reduce the reported logical environment cost.

Unimplemented full-profile estimates reserve 100 MiB for Python, at most 44 MiB for control dependencies and 4.2 MiB for source, within the existing 160 MiB installed full-control cap. Full RSS has a 155 MiB estimated peak under the 160 MiB cap for the defined bounded control workload; this is not measured. The first complete supplier must stay within 120 MiB download and 80 MiB cache, giving a 240 MiB full installed-control-plus-cache cap. Notebook, MCP/attachments, background science jobs and all enabled capabilities must be measured at their Stage3/4 gates, and the combined promised control profile before release. Unbounded data/results belong to declared state/science locations rather than that installation budget.

The candidate is technically viable for independent Stage1 review: shared business assembly/state machines remain single definitions, public SDK injection works over real OpenSSH, and persistent receipts fence duplicate admission. Production provider settings, named caller/turn capabilities, supply/update, complete route adapters, durable kernels/jobs, all science payloads and legacy-mode deletion remain required. The Stage1 recommendation is conditional on the independent code review and exact verification results; it does not authorize a release or claim final acceptance.
