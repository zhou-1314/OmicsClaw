"""Finite SSH process entry; it opens no product HTTP service."""

import argparse
import asyncio
from pathlib import Path

from .controller import run_stdio


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run_stdio(args.workspace))


if __name__ == "__main__":
    main()
