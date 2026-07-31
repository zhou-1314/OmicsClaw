# HF Mirror Benchmark Download Design

## Goal

Resume the interrupted OmicBench and BiomniBench-DA downloads through
`https://hf-mirror.com` without allowing the download script or its children to
use any configured HTTP, HTTPS, or SOCKS proxy.

## Scope

The change is limited to the ignored operational helper
`data/benchmarks/download_hf_gated.sh` and its companion benchmark notes. It
does not modify the caller's shell, Claude sessions, login files, or system
proxy configuration. It preserves the current download destinations so the
Hugging Face client can reuse completed files and resume the existing
`.incomplete` files.

OmicBench is already complete at the expected 432 repository files and about
9.99 GiB. BiomniBench-DA remains partial; the resumed run must finish its 766
repository files in the existing `data/benchmarks/biomnibench-da` directory.
The separately cloned `OmicOS-BiomniBench` comparison repository is not part of
the Hugging Face inventory and must remain untouched.

## Selected Approach

Keep the existing Hugging Face CLI-based downloader and harden its process
environment:

1. Unset lowercase and uppercase HTTP, HTTPS, ALL, and NO proxy variables at
   script startup. Child processes inherit the proxy-free environment.
2. Export `HF_ENDPOINT=https://hf-mirror.com` so API, metadata, and file
   resolution begin at the selected mirror.
3. Export `HF_HUB_DISABLE_XET=1`. The existing partial downloads use the
   classic HTTP path, and retaining that path avoids switching transfer
   backends mid-resume.
4. Increase the Hugging Face download timeout for large omics files while
   retaining bounded retries around each dataset download.
5. Run an authenticated mirror preflight before downloading. The preflight
   must prove that the locally stored Hugging Face credential can access both
   gated dataset repositories without printing the token.
6. Invoke `hf download` against the existing local directories. A completed
   OmicBench run should be an inexpensive no-op; BiomniBench-DA should resume
   the existing partial files.

This approach preserves the Hugging Face client's native metadata and range
resume behavior. A one-off environment prefix was rejected because it is easy
to omit on a later retry. A custom Python file downloader was rejected because
it would duplicate client behavior and risk invalidating compatible partial
state.

## Data Flow

The caller starts `download_hf_gated.sh`, optionally under `nohup`. The script
removes proxy variables from its own environment, sets the mirror and HTTP
backend settings, and performs authenticated metadata probes. It then handles
OmicBench followed by BiomniBench-DA. Each `hf download` reads local metadata,
skips matching completed files, and resumes matching partial files. Progress
and retry events are written to the caller-selected log.

The script does not alter or delete payloads, `.incomplete` files, Hugging Face
credentials, or unrelated benchmark data.

## Failure Handling

- Missing `hf`, missing credentials, failed gated access, or a mirror
  preflight failure stops the run before data transfer with an actionable
  message.
- Transient download failures retry a bounded number of times with a delay.
  Existing partial files remain available for the next attempt.
- Exhausting retries returns a nonzero exit status. Failure of OmicBench must
  not be reported as success, and failure of BiomniBench-DA must prevent the
  final completion marker.
- Interrupting the script must leave the Hugging Face partial files intact.

## Verification

Before starting the long transfer:

- inspect the script environment contract and run a proxy-free authenticated
  metadata probe through `hf-mirror.com` for both gated repositories;
- run a shell syntax check on the updated script;
- confirm the old proxy-backed downloader is no longer running.

During and after transfer:

- confirm the active process contains `HF_ENDPOINT=https://hf-mirror.com` and
  does not contain proxy variables;
- confirm the log identifies the mirror and shows native resume behavior;
- require successful `hf download` exit status for both repositories;
- verify the Hugging Face repository inventory is present locally and no
  current `.incomplete` files remain;
- report actual payload sizes, file counts, process status, and remaining disk
  space.
