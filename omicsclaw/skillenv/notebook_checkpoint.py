"""Fixed scientific serializers; invoked only with an owner-created save intent."""

import json
import os
import pickle
import re
from collections import OrderedDict
from pathlib import Path

VERSION = 1


def scalar_tree(value, *, depth=0):
    if depth > 16:
        raise ValueError("unsupported_serializer")
    if value is None or type(value) in (str, bool, int, float, bytes):
        return
    if type(value) in (list, tuple):
        for item in value:
            scalar_tree(item, depth=depth + 1)
        return
    if type(value) in (dict, OrderedDict) and all(type(key) is str for key in value):
        for item in value.values():
            scalar_tree(item, depth=depth + 1)
        return
    raise ValueError("unsupported_serializer")


def safe_value(value, *, depth=0):
    """Reject user callbacks and unsupported references before a writer is opened."""
    import numpy as np
    import pandas as pd
    from scipy import sparse

    if depth > 16:
        raise ValueError("unsupported_serializer")
    if type(value) in (dict, OrderedDict) and all(type(key) is str for key in value):
        for item in value.values():
            safe_value(item, depth=depth + 1)
    elif type(value) in (list, tuple):
        for item in value:
            safe_value(item, depth=depth + 1)
    elif type(value) is pd.DataFrame:
        scalar_tree(value.attrs)
        for index in (value.index, value.columns):
            if not type(index).__module__.startswith("pandas.core.indexes."):
                raise ValueError("unsupported_serializer")
            for label in index:
                safe_value(label, depth=depth + 1)
            scalar_tree(list(index.names))
        for column in value.items():
            series = column[1]
            dtype = series.dtype
            if isinstance(dtype, pd.CategoricalDtype):
                for category in dtype.categories:
                    safe_value(category, depth=depth + 1)
            elif dtype == object:
                for item in series.array:
                    safe_value(item, depth=depth + 1)
            elif not (
                isinstance(dtype, np.dtype)
                or type(dtype).__module__.startswith("pandas.core.arrays.")
            ):
                raise ValueError("unsupported_serializer")
    elif type(value) in (np.ndarray, np.recarray):
        if value.dtype.hasobject:
            if value.dtype.names:
                for name in value.dtype.names:
                    safe_value(value[name], depth=depth + 1)
            else:
                for item in value.flat:
                    safe_value(item, depth=depth + 1)
    elif (
        type(value)
        in (
            sparse.csr_matrix,
            sparse.csc_matrix,
            sparse.coo_matrix,
            sparse.csr_array,
            sparse.csc_array,
            sparse.coo_array,
        )
        or isinstance(value, np.generic)
        and type(value).__module__ == "numpy"
    ):
        if value.dtype.hasobject:
            raise ValueError("unsupported_serializer")
    elif value is pd.NA or value is pd.NaT:
        return
    else:
        scalar_tree(value, depth=depth)


def supported(value, adapter):
    import pandas as pd

    if adapter == "python_table" and type(value) is pd.DataFrame:
        safe_value(value)
        return "pandas.DataFrame", "pickle5"
    if adapter == "anndata":
        import anndata

        if type(value) is not anndata.AnnData or value.isbacked or value.is_view:
            raise ValueError("unsupported_serializer")
        safe_value(value.X)
        for item in (value.obs, value.var, value.uns):
            safe_value(item)
        for mapping in (value.layers, value.obsm, value.varm, value.obsp, value.varp):
            for item in mapping.values():
                safe_value(item)
        if value.raw is not None:
            safe_value(value.raw.X)
            safe_value(value.raw.var)
            for item in value.raw.varm.values():
                safe_value(item)
        return "anndata.AnnData", "h5ad"
    raise ValueError("unsupported_serializer")


class BoundedFile:
    """Bound Python writes and retain the first error until publication is refused."""

    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.error = stream, maximum, None

    def write(self, data):
        size = memoryview(data).nbytes
        if self.error is not None:
            # A failed stream remains unpublished even if a writer ignores its error.
            return size
        try:
            if self.tell() + size > self.maximum:
                raise ValueError("checkpoint_byte_limit")
            written = self.stream.write(data)
            if written != size:
                raise OSError("checkpoint_short_write")
            return written
        except BaseException as exc:
            self.error = exc
            raise

    def truncate(self, size=None):
        if self.error is not None:
            return self.tell()
        try:
            size = self.tell() if size is None else size
            if size > self.maximum:
                raise ValueError("checkpoint_byte_limit")
            return self.stream.truncate(size)
        except BaseException as exc:
            self.error = exc
            raise

    def flush(self):
        if self.error is None:
            try:
                self.stream.flush()
            except BaseException as exc:
                self.error = exc
                raise

    def seek(self, *args):
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()

    def read(self, *args):
        return self.stream.read(*args)

    def readinto(self, *args):
        return self.stream.readinto(*args)


