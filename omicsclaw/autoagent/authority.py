"""Per-trial Skill authority for AutoAgent execution and evidence consumers.

The AutoAgent child executes from either the live Backend repository or a
harness worktree.  Parent-side tracing, evaluation and hard gates must consume
the exact manifest/source revision selected from that tree; the process-global
Registry is a different authority and may legitimately describe different
files while a trial is running.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from omicsclaw.skill.execution_contract import primary_anndata_output_path
from omicsclaw.skill.registry import OmicsRegistry

_REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class TrialSkillAuthority:
    """Frozen identity and output contract for one executed trial."""

    requested_skill_name: str
    canonical_skill_id: str
    skill_version: str
    manifest_hash: str
    source_hash: str
    primary_anndata_path: str | None
    skills_root: str

    def __post_init__(self) -> None:
        if (
            not self.requested_skill_name.strip()
            or not self.canonical_skill_id.strip()
            or not self.skill_version.strip()
            or self.skill_version == "unknown"
            or not _REVISION_RE.fullmatch(self.manifest_hash)
            or not _REVISION_RE.fullmatch(self.source_hash)
            or not Path(self.skills_root).is_absolute()
        ):
            raise ValueError("invalid frozen trial authority")
        if self.primary_anndata_path is not None:
            _validate_primary_anndata_path(self.primary_anndata_path)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TrialSkillAuthority":
        try:
            return cls(
                requested_skill_name=str(value["requested_skill_name"]),
                canonical_skill_id=str(value["canonical_skill_id"]),
                skill_version=str(value["skill_version"]),
                manifest_hash=str(value["manifest_hash"]),
                source_hash=str(value["source_hash"]),
                primary_anndata_path=(
                    str(value["primary_anndata_path"])
                    if value.get("primary_anndata_path") is not None
                    else None
                ),
                skills_root=str(value["skills_root"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid frozen trial authority") from exc

    def matches_skill_name(self, skill_name: str) -> bool:
        """Return whether a consumer names this requested/canonical Skill."""

        return skill_name in {
            self.requested_skill_name,
            self.canonical_skill_id,
        }


def capture_trial_skill_authority(
    project_root: str | Path,
    requested_skill_name: str,
) -> TrialSkillAuthority:
    """Capture one read-stable authority from the tree the child will run.

    A private Registry instance is deliberate: selecting a harness worktree
    must never publish it as the process-global Backend Registry.
    """

    root = Path(project_root).expanduser().resolve(strict=True)
    skills_root = root / "skills"
    snapshot = OmicsRegistry().snapshot(skills_root)
    requested_info = snapshot.skills.get(requested_skill_name)
    if requested_info is None:
        raise ValueError(f"unknown trial Skill: {requested_skill_name!r}")

    canonical = str(requested_info.get("alias") or requested_skill_name).strip()
    canonical_info = snapshot.skills.get(canonical)
    if not canonical or canonical_info is None:
        raise ValueError("trial Skill alias does not resolve to a canonical Skill")
    if str(canonical_info.get("alias") or canonical) != canonical:
        raise ValueError("trial Skill canonical identity is inconsistent")

    revision = snapshot.skill_revision(canonical)
    manifest_hash = str(revision.get("manifest_hash") or "")
    source_hash = str(revision.get("source_hash") or "")
    version = str(revision.get("skill_version") or "").strip()
    if (
        str(revision.get("skill_id") or "") != canonical
        or not version
        or version == "unknown"
        or not _REVISION_RE.fullmatch(manifest_hash)
        or not _REVISION_RE.fullmatch(source_hash)
    ):
        raise ValueError("trial Skill has no complete manifest/source revision")

    saves_anndata = bool(canonical_info.get("saves_h5ad"))
    primary = (
        primary_anndata_output_path(canonical_info.get("output_contract") or {})
        if saves_anndata
        else None
    )
    if saves_anndata and primary is None:
        raise ValueError(
            "trial Skill declares AnnData output without one unambiguous primary path"
        )
    if primary is not None:
        _validate_primary_anndata_path(primary)

    return TrialSkillAuthority(
        requested_skill_name=requested_skill_name,
        canonical_skill_id=canonical,
        skill_version=version,
        manifest_hash=manifest_hash,
        source_hash=source_hash,
        primary_anndata_path=primary,
        skills_root=str(snapshot.loaded_dir),
    )


def verify_trial_skill_authority(
    project_root: str | Path,
    expected: TrialSkillAuthority,
) -> None:
    """Fail when the execution tree no longer matches the pre-spawn freeze."""

    observed = capture_trial_skill_authority(
        project_root,
        expected.requested_skill_name,
    )
    if observed != expected:
        raise RuntimeError("trial Skill authority changed during child execution")


def _validate_primary_anndata_path(value: str) -> None:
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or bool(windows_path.drive)
        or windows_path.is_absolute()
        or ".." in path.parts
        or path.suffix.casefold() != ".h5ad"
    ):
        raise ValueError("trial primary AnnData path is not safe and output-relative")
