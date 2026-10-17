"""Bounded ordered IOPub output with inert text and small static tables."""

import json
from html.parser import HTMLParser


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def text(value, limit=2048):
    value = value if isinstance(value, str) else str(value)
    raw = value[: limit + 1].encode()
    return (
        value
        if len(value) <= limit and len(raw) <= limit
        else raw[: max(0, limit - 24)].decode(errors="ignore") + "\n[output truncated]"
    )


class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"th", "td"} and self.row is not None:
            self.cell = ""

    def handle_data(self, data):
        if self.cell is not None:
            self.cell = text(self.cell + data, 256)

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self.cell is not None and self.row is not None:
            if len(self.row) < 16:
                self.row.append(self.cell.strip())
            else:
                self.truncated = True
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if len(self.rows) < 21:
                self.rows.append(self.row)
            else:
                self.truncated = True
            self.row = None


def table(data):
    html = data.get("text/html")
    if (
        not isinstance(html, str)
        or len(html) > 65536
        or len(html.encode()) > 65536
        or "<table" not in html.lower()
    ):
        return None
    parser = _Table()
    parser.feed(html)
    if len(parser.rows) < 2:
        return None
    return {
        "type": "table",
        "columns": parser.rows[0],
        "rows": parser.rows[1:],
        "truncated": parser.truncated,
    }


class OrderedOutputs:
    def __init__(self, *, limit=24576, sink=None):
        self.limit, self.sink = limit, sink
        self.items, self.bytes = [], 2
        self.stopped = False

    def add(self, item):
        if self.stopped:
            return
        item = {"seq": len(self.items) + 1, **item}
        cost = len(encoded(item)) + 1
        if (
            cost > 8192
            or self.bytes + cost > self.limit - 256
            or len(self.items) >= 120
        ):
            if item["type"] == "table" and item["rows"]:
                item["rows"] = item["rows"][:-1]
                item["truncated"] = True
                return self.add({k: v for k, v in item.items() if k != "seq"})
            item = {
                "seq": len(self.items) + 1,
                "type": "truncated",
                "reason": "ordered_output_budget",
            }
            self.stopped = True
        self.items.append(item)
        self.bytes += len(encoded(item)) + 1
        if self.sink is not None:
            try:
                self.sink(dict(item))
            except Exception:  # noqa: BLE001, S110 - observers cannot fail scientific execution
                pass

    def display(self, data, *, image=None, display_id=None, update=False):
        if image is not None and image.artifact_id:
            item = {
                "type": "image",
                "artifactId": image.artifact_id,
                "mime": "image/png",
            }
        else:
            item = table(data)
            if item is None:
                plain = data.get("text/plain") or data.get("text/html")
                if plain is None:
                    return
                item = {"type": "text", "text": text(plain, 4096)}
        if display_id:
            display_id = text(display_id, 128)
            if update:
                item = {"type": "update", "displayId": display_id, "output": item}
            else:
                item["displayId"] = display_id
        self.add(item)
