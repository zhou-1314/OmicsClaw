"""Canonical identities match frozen bytes in Python and the selected real R."""

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omicsclaw.skillenv.notebook_identity import (
    digest_rows,
    read_rows,
    tuple_bytes,
    write_rows,
)

VECTORS = Path(__file__).parents[1] / "fixtures/notebook-analysis/tuple-vectors.json"
CASES = json.loads(VECTORS.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_python_identity_matches_frozen_bytes(case):
    assert digest_rows(case["keys"], case["rows"]) == case["orderedIdsSha256"]
    assert (
        digest_rows(case["keys"], case["rows"], ordered=False) == case["membersSha256"]
    )
    stream = io.BytesIO()
    assert write_rows(stream, case["keys"], case["rows"]) == len(case["rows"])
    raw = stream.getvalue()
    assert raw.hex() == case["idFileUtf8Hex"]
    assert hashlib.sha256(raw).hexdigest() == case["idFileSha256"]
    assert list(read_rows(io.BytesIO(raw), case["keys"])) == case["rows"]


def test_selected_r_emits_the_same_canonical_files(tmp_path):
    executable = Path(sys.executable).with_name("Rscript")
    if not executable.is_file():
        pytest.skip("Selected scientific Python has no adjacent Rscript")
    positive = tmp_path / "positive.json"
    positive.write_text(json.dumps(CASES, ensure_ascii=False))
    source = Path(__file__).parents[2] / "omicsclaw/skillenv/notebook_identity.R"
    script = """
args <- commandArgs(trailingOnly=TRUE)
sys.source(args[[1]], envir=globalenv())
cases <- jsonlite::fromJSON(args[[2]], simplifyVector=FALSE)
for (index in seq_along(cases)) {
  case <- cases[[index]]
  keys <- unlist(case$keys, use.names=FALSE)
  rows <- lapply(case$rows, function(row) unlist(row, use.names=FALSE))
  connection <- file(file.path(args[[3]], paste0(index, ".ids")), open="wb")
  identity_write(connection, keys, rows)
  close(connection)
  lines <- vapply(rows, identity_tuple, character(1), USE.NAMES=FALSE)
  hex <- vapply(lines, function(line) paste0(sprintf("%02x", as.integer(charToRaw(line))), collapse=""), character(1))
  for (kind in c("axis", "members")) {
    domain <- if (kind == "axis") "omicsclaw-axis-v1" else "omicsclaw-selection-v1"
    body <- if (kind == "axis") lines else lines[order(hex, method="radix")]
    raw <- charToRaw(paste0(domain, "\\n", identity_tuple(keys), "\\n", paste0(body, if (length(body)) "\\n" else "", collapse="")))
    connection <- file(file.path(args[[3]], paste0(index, ".", kind)), open="wb")
    writeBin(raw, connection)
    close(connection)
  }
}
"""
    completed = subprocess.run(
        [
            str(executable),
            "--vanilla",
            "-e",
            script,
            str(source),
            str(positive),
            str(tmp_path),
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    for index, case in enumerate(CASES, 1):
        raw = (tmp_path / f"{index}.ids").read_bytes()
        assert raw.hex() == case["idFileUtf8Hex"], case["name"]
        assert hashlib.sha256(raw).hexdigest() == case["idFileSha256"]
        for suffix, bytes_key, digest_key in (
            ("axis", "axisUtf8Hex", "orderedIdsSha256"),
            ("members", "membersUtf8Hex", "membersSha256"),
        ):
            raw = (tmp_path / f"{index}.{suffix}").read_bytes()
            assert raw.hex() == case[bytes_key], case["name"]
            assert hashlib.sha256(raw).hexdigest() == case[digest_key]


@pytest.mark.parametrize("value", ["a\0b", "\ud800", "a" * 513, 1, None])
def test_identity_values_are_not_silently_cleaned_or_coerced(value):
    with pytest.raises(ValueError):
        tuple_bytes([value])


@pytest.mark.parametrize(
    "record",
    [
        b'["a\\nb"]\n',
        b'["A\\u0000B"]\n',
        b'["\\ud800"]\n',
        b"[12]\n",
        b'["a"]',
        b'["a", "b"]\n',
    ],
)
def test_owned_file_requires_canonical_identity_before_r_conversion(record):
    with pytest.raises(ValueError):
        list(
            read_rows(
                io.BytesIO(b'{"schemaVersion":1,"keys":["id"]}\n' + record), ["id"]
            )
        )


def test_set_digest_rejects_duplicate_identity_and_preserves_composite_members():
    with pytest.raises(ValueError, match="duplicate"):
        digest_rows(["id"], [["AA"], ["AA"]], ordered=False)
    assert digest_rows(
        ["sample", "barcode"], [["S1", "AA"], ["S2", "AA"]], ordered=False
    )


@pytest.mark.parametrize(
    "case",
    json.loads(VECTORS.read_text())["negativeCases"],
    ids=lambda case: case["name"],
)
def test_frozen_nul_vectors_are_rejected_before_any_r_json_conversion(case):
    with pytest.raises(ValueError, match="invalid_identity"):
        write_rows(io.BytesIO(), case["keys"], case["rows"])
    raw = (
        json.dumps(
            {"schemaVersion": 1, "keys": case["keys"]}, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    raw += b"".join(
        json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in case["rows"]
    )
    with pytest.raises(ValueError, match="invalid_identity"):
        list(read_rows(io.BytesIO(raw), case["keys"]))