def native_cgroup_headroom():
    """Read the current memory hierarchy and every visible parent limit."""
    groups = [
        line.split(":", 2)
        for line in Path("/proc/self/cgroup").read_text().splitlines()
    ]
    available = float("inf")
    found = False
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, mount = left.split(), right.split()
        if mount[0] == "cgroup" and "memory" in mount[2].split(","):
            names, limit_name, used_name = (
                "memory",
                "memory.limit_in_bytes",
                "memory.usage_in_bytes",
            )
        elif mount[0] == "cgroup2":
            names, limit_name, used_name = "", "memory.max", "memory.current"
        else:
            continue
        memberships = [
            v[2] for v in groups if (names in v[1].split(",") if names else v[0] == "0")
        ]
        if not memberships:
            continue
        decode = lambda value: re.sub(
            r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value
        )
        root, point = Path(decode(fields[3])), Path(decode(fields[4]))
        current = Path(memberships[0])
        if current == Path("/"):
            current = point
        elif current.is_relative_to(root):
            current = point / current.relative_to(root)
        else:
            raise ValueError("checkpoint_memory_scope_unavailable")
        if not current.is_dir():
            raise ValueError("checkpoint_memory_scope_unavailable")
        found = True
        while current.is_relative_to(point):
            path = current / limit_name
            if path.is_file():
                bound = path.read_text().strip()
                if bound != "max":
                    available = min(
                        available,
                        max(0, int(bound) - int((current / used_name).read_text())),
                    )
            if current == point:
                break
            current = current.parent
    if any(v[0] == "0" or "memory" in v[1].split(",") for v in groups) and not found:
        raise ValueError("checkpoint_memory_scope_unavailable")
    return available


def native_memory_limit(maximum):
    """Reserve room for one COW child and cap its additional virtual allocation."""
    import resource

    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
    )
    rss = int(status["VmRSS"].split()[0]) * 1024
    virtual = int(status["VmSize"].split()[0]) * 1024
    extra = 64 * 1024 * 1024
    available = next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    available = min(available, native_cgroup_headroom())
    if rss * 2 + extra > maximum or rss + extra * 2 > available:
        raise ValueError("checkpoint_memory_limit")
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    ceiling = virtual + extra
    for bound in (soft, hard):
        if bound != resource.RLIM_INFINITY:
            ceiling = min(ceiling, bound)
    if ceiling < virtual + extra:
        raise ValueError("checkpoint_memory_limit")
    return extra


def native_birth(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19]


