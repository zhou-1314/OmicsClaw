"""The host probe: one read-only script, one JSON object, best effort.

The knowledge-gathering half of C1. The plan's ruling (Claude Science's
probe, adapted): a host is characterised by running **one POSIX shell
script in a login shell** and parsing the JSON it prints between two
sentinel lines — not by installing anything, not by assuming Python
exists remotely, not by parsing human-formatted tool output a second
time.

**Read-only, and visibly so.** The script touches nothing: ``nproc``,
``free -m``, ``nvidia-smi -L``, ``command -v``, ``sinfo``, ``hostname``,
``uname``, and existence tests on scratch candidates. A host
administrator reading the base64 blob can decode it and find no writes
anywhere, which is the only audit story a "just probe it" feature can
offer.

**Everything is optional.** Every field is emitted best-effort: a
machine without ``nvidia-smi`` answers an empty GPU list, a machine
without SLURM answers no partitions, a shell without ``free`` answers
``null`` memory. :func:`parse_probe_output` carries the same tolerance —
a missing field, a truncated tail, a marker pair that never closes, or
JSON that will not parse all yield a :class:`HostProbe` rather than an
exception, with :attr:`HostProbe.parse_error` saying what was imperfect.
A probe that crashes on half the fleet is worse than no probe.

**The logic is local, the facts are remote.** Which scratch candidate to
prefer (:meth:`HostProbe.scratch_root`) is decided in Python where it
can be tested and changed; the script merely reports which candidates
are defined and which exist. The script is delivered base64-wrapped
(:func:`~omicsclaw.remote.ssh.encode_login_script`) so no quoting
question exists in either shell, and runs under ``bash -l`` so the
module/conda environment a user actually gets is the one measured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PROBE_START = "===OMICSCLAW_PROBE_START==="
PROBE_END = "===OMICSCLAW_PROBE_END==="
"""Sentinel lines the probe's JSON is wrapped in.

Delimiters rather than "the whole output is JSON" because a login shell
is a noisy place: profile scripts print motd fragments, conda
initialisation warns, and a parser that demanded byte-clean stdout would
fail on exactly the misconfigured hosts a probe exists to characterise.
"""

PROBE_SCRIPT = r"""# omicsclaw host probe - read-only; prints one JSON object between sentinels
umask 077
jstr() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\011\012\015'
}
num_or_null() {
  case "${1:-}" in
    ''|*[!0-9]*) printf 'null' ;;
    *) printf '%s' "$1" ;;
  esac
}
printf '%s\n' '===OMICSCLAW_PROBE_START==='
printf '{"hostname":"%s"' "$(jstr "$(hostname 2>/dev/null)")"
printf ',"platform":"%s"' "$(jstr "$(uname -s -m 2>/dev/null)")"
printf ',"home":"%s"' "$(jstr "${HOME:-}")"
NPROC="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)"
printf ',"nproc":%s' "$(num_or_null "$NPROC")"
MEM="$(free -m 2>/dev/null | awk '/^Mem:/{print $2; exit}')"
printf ',"memMb":%s' "$(num_or_null "$MEM")"
printf ',"gpus":['
FIRST=1
while IFS= read -r line; do
  [ -n "$line" ] || continue
  [ "$FIRST" -eq 1 ] || printf ','
  FIRST=0
  printf '"%s"' "$(jstr "$line")"
done <<EOF
$(nvidia-smi -L 2>/dev/null)
EOF
printf ']'
printf ',"commands":{'
SEP=""
for c in sbatch conda module uv sinfo; do
  P="$(command -v "$c" 2>/dev/null)"
  if [ -n "$P" ]; then
    printf '%s"%s":"%s"' "$SEP" "$c" "$(jstr "$P")"
  else
    printf '%s"%s":null' "$SEP" "$c"
  fi
  SEP=","
done
printf '}'
printf ',"slurm":{'
if command -v sinfo >/dev/null 2>&1; then
  printf '"partitions":['
  FIRST=1
  for p in $(sinfo -h -o '%P' 2>/dev/null | tr -d '*'); do
    [ -n "$p" ] || continue
    [ "$FIRST" -eq 1 ] || printf ','
    FIRST=0
    printf '"%s"' "$(jstr "$p")"
  done
  printf ']'
else
  printf '"partitions":null'
fi
printf '}'
printf ',"scratch":['
SEP=""
for d in "${SCRATCH:-}" "${WORK:-}" "/scratch/${USER:-}" "$HOME/scratch" "$HOME/.omicsclaw-scratch"; do
  [ -n "$d" ] || continue
  if [ -d "$d" ]; then
    printf '%s{"path":"%s","exists":true}' "$SEP" "$(jstr "$d")"
  else
    printf '%s{"path":"%s","exists":false}' "$SEP" "$(jstr "$d")"
  fi
  SEP=","
done
printf ']'
printf '}\n'
printf '%s\n' '===OMICSCLAW_PROBE_END==='
"""
"""The probe, as POSIX-ish bash. Rules for anyone editing it:

* **No writes.** Not one ``>``, ``tee`` or ``mkdir`` — the audit story
  in this module's docstring depends on it.
* **Never fail.** Every command is ``2>/dev/null``-guarded or wrapped in
  a ``command -v`` check, and a missing value prints ``null`` or an
  empty array.
