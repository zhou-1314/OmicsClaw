"""Byte caps apply before rich output reaches persistence or a renderer."""

from omicsclaw.kernel.outputs import OrderedOutputs, encoded, table, text


def test_multibyte_limits_and_event_budget_keep_explicit_truncation():
    assert len(text("汉" * 10000, 100).encode()) <= 100
    sent = []
    outputs = OrderedOutputs(limit=2048, sink=sent.append)
    for _ in range(1000):
        outputs.add({"type": "stream", "name": "stdout", "text": "汉" * 100})
    assert outputs.items[-1]["type"] == "truncated"
    assert len(encoded(outputs.items)) <= 2048
    assert sent == outputs.items


def test_table_is_inert_and_bounded_before_parsing():
    assert table({"text/html": "<table>" + "a" * 65537}) is None
    parsed = table(
        {
            "text/html": "<table><tr><th>gene</th></tr><tr><td><script>alert(1)</script>A</td></tr></table>"
        }
    )
    assert parsed["rows"] == [["alert(1)A"]]
    assert all(isinstance(value, str) for row in parsed["rows"] for value in row)


def test_png_accepts_mime_wrapping_but_rejects_other_characters(tmp_path):
    from omicsclaw.kernel.session import SessionKernel

    png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWM8AAAAASUVORK5CYII='
    kernel = SessionKernel('mime', workspace=tmp_path)
    assert kernel._save_figure(png[:40] + '\r\n' + png[40:], 1) is not None
    assert kernel._save_figure(png[:40] + '!' + png[40:], 2) is None
    assert kernel._save_figure('not PNG', 2) is None
    assert kernel._save_figure('汉字', 2) is None
    assert len(list((tmp_path / 'figures').glob('*.png'))) == 1