def write_h5ad(value, stream, maximum_memory=32 * 1024**3, timeout_seconds=120):
    """Run native HDF5 in a bounded child while retaining the resident object."""
    import sys

    if sys.platform != "linux" or not all(
        hasattr(os, name) for name in ("fork", "waitid", "WNOWAIT")
    ):
        raise ValueError("checkpoint_native_bounded_writer_unavailable")
    import ctypes
    import resource
    import signal
    import time

    import h5py
    from anndata.io import write_elem

    if int(Path("/proc/self/stat").read_text().split(" ", 1)[0]) != os.getpid():
        raise ValueError("checkpoint_native_bounded_writer_unavailable")
    extra = native_memory_limit(maximum_memory)
    descriptor = stream.stream.fileno()
    original = os.fstat(descriptor)
    native = ctypes.CDLL(None, use_errno=True)
    prctl = native.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    parent = os.getpid()
    child, birth, result = None, None, None
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        child = os.fork()
        if child == 0:
            try:
                if prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
                    os._exit(4)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                null = os.open(os.devnull, os.O_WRONLY)
                os.dup2(null, 1)
                os.dup2(null, 2)
                os.close(null)
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                resource.setrlimit(
                    resource.RLIMIT_FSIZE, (stream.maximum, stream.maximum)
                )
                status = dict(
                    line.split(":", 1)
                    for line in Path("/proc/self/status").read_text().splitlines()
                )
                ceiling = int(status["VmSize"].split()[0]) * 1024 + extra
                resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
                with h5py.File(
                    f"/proc/self/fd/{descriptor}", "w", driver="sec2"
                ) as handle:
                    write_elem(handle, "/", value)
                os._exit(0)
            except MemoryError:
                os._exit(3)
            except BaseException:  # noqa: BLE001 - never unwind the copied kernel stack
                os._exit(2)
        birth = native_birth(child)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        deadline = time.monotonic() + timeout_seconds
        while result is None:
            result = os.waitid(os.P_PID, child, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if result is None:
                if time.monotonic() >= deadline:
                    raise ValueError("checkpoint_writer_timeout")
                time.sleep(0.01)
    finally:
        if child:
            # Keep the child unreaped until cleanup, so its PID cannot be reused.
            while result is None:
                try:
                    result = os.waitid(
                        os.P_PID, child, os.WEXITED | os.WNOHANG | os.WNOWAIT
                    )
                    if result is None:
                        if birth is None:
                            birth = native_birth(child)
                        if native_birth(child) != birth:
                            raise RuntimeError("checkpoint_writer_identity_changed")
                        os.kill(child, signal.SIGKILL)
                        result = os.waitid(os.P_PID, child, os.WEXITED | os.WNOWAIT)
                except (KeyboardInterrupt, InterruptedError):
                    continue
            while True:
                try:
                    os.waitpid(child, 0)
                    break
                except (KeyboardInterrupt, InterruptedError):
                    continue
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    if result.si_code != os.CLD_EXITED or result.si_status:
        raise ValueError(
            "checkpoint_memory_limit"
            if result.si_status == 3
            else "checkpoint_native_write_failed"
        )
    final = os.fstat(descriptor)
    if (original.st_dev, original.st_ino) != (
        final.st_dev,
        final.st_ino,
    ) or final.st_size > stream.maximum:
        raise ValueError("checkpoint_byte_limit")


def atomic_json(path, value):
    data = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(data) > 32768:
        raise ValueError("checkpoint_manifest_limit")
    temporary = path.with_suffix(".partial")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def save(namespace, request, directory):
    """Write a complete fixed object group without mutating its resident bindings."""
    if (
        type(request) is not dict
        or not {"objects", "maxFileBytes", "maxBytes"}
        <= set(request)
        <= {"objects", "maxFileBytes", "maxBytes", "maxMemoryBytes", "timeoutSeconds"}
        or type(request["objects"]) is not list
        or not 1 <= len(request["objects"]) <= 32
    ):
        raise ValueError("invalid_checkpoint_request")
    for key in (
        "maxFileBytes",
        "maxBytes",
        *(["maxMemoryBytes"] if "maxMemoryBytes" in request else []),
    ):
        if type(request[key]) is not int or request[key] < 1:
            raise ValueError("invalid_checkpoint_request")
    if (
        type(request.get("timeoutSeconds", 120)) not in (int, float)
        or not 0 < request.get("timeoutSeconds", 120) <= 120
    ):
        raise ValueError("invalid_checkpoint_request")
    prepared, symbols, keys = [], set(), set()
    for entry in request["objects"]:
        if type(entry) is not dict or set(entry) != {"logicalKey", "symbol", "adapter"}:
            raise ValueError("invalid_checkpoint_request")
        key, symbol = entry["logicalKey"], entry["symbol"]
        if (
            type(key) is not str
            or not re.fullmatch("[a-zA-Z0-9_-]{1,64}", key)
            or type(symbol) is not str
            or not symbol.isidentifier()
            or key in keys
            or symbol in symbols
            or symbol not in namespace
        ):
            raise ValueError("invalid_checkpoint_binding")
        keys.add(key)
        symbols.add(symbol)
        value = namespace[symbol]
        kind, format = supported(value, entry["adapter"])
        prepared.append((entry, value, kind, format))
    directory = Path(directory)
    total, result = 0, []
    for entry, value, kind, format in prepared:
        path = directory / (
            entry["logicalKey"] + (".h5ad" if format == "h5ad" else ".pkl")
        )
        with path.open("xb"):
            pass
        maximum = min(request["maxFileBytes"], request["maxBytes"] - total)
        if maximum < 1:
            raise ValueError("checkpoint_byte_limit")
        with path.open("r+b", buffering=0) as stream:
            bounded = BoundedFile(stream, maximum)
            if format == "h5ad":
                write_h5ad(
                    value,
                    bounded,
                    request.get("maxMemoryBytes", 32 * 1024**3),
                    request.get("timeoutSeconds", 120),
                )
            else:
                pickle.dump(value, bounded, protocol=5)
            if bounded.error is not None:
                raise bounded.error
            bounded.flush()
            os.fsync(stream.fileno())
        size = path.stat().st_size
        if size > maximum:
            raise ValueError("checkpoint_byte_limit")
        total += size
        result.append(
            {
                **entry,
                "type": kind,
                "format": format,
                "file": path.name,
                "bytes": size,
                "shape": list(value.shape),
                "serializerVersion": VERSION,
            }
        )
    atomic_json(
        directory / "saved.json",
        {"schemaVersion": 1, "objects": result, "totalBytes": total},
    )


def verify(path, format, expected_shape):
    """Load an owner-owned, hash-checked checkpoint for structural verification."""
    if format == "h5ad":
        import anndata

        value = anndata.read_h5ad(path)
        supported(value, "anndata")
    elif format == "pickle5":
        with Path(path).open("rb") as stream:
            value = pickle.load(stream)
        supported(value, "python_table")
    else:
        raise ValueError("unsupported_serializer")
    if list(value.shape) != expected_shape:
        raise ValueError("checkpoint_shape_mismatch")
    return value
