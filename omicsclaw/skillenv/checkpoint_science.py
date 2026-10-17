"""Fixed group inspection and stage descriptors, loaded into private kernel scopes."""

import hashlib
import json
import os
import runpy
from contextlib import contextmanager
from pathlib import Path

STAGES = frozenset(
    {
        "qc",
        "normalization",
        "integration",
        "clustering",
        "annotation",
        "differential_expression",
    }
)


def runtime_evidence(directory):
    import sys

    packages = {}
    for name in ("anndata", "pandas", "numpy", "scipy", "h5py"):
        module = sys.modules.get(name)
        packages[name] = (
            None
            if module is None
            else {
                "version": getattr(module, "__version__", None),
                "path": getattr(module, "__file__", None),
            }
        )
    evidence = {
        "schemaVersion": 1,
        "language": "python",
        "runtimeVersion": sys.version,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "libraryPaths": [],
        "packages": packages,
    }
    serializer = runpy.run_path(str(Path(__file__).with_name("notebook_checkpoint.py")))
    serializer["atomic_json"](Path(directory) / "runtime.json", evidence)


def verify_group(files, directory):
    serializer = runpy.run_path(str(Path(__file__).with_name("notebook_checkpoint.py")))
    for item in files:
        path = Path(directory) / item["file"]

        def check(path=path, item=item):
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as stream:
                while block := stream.read(65536):
                    digest.update(block)
                    size += len(block)
            if size != item["bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError("checkpoint_file_changed")

        check()
        value = serializer["verify"](path, item["format"], item["shape"])
        del value
        check()


def inspect_group(namespace, requests, directory, maximum):
    root = Path(__file__).parent
    codec = runpy.run_path(str(root / "notebook_identity.py"))
    inspector = runpy.run_path(str(root / "notebook_inspect.py"))
    remaining = maximum

    class Writer:
        def __init__(self, stream):
            self.stream = stream

        def write(self, data):
            nonlocal remaining
            if len(data) > remaining:
                raise ValueError("checkpoint_byte_limit")
            remaining -= len(data)
            result = self.stream.write(data)
            if result != len(data):
                raise OSError("checkpoint_short_write")
            return result

    @contextmanager
    def writing(path):
        with path.open("xb") as stream:
            yield Writer(stream)
            stream.flush()
            os.fsync(stream.fileno())

    # This runpy namespace is private to the fixed inspection, not user code.
    inspector["inspect_object"].__globals__["_writing"] = writing
    for item in requests:
        destination = Path(directory) / item["logicalKey"]
        destination.mkdir(mode=0o700)
        inspector["inspect_object"](namespace, item["request"], destination, codec)


def stage_writer(directory):
    directory = Path(directory)

    def report(stage, metadata=None):
        if stage not in STAGES:
            raise ValueError("invalid_checkpoint_stage")

        def validate(value, depth=0):
            if depth > 8:
                raise ValueError("checkpoint_stage_metadata_limit")
            if value is None or type(value) in (str, bool, int, float):
                return
            if type(value) in (list, tuple) and len(value) <= 100:
                for item in value:
                    validate(item, depth + 1)
                return
            if (
                type(value) is dict
                and len(value) <= 100
                and all(type(key) is str for key in value)
            ):
                for item in value.values():
                    validate(item, depth + 1)
                return
            raise ValueError("invalid_checkpoint_stage_metadata")

        validate(metadata)
        data = json.dumps(
            {"schemaVersion": 1, "stage": stage, "ordinal": 1, "metadata": metadata},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if len(data) > 4096:
            raise ValueError("checkpoint_stage_metadata_limit")
        path = directory / "stage.json"
        if path.exists():
            (directory / "duplicate").touch(exist_ok=True)
            raise ValueError("one_stage_per_execution")
        temporary = directory / "stage.partial"
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    return report