* **No single-quote assumptions about delivery.** The script travels
  base64-encoded, but keep it free of heredocs-in-heredocs and similar
  nesting surprises; ``tests/remote/test_probe.py`` pins that the
  encoded form round-trips.
"""

SCRATCH_CANDIDATE_ORDER = ("SCRATCH", "WORK", "/scratch/$USER", "~/scratch", "~/.omicsclaw-scratch")
"""The plan's candidate list, in preference order — mirrored here so
:meth:`HostProbe.scratch_root` and the script's emission order are
checkable against one source of truth (a test asserts they agree)."""


@dataclass(slots=True)
class HostProbe:
    """What one probe learned, every field optional.

    ``raw`` keeps the decoded JSON and ``parse_error`` the complaint when
    decoding was imperfect, so a caller can show a resource card that is
    honest about what it does not know rather than a card that silently
    omits the reason.
    """

    hostname: str = ""
    platform: str = ""
    home: str = ""
    """The remote ``$HOME``, absolute — the fallback scratch needs it,
    because a literal ``~`` handed to ``mkdir``/``sftp`` means three
    different things to three different programs and none of them
    agree."""
    nproc: int | None = None
    mem_mb: int | None = None
    gpus: list[str] = field(default_factory=list)
    commands: dict[str, str | None] = field(default_factory=dict)
    slurm_partitions: list[str] = field(default_factory=list)
    scratch_candidates: list[tuple[str, bool]] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    parse_error: str = ""

    def scratch_root(self) -> str:
        """The directory job work directories go under.

        First candidate that both is defined and exists, else the remote
        home (absolute — see :attr:`home`), else ``~`` for a host whose
        probe said nothing at all. Local logic on remote facts, per this
        module's docstring.
        """
        for path, exists in self.scratch_candidates:
            if exists:
                return path
        return self.home or "~"

    def as_json(self) -> dict:
        """The knowledge-base form: ``raw`` when it parsed, else a rebuild."""
        if self.raw and not self.parse_error:
            return dict(self.raw)
        return {
            "hostname": self.hostname,
            "platform": self.platform,
            "home": self.home,
            "nproc": self.nproc,
            "memMb": self.mem_mb,
            "gpus": list(self.gpus),
            "commands": dict(self.commands),
            "slurm": {"partitions": list(self.slurm_partitions) or None},
            "scratch": [
                {"path": path, "exists": exists}
                for path, exists in self.scratch_candidates
            ],
        }


def parse_probe_output(text: str) -> HostProbe:
    """Extract and decode one probe answer from noisy shell output.

    The rules, in order: take the **last** ``START`` marker (a login
    shell that sourced something printing our own sentinels twice is
    broken, but the last run is the one that finished), then everything
    up to the first ``END`` after it — or to the end of the text when no
    ``END`` arrived (a truncated tail still has most of the object).
    Within that window, the first ``{`` to the last ``}`` is offered to
    ``json.loads``; failure is recorded on the probe, not raised.
    """
    start = text.rfind(PROBE_START)
    window = text[start + len(PROBE_START):] if start != -1 else text
    end = window.find(PROBE_END)
    if end != -1:
        window = window[:end]
    opening = window.find("{")
    closing = window.rfind("}")
    if opening == -1 or closing == -1 or closing < opening:
        return HostProbe(parse_error="no JSON object found between markers")
    fragment = window[opening:closing + 1]
    try:
        decoded = json.loads(fragment)
    except ValueError as exc:
        return HostProbe(parse_error=f"probe JSON did not parse: {exc}")
    if not isinstance(decoded, dict):
        return HostProbe(parse_error="probe JSON was not an object")
    return _from_decoded(decoded, parse_error="" if end != -1 else "probe END marker missing")


def _from_decoded(decoded: dict, *, parse_error: str = "") -> HostProbe:
    """Build a :class:`HostProbe` from decoded JSON, tolerating absence.

    A field of the wrong type is dropped rather than coerced: a probe
    that answered ``"nproc": "lots"`` is reporting a broken remote
    environment, and inventing a number would hide it.
    """
    def _int(key: str) -> int | None:
        value = decoded.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    commands_raw = decoded.get("commands")
    commands = (
        {
            str(name): (str(path) if isinstance(path, str) else None)
            for name, path in commands_raw.items()
        }
        if isinstance(commands_raw, dict)
        else {}
    )
    slurm_raw = decoded.get("slurm")
    partitions = (
        [str(p) for p in slurm_raw.get("partitions")]
        if isinstance(slurm_raw, dict) and isinstance(slurm_raw.get("partitions"), list)
        else []
    )
    scratch_raw = decoded.get("scratch")
    candidates: list[tuple[str, bool]] = []
    if isinstance(scratch_raw, list):
        for item in scratch_raw:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                candidates.append((item["path"], bool(item.get("exists"))))
    return HostProbe(
        hostname=str(decoded.get("hostname") or ""),
        platform=str(decoded.get("platform") or ""),
        home=str(decoded.get("home") or ""),
        nproc=_int("nproc"),
        mem_mb=_int("memMb"),
        gpus=[str(g) for g in decoded.get("gpus") or []],
        commands=commands,
        slurm_partitions=partitions,
        scratch_candidates=candidates,
        raw=decoded,
        parse_error=parse_error,
    )


__all__ = [
    "HostProbe",
    "PROBE_END",
    "PROBE_SCRIPT",
    "PROBE_START",
    "SCRATCH_CANDIDATE_ORDER",
    "parse_probe_output",
]
