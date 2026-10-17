"""Wait for the host's durable launch evidence before starting a finite child."""

import json
import os
import sys
import time
from pathlib import Path


def main():
    request = json.loads(sys.argv[1])
    gate = Path(request["gate"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if gate.read_text() == request["token"]:
                gate.unlink()
                os.execv(request["argv"][0], request["argv"])
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise SystemExit("contained launch was not acknowledged")


if __name__ == "__main__":
    main()
