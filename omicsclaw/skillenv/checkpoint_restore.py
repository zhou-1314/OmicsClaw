"""Fixed readers for an owner-authenticated checkpoint and a fresh target."""

import hashlib
import importlib
import runpy
import sys
from pathlib import Path


def prepare(namespace, request, directory):
    root, directory = Path(__file__).parent, Path(directory)
    science = runpy.run_path(str(root / "checkpoint_science.py"))
    serializer = runpy.run_path(str(root / "notebook_checkpoint.py"))
    source = request["runtimeEvidence"]
    for name in ("anndata", "pandas", "numpy", "scipy", "h5py"):
        if source["packages"].get(name) is not None:
            importlib.import_module(name)
    science["runtime_evidence"](directory)
    if source["runtimeVersion"].split()[0].split(".")[:2] != list(
        map(str, sys.version_info[:2])
    ):
        raise ValueError("restore_runtime_incompatible")
    for name, evidence in source["packages"].items():
        if evidence is not None and (
            not evidence.get("version")
            or getattr(sys.modules.get(name), "__version__", None)
            != evidence["version"]
        ):
            raise ValueError("restore_dependency_incompatible")
    token = request["token"]
    if token in namespace:
        raise ValueError("restore_target_consumed")
    values = {}
    for item in request["files"]:
        symbol = item["symbol"]
        if symbol in namespace or symbol in values:
            raise ValueError("restore_symbol_conflict")
        path = Path(item["path"])

        def check(path=path, item=item):
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
                    size += len(chunk)
            if size != item["bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError("restore_source_changed")

        check()
        values[symbol] = serializer["verify"](path, item["format"], item["shape"])
        check()
    namespace[token] = values
    science["inspect_group"](
        values, request["inspection"], directory, request["maxStagingBytes"]
    )


def commit(namespace, token, directory):
    values = namespace.get(token)
    if (
        type(values) is not dict
        or not values
        or any(name in namespace for name in values)
    ):
        raise ValueError("restore_target_changed")
    namespace.update(values)
    del namespace[token]
    serializer = runpy.run_path(str(Path(__file__).with_name("notebook_checkpoint.py")))
    serializer["atomic_json"](Path(directory) / "committed.json", {"token": token})
