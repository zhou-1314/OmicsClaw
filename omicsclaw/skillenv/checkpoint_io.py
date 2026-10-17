"""Finite bounded sink for an R serialization pipe; no deployment configuration."""

import os
import sys
from pathlib import Path


def write(source, path, maximum):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".partial")
    total = 0
    with temporary.open("xb") as target:
        while block := source.read(min(65536, maximum - total + 1)):
            total += len(block)
            if total > maximum:
                raise ValueError("checkpoint_byte_limit")
            target.write(block)
        target.flush()
        os.fsync(target.fileno())
    os.link(temporary, path)
    temporary.unlink()


def main():
    if len(sys.argv) != 3 or not sys.argv[1].isdigit():
        return 2
    maximum, path = int(sys.argv[1]), Path(sys.argv[2])
    if not 1 <= maximum <= 512 * 1024 * 1024 or not path.is_absolute():
        return 2
    try:
        write(sys.stdin.buffer, path, maximum)
    except (OSError, ValueError):
        return 74
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
