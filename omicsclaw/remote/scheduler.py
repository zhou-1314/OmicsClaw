"""Slurm observations bind cluster, owner, submit time and an admission nonce."""

from __future__ import annotations

import re

from omicsclaw.tools.builtin.remote import StatusReport

from .jobs import parse_status
from .ssh import shell_quote

MAX_SCHEDULER_BYTES = 64 * 1024


def probe_command(job_id: str, cluster: str = "") -> str:
    if (
        not re.fullmatch(r"[1-9][0-9]{0,19}", job_id)
        or cluster
        and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", cluster)
    ):
        raise ValueError("invalid_scheduler_query")
    selected = " --clusters=" + shell_quote(cluster) if cluster else ""
    return (
        'printf \'UID=%s\\nUSER=%s\\n\' "$(id -u)" "$(id -un)"; '
        "printf 'CLUSTER=%s\\n' \"$(scontrol show config 2>/dev/null | awk '$1 == \"ClusterName\" {print $3; exit}')\"; "
        "printf 'LIVE\\n'; "
        f"scontrol{selected} --oneliner show job {job_id} 2>/dev/null | head -c {MAX_SCHEDULER_BYTES + 1}; "
        "printf '\\nACCOUNTING\\n'; "
        f"sacct{selected} --duplicates --noheader --allocations --parsable2 --jobs={job_id} "
        "--format=JobIDRaw,Cluster,User,UID,Submit,JobName%80,State,ExitCode 2>/dev/null "
        f"| head -c {MAX_SCHEDULER_BYTES + 1}"
    )


def parse_observation(output: str, *, job_id: str, nonce: str, expected=None):
    if len(output.encode()) > 2 * MAX_SCHEDULER_BYTES + 1024:
        return None, StatusReport(detail="scheduler_observation_budget")
    try:
        header, remainder = output.split("\nLIVE\n", 1)
        live, accounting = remainder.split("\nACCOUNTING\n", 1)
        if (
            len(live.encode()) > MAX_SCHEDULER_BYTES
            or len(accounting.encode()) > MAX_SCHEDULER_BYTES
        ):
            return None, StatusReport(detail="scheduler_observation_truncated")
        facts = dict(line.split("=", 1) for line in header.splitlines())
        uid = int(facts["UID"])
        user, cluster = facts["USER"], facts["CLUSTER"]
    except (KeyError, ValueError):
        return None, StatusReport(detail="scheduler_identity_unconfirmed")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", user)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", cluster)
        or uid < 0
    ):
        return None, StatusReport(detail="scheduler_identity_unconfirmed")
    tag = "omicsclaw_" + nonce
    candidates = []
    for line in live.splitlines():
        fields = dict(token.split("=", 1) for token in line.split() if "=" in token)
        if (
            fields.get("JobId") != job_id
            or fields.get("JobName") != tag
            or fields.get("UserId") != f"{user}({uid})"
        ):
            continue
        candidates.append(
            (
                {
                    "uid": uid,
                    "user": user,
                    "cluster": cluster,
                    "job_id": job_id,
                    "submit_time": fields.get("SubmitTime", ""),
                    "nonce": nonce,
                },
                fields.get("JobState", ""),
                None,
            )
        )
    live_proof = candidates[0][0] if len(candidates) == 1 else None
    if (
        not candidates
        or len(candidates) == 1
        and parse_status("slurm", candidates[0][1])[0] in {"done", "failed", "canceled"}
    ):
        candidates = []
        for line in accounting.splitlines():
            values = line.split("|")
            if len(values) != 8:
                continue
            observed_id, site, owner, owner_uid, submitted, name, state, code = values
            if (observed_id, site, owner, owner_uid, name) != (
                job_id,
                cluster,
                user,
                str(uid),
                tag,
            ):
                continue
            if live_proof is not None and submitted != live_proof["submit_time"]:
                continue
            candidates.append(
                (
                    {
                        "uid": uid,
                        "user": user,
                        "cluster": cluster,
                        "job_id": job_id,
                        "submit_time": submitted,
                        "nonce": nonce,
                    },
                    state,
                    code,
                )
            )
    candidates = [
        value for value in candidates if expected is None or value[0] == dict(expected)
    ]
    if len(candidates) != 1:
        return None, StatusReport(
            detail="scheduler_identity_ambiguous"
            if len(candidates) > 1
            else "scheduler_identity_unconfirmed"
        )
    proof, state_text, raw_exit = candidates[0]
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}", proof["submit_time"]
    ):
        return None, StatusReport(detail="scheduler_submit_time_unconfirmed")
    state, code = parse_status("slurm", state_text)
    if state in {"done", "failed", "canceled"}:
        if raw_exit is None or not re.fullmatch(r"[0-9]+:[0-9]+", raw_exit):
            return proof, StatusReport(detail="scheduler_accounting_pending")
        exit_status, signal = map(int, raw_exit.split(":"))
        code = 128 + signal if signal else exit_status
        if state == "done" and code:
            state = "failed"
    return proof, StatusReport(
        state=state, exit_code=code, identity_verified=state != "unknown"
    )


def cancel_command(proof) -> str:
    tag = "omicsclaw_" + proof["nonce"]
    cluster, job_id = shell_quote(proof["cluster"]), proof["job_id"]
    conditions = [
        f'[ "$(id -u)" = {proof["uid"]} ]',
        f'[ "$(id -un)" = {shell_quote(proof["user"])} ]',
        '[ "$(scontrol show config 2>/dev/null | awk \'$1 == "ClusterName" {print $3; exit}\')" = '
        + cluster
        + " ]",
        f"_live=$(scontrol --clusters={cluster} --oneliner show job {job_id} 2>/dev/null | head -c {MAX_SCHEDULER_BYTES})",
    ]
    for key, value in {
        "JobId": job_id,
        "JobName": tag,
        "UserId": f"{proof['user']}({proof['uid']})",
        "SubmitTime": proof["submit_time"],
    }.items():
        pattern = r"(^|[[:space:]])" + re.escape(key + "=" + value) + r"([[:space:]]|$)"
        conditions.append("printf '%s' \"$_live\" | grep -Eq " + shell_quote(pattern))
    return (
        " && ".join(conditions)
        + f" && scancel --clusters={cluster} --user={shell_quote(proof['user'])} --name={shell_quote(tag)} {job_id}"
    )
