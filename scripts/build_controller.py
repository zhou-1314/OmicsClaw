"""Build a source-only controller closure, independently of the product wheel."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path

FORBIDDEN = (
    "omicsclaw.entry.desktop.server",
    "omicsclaw.entry.cli",
    "omicsclaw.entry.channel",
    "omicsclaw.launch",
    "omicsclaw.surfaces",
    "omicsclaw.routing",
    "skills",
)


def source_closure(root: Path) -> dict[str, Path]:
    modules = {}
    for path in (root / "omicsclaw").rglob("*.py"):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    pending = [
        "omicsclaw.entry.ssh.__main__",
        "omicsclaw.entry.ssh.science_runner",
        "omicsclaw.entry.ssh.science_worker",
    ]
    included = {}

    def add(name):
        if name in modules:
            pending.append(name)
        # Package initializers execute before every submodule.
        parts = name.split(".")
        for count in range(1, len(parts)):
            prefix = ".".join(parts[:count])
            if prefix in modules:
                pending.append(prefix)

    while pending:
        name = pending.pop()
        if name in included:
            continue
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN):
            raise ValueError(f"controller closure reached forbidden module: {name}")
        path = modules[name]
        included[name] = path
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = importlib.util.resolve_name(
                        "." * node.level + module, package
                    )
                add(module)
                for alias in node.names:
                    add(module + "." + alias.name)
    return included


def build(root: Path, destination: Path, *, allow_dirty: bool = False):
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
        )
    )
    if dirty and not allow_dirty:
        raise ValueError(
            "controller supply requires a fixed clean commit; --allow-dirty is development only"
        )
    closure = source_closure(root)
    files = sorted(closure.values())
    # The catalogue is data. Algorithms and SDK code belong in science payloads.
    files.extend(sorted((root / "skills").rglob("SKILL.md")))
    files.extend([root / "OMICSCLAW.md", root / "LICENSE"])
    files.extend(sorted((root / "omicsclaw/entry/ssh/profiles").glob("*")))
    records = []
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination / "controller.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            records.append(
                {
                    "path": relative,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
            info = zipfile.ZipInfo(relative, date_time=(2026, 10, 13, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    budgets = json.loads(
        (root / "omicsclaw/entry/ssh/profiles/budgets.json").read_text()
    )
    source_bytes = sum(row["bytes"] for row in records)
    if (
        source_bytes > budgets["sourceBytes"]
        or (destination / "controller.zip").stat().st_size
        > budgets["sourceArchiveBytes"]
    ):
        raise ValueError("controller source exceeds frozen budget")
    manifest = {
        "componentVersion": next(
            ast.literal_eval(node.value)
            for node in ast.parse(
                (root / "omicsclaw/entry/ssh/controller.py").read_text()
            ).body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "COMPONENT_VERSION"
                for target in node.targets
            )
        ),
        "protocolVersion": 1,
        "sourceCommit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "sourceCommitVerified": not dirty,
        "sourceFilesSha256": hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "entry": "omicsclaw.entry.ssh",
        "files": records,
        "sourceBytes": source_bytes,
        "budgets": budgets,
        "archiveSha256": hashlib.sha256(
            (destination / "controller.zip").read_bytes()
        ).hexdigest(),
        "profiles": {
            "base": {
                "dependencies": [
                    "openai==2.33.0",
                    "anthropic==0.125.0",
                    "httpx==0.28.1",
                    "PyYAML==6.0.3",
                ]
            },
            "notebook": {"dependencies": ["jupyter-client==8.8.0"]},
            "mcp": {"dependencies": []},
        },
    }
    (destination / "controller-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Development artifacts only; manifest will not verify the commit",
    )
    args = parser.parse_args()
    manifest = build(
        Path(__file__).resolve().parents[1], args.output, allow_dirty=args.allow_dirty
    )
    print(
        json.dumps(
            {"files": len(manifest["files"]), "sourceBytes": manifest["sourceBytes"]}
        )
    )
