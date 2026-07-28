"""Regression tests for build-backend-runtime.py's health-contract gate.

The builder's phase-2 probe starts the desktop server and refuses to ship a
runtime whose /health payload does not match the contract it was built for.
These cover that validator; the App-release pairing tests that used to live
here asserted on OmicsClaw-App's build.yml and did not come across with the
script.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts" / "build-backend-runtime.py"




def _load_build_script():
    spec = importlib.util.spec_from_file_location(
        "omicsclaw_app_build_backend_runtime", BUILD_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load build-backend-runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILD_RUNTIME = _load_build_script()


def _health_payload(**desktop_chat_overrides: object) -> str:
    desktop_chat: dict[str, object] = {
        "request_schema_version": 1,
        "sse_schema_version": 1,
        "interrupt_schema_version": 1,
        "authoritative_ingress": False,
        "durable_ingress_idempotency": False,
    }
    desktop_chat.update(desktop_chat_overrides)
    return json.dumps(
        {
            "status": "ok",
            "contracts": {"desktop_chat": desktop_chat},
        }
    )


class DesktopChatHealthContractTests(unittest.TestCase):
    def test_accepts_every_shape_the_app_itself_accepts(self) -> None:
        """The gate must not be stricter than `src/lib/backend-health.ts`.

        That validator requires integer schema versions and boolean ingress
        flags, then passes the values through as capability flags. Pinning them
        to one stage is what broke the npm release: the backend advanced to
        authoritative + durable ingress and the builder rejected its own
        runtime.
        """

        accepted = (
            _health_payload(),  # the old non-authoritative stage
            _health_payload(  # what desktop_chat_contract() reports today
                authoritative_ingress=True, durable_ingress_idempotency=True
            ),
            _health_payload(request_schema_version=2, sse_schema_version=3),
        )
        for payload in accepted:
            with self.subTest(payload=payload):
                BUILD_RUNTIME._validate_desktop_chat_health_contract(payload)

    def test_rejects_unusable_contract_shapes(self) -> None:
        invalid_payloads = (
            "not-json",
            json.dumps({"status": 7, "contracts": {}}),
            json.dumps({"status": "ok", "contracts": {}}),
            # bool is an int subclass — it must not pass as a schema version.
            _health_payload(request_schema_version=True),
            _health_payload(interrupt_schema_version=True),
            _health_payload(sse_schema_version=1.0),
            _health_payload(request_schema_version=0),
            _health_payload(authoritative_ingress="false"),
            _health_payload(durable_ingress_idempotency=None),
            # interrupt_schema_version and durable_ingress_idempotency absent.
            json.dumps(
                {
                    "status": "ok",
                    "contracts": {
                        "desktop_chat": {
                            "request_schema_version": 1,
                            "sse_schema_version": 1,
                            "authoritative_ingress": False,
                        }
                    },
                }
            ),
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                BUILD_RUNTIME._validate_desktop_chat_health_contract(payload)
