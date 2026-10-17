"""Canonical Notebook identities shared by inspectors and bounded file readers."""

from __future__ import annotations

import hashlib
import json

MAX_IDS = 1_000_000
MAX_ID_BYTES = 32 * 1024 * 1024


def text(value, maximum=512):
    if type(value) is not str or "\0" in value:
        raise ValueError("invalid_identity")
    try:
        if len(value.encode("utf-8", errors="strict")) > maximum:
            raise ValueError("identity_limit")
    except UnicodeError:
        raise ValueError("invalid_identity") from None
    return value


def keys(values):
    if type(values) not in (list, tuple) or not 1 <= len(values) <= 4:
        raise ValueError("invalid_identity")
    result = [text(value, 128) for value in values]
    if len(set(result)) != len(result):
        raise ValueError("invalid_identity")
    return result


def tuple_bytes(values):
    if type(values) not in (list, tuple) or not 1 <= len(values) <= 4:
        raise ValueError("invalid_identity")
    quoted = []
    for value in values:
        pieces = ['"']
        for character in text(value):
            if character in {'"', "\\"}:
                pieces.append("\\" + character)
            elif ord(character) < 32:
                pieces.append(f"\\u{ord(character):04x}")
            else:
                pieces.append(character)
        pieces.append('"')
        quoted.append("".join(pieces))
    return ("[" + ",".join(quoted) + "]").encode("utf-8")


def header(identity_keys):
    return b'{"schemaVersion":1,"keys":' + tuple_bytes(keys(identity_keys)) + b"}\n"


def digest_rows(identity_keys, rows, *, ordered=True):
    """Small callers may sort here; large owner collections use SQLite byte ordering."""
    identity_keys = keys(identity_keys)
    lines, size = [], 0
    for row in rows:
        if len(row) != len(identity_keys):
            raise ValueError("invalid_identity")
        encoded = tuple_bytes(row)
        size += len(encoded) + 1
        if len(lines) >= MAX_IDS or size > MAX_ID_BYTES:
            raise ValueError("identity_limit")
        lines.append(encoded)
    if not ordered and len(set(lines)) != len(lines):
        raise ValueError("duplicate_identity")
    result = hashlib.sha256(
        (b"omicsclaw-axis-v1\n" if ordered else b"omicsclaw-selection-v1\n")
        + tuple_bytes(identity_keys)
        + b"\n"
    )
    for line in lines if ordered else sorted(lines):
        result.update(line + b"\n")
    return result.hexdigest()


def read_rows(stream, identity_keys):
    """Require canonical bytes before any R/jsonlite conversion can discard data."""
    identity_keys = keys(identity_keys)
    first = stream.readline(4097)
    if first != header(identity_keys):
        raise ValueError("invalid_identity")
    size, count = len(first), 0
    while raw := stream.readline(16385):
        size += len(raw)
        count += 1
        if size > MAX_ID_BYTES or count > MAX_IDS or len(raw) > 16384:
            raise ValueError("identity_limit")
        try:
            row = json.loads(raw.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError):
            raise ValueError("invalid_identity") from None
        if (
            not isinstance(row, list)
            or len(row) != len(identity_keys)
            or raw != tuple_bytes(row) + b"\n"
        ):
            raise ValueError("invalid_identity")
        yield row


def write_rows(stream, identity_keys, rows):
    identity_keys = keys(identity_keys)
    first = header(identity_keys)
    stream.write(first)
    count, size = 0, len(first)
    for row in rows:
        if type(row) not in (list, tuple) or len(row) != len(identity_keys):
            raise ValueError("invalid_identity")
        line = tuple_bytes(row) + b"\n"
        count, size = count + 1, size + len(line)
        if size > MAX_ID_BYTES or count > MAX_IDS:
            raise ValueError("identity_limit")
        stream.write(line)
    return count
