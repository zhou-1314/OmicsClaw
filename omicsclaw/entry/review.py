"""Archive a reviewer reply against the project files it was asked to read.

Receipts detect stale inputs and changed text. They are workspace files,
not signatures or proof against a writer who can replace both files.
"""

import hashlib
import json
import re
import uuid
from pathlib import Path

from omicsclaw.tools._workspace import Workspace

from .project import MANIFEST_FILE, MODULE_DIR_PATTERN, REPORT_FILE, REVIEW_BRIEF_FILE


class ReviewArchive:
    def __init__(self, root: Path, module: str):
        match = re.fullmatch(MODULE_DIR_PATTERN, module)
        if match is None:
            raise ValueError("review_module must be NN_slug, for example 01_qc")
        self.workspace = Workspace(root)
        self.module = module
        self.results = f"results/{module}"
        self.report = REPORT_FILE.format(nn=match[1], slug=match[2])
        self.snapshot = self._snapshot()

    def _snapshot(self) -> dict:
        legacy_readme = self.workspace.resolve(f"analysis/{self.module}/README.md")
        manifest_path = self.workspace.resolve(f"{self.results}/{MANIFEST_FILE}")
        if legacy_readme.is_file() and manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            replay = manifest.get("replay") or {}
            if manifest.get("frozen") or replay.get("status") != "ok":
                raise ValueError("review_module needs a successful replay and an unfrozen module")
            paths = [
                f"analysis/{self.module}/README.md",
                f"{self.results}/{self.report}",
                f"{self.results}/{REVIEW_BRIEF_FILE}",
            ]
            hashes = replay.get("step_sha256")
            if not isinstance(hashes, dict) or not hashes:
                raise ValueError("replay has no step hashes")
            paths.extend(f"analysis/{self.module}/{name}" for name in hashes)
            files = {
                name: hashlib.sha256(self.workspace.resolve(name).read_bytes()).hexdigest()
                for name in paths
            }
            if any(files[f"analysis/{self.module}/{name}"] != sha for name, sha in hashes.items()):
                raise ValueError("step files changed since replay; replay before reviewing")
            return {"replay": replay, "files": files}

        # Notebook / Clean-Room Replay provenance path (C1-C3)
        receipt_candidates = [
            f"{self.results}/provenance/replay_receipt.json",
            "provenance/replay_receipt.json",
        ]
        receipt = None
        receipt_rel = ""
        for cand in receipt_candidates:
            cand_file = self.workspace.resolve(cand)
            if cand_file.is_file():
                receipt = json.loads(cand_file.read_text(encoding="utf-8"))
                receipt_rel = cand
                break
        if receipt is None and manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("frozen"):
                raise ValueError("review_module needs a successful replay and an unfrozen module")
            receipt = manifest.get("replay")
            receipt_rel = f"{self.results}/{MANIFEST_FILE}"

        if not isinstance(receipt, dict) or receipt.get("status") != "ok":
            raise ValueError("review_module needs a successful replay and an unfrozen module")
        if (
            receipt.get("changed_outputs")
            or receipt.get("missing_outputs")
            or receipt.get("orphan_outputs")
            or receipt.get("stale_inputs")
            or receipt.get("unregistered_reads")
        ):
            raise ValueError("replay receipt has stale inputs, changed outputs, or orphan outputs")

        tracked_paths: list[str] = [receipt_rel]
        for brief_cand in (f"{self.results}/{REVIEW_BRIEF_FILE}", "provenance/review_brief.md"):
            if self.workspace.resolve(brief_cand).is_file():
                tracked_paths.append(brief_cand)
                break
        else:
            raise ValueError("review_brief.md is missing; run export_and_replay first")

        for rep_cand in (
            f"{self.results}/{self.report}",
            str(receipt.get("report_path") or ""),
            "output/REPORT.md",
            "REPORT.md",
        ):
            if rep_cand and self.workspace.resolve(rep_cand).is_file():
                rep_file = self.workspace.resolve(rep_cand)
                rep_sha = hashlib.sha256(rep_file.read_bytes()).hexdigest()
                if receipt.get("report_sha256") and rep_sha != receipt["report_sha256"]:
                    raise ValueError(f"report modified since replay: {rep_cand}")
                try:
                    rep_txt = rep_file.read_text(encoding="utf-8")
                except OSError:
                    rep_txt = ""
                if "{{" in rep_txt and "artifact:" in rep_txt:
                    from omicsclaw.runs.verification import Verdict, verify_artifact_markers

                    m_audit = verify_artifact_markers(rep_file, workspace=self.workspace.root)
                    if m_audit.verdict == Verdict.FAIL:
                        raise ValueError(
                            f"report has invalid or unescaped artifact markers: {m_audit.details}"
                        )
                tracked_paths.append(rep_cand)
                break
        else:
            raise ValueError("module report is missing")

        script_rel = str(receipt.get("script") or "")
        if script_rel:
            script_file = self.workspace.resolve(script_rel)
            if not script_file.is_file():
                raise ValueError(f"replay script missing: {script_rel}")
            script_sha = hashlib.sha256(script_file.read_bytes()).hexdigest()
            if receipt.get("script_sha256") and script_sha != receipt["script_sha256"]:
                raise ValueError("replay script changed since replay; replay before reviewing")
            tracked_paths.append(script_rel)

        for item in [*(receipt.get("inputs") or []), *(receipt.get("verified_outputs") or [])]:
            if isinstance(item, dict) and item.get("path"):
                rel_p = str(item["path"])
                f_obj = self.workspace.resolve(rel_p)
                if not f_obj.is_file():
                    raise ValueError(f"tracked artifact missing since replay: {rel_p}")
                cur_sha = hashlib.sha256(f_obj.read_bytes()).hexdigest()
                if item.get("sha256") and cur_sha != item["sha256"]:
                    raise ValueError(f"tracked artifact changed since replay: {rel_p}")
                if rel_p not in tracked_paths:
                    tracked_paths.append(rel_p)

        for helper in receipt.get("skill_files") or []:
            if isinstance(helper, dict) and helper.get("path") and helper.get("sha256"):
                hp = Path(str(helper["path"]))
                if not hp.is_absolute():
                    hp = self.workspace.resolve(str(helper["path"]))
                if not hp.is_file():
                    raise ValueError(f"sidecar skill file missing since replay: {helper['path']}")
                h_sha = hashlib.sha256(hp.read_bytes()).hexdigest()
                if h_sha != helper["sha256"]:
                    raise ValueError(f"sidecar skill file changed since replay: {helper['path']}")

        from omicsclaw.runs.verification import _discover_workspace_outputs

        recorded_outputs = {
            str(out["path"])
            for out in (receipt.get("verified_outputs") or [])
            if isinstance(out, dict) and out.get("path")
        }
        if recorded_outputs:
            current_disk = set(_discover_workspace_outputs(self.workspace.root))
            new_orphans = sorted(current_disk - recorded_outputs)
            if new_orphans:
                raise ValueError(f"untracked orphan outputs created after replay: {new_orphans}")

        files = {
            name: hashlib.sha256(self.workspace.resolve(name).read_bytes()).hexdigest()
            for name in dict.fromkeys(tracked_paths)
        }
        return {"replay": receipt, "files": files}

    def save(self, text: str) -> str:
        if self._snapshot() != self.snapshot:
            raise ValueError("module changed during review; review the current replay again")
        if not text.splitlines() or text.splitlines()[0] not in {"VERDICT: APPROVE", "VERDICT: REVISE"}:
            raise ValueError("review must start with VERDICT: APPROVE or VERDICT: REVISE")
        relative = f"{self.results}/reviews/task-review-{uuid.uuid4().hex}.md"
        path = self.workspace.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = text.encode("utf-8")
        receipt = {
            "schema": 1,
            "module": self.module,
            "review_sha256": hashlib.sha256(content).hexdigest(),
            **self.snapshot,
        }
        with path.open("xb") as target:
            target.write(content)
        with path.with_suffix(".json").open("x", encoding="utf-8") as target:
            json.dump(receipt, target, ensure_ascii=False, indent=2)
        return relative
