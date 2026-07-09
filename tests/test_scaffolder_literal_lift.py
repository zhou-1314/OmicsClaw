"""Unit tests for the P2a literal-lift pass (acquisition-plan.md §P2).

``lift_oc_run_literals`` rewrites only the literal-valued keyword arguments of
``oc.run(...)`` calls into CLI-flag references, so a promoted skill isn't
frozen to whatever thresholds the original mini-agent run happened to use.
These tests exercise the pure function directly (no scaffolding/gate
involved) — see ``tests/test_skill_scaffolder.py`` for the end-to-end
integration + fallback coverage.
"""

from __future__ import annotations

from omicsclaw.skill.scaffolder import LiftedParam, lift_oc_run_literals


def test_lifts_a_single_literal_kwarg():
    code = "res = oc.run('sc-preprocessing', adata, min_genes=200)\nadata = res.adata"
    result = lift_oc_run_literals(code)

    assert result.skipped == []
    assert result.lifted == [
        LiftedParam(
            name="min_genes",
            flag="--min-genes",
            default=200,
            type="int",
            help="Override min_genes for oc.run('sc-preprocessing', ...) (call #1)",
            call_index=1,
        )
    ]
    assert "min_genes=args.min_genes" in result.code
    assert "min_genes=200" not in result.code


def test_shared_kwarg_name_and_identical_value_collapses_to_one_flag():
    code = "a = oc.run('x', adata, resolution=1.0)\nb = oc.run('y', adata, resolution=1.0)"
    result = lift_oc_run_literals(code)

    assert len(result.lifted) == 1
    assert result.lifted[0].flag == "--resolution"
    assert result.code.count("args.resolution") == 2
    assert "resolution=1.0" not in result.code


def test_same_kwarg_name_different_value_gets_a_suffixed_flag():
    code = "a = oc.run('x', adata, resolution=1.0)\nb = oc.run('y', adata, resolution=0.5)"
    result = lift_oc_run_literals(code)

    names = {p.name: p for p in result.lifted}
    assert set(names) == {"resolution", "resolution_2"}
    assert names["resolution"].default == 1.0
    assert names["resolution"].flag == "--resolution"
    assert names["resolution_2"].default == 0.5
    assert names["resolution_2"].flag == "--resolution-2"
    assert "args.resolution)" in result.code
    assert "args.resolution_2)" in result.code


def test_non_literal_kwarg_is_skipped_not_lifted():
    code = "n = 5\noc.run('x', adata, k=n)\noc.run('x', adata, k2=f'{n}')"
    result = lift_oc_run_literals(code)

    assert result.lifted == []
    assert result.code == code
    assert len(result.skipped) == 2
    assert all("not a literal" in reason for reason in result.skipped)


def test_list_literal_kwarg_is_skipped_not_lifted():
    """Gene-panel-style list kwargs need nargs-aware flag handling — out of
    scope for this slice (acquisition-plan.md §P2 explicit boundary)."""
    code = "oc.run('x', adata, genes=['CD3D', 'CD8A'])"
    result = lift_oc_run_literals(code)

    assert result.lifted == []
    assert result.code == code
    assert len(result.skipped) == 1
    assert "list literal not liftable" in result.skipped[0]


def test_oc_rebinding_skips_the_whole_lift():
    """If `oc` is ever reassigned, trusting `ast.Name(id='oc')` calls as THE
    facade would risk misattributing a call to a name that no longer means
    what we think — bail out entirely rather than guess."""
    code = "oc = something()\noc.run('x', adata, k=1)"
    result = lift_oc_run_literals(code)

    assert result.code == code
    assert result.lifted == []
    assert any("rebound" in reason for reason in result.skipped)


def test_kwarg_colliding_with_a_reserved_template_flag_is_suffixed():
    """A kwarg literally named `method` must not silently take over the
    template's own static `--method` flag."""
    code = "oc.run('x', adata, method=42)"
    result = lift_oc_run_literals(code)

    assert len(result.lifted) == 1
    assert result.lifted[0].flag == "--method-2"
    assert result.lifted[0].name == "method_2"


def test_skill_and_data_kwargs_are_never_lift_candidates():
    """`skill`/`data` identify WHICH vetted skill to call and what AnnData to
    pass it — never a tunable value, even when passed as keywords."""
    code = "oc.run('x', data=adata, timeout=10)"
    result = lift_oc_run_literals(code)

    names = {p.name for p in result.lifted}
    assert names == {"timeout"}


def test_negative_number_literal_is_lifted_whole():
    code = "oc.run('x', adata, offset=-0.5)"
    result = lift_oc_run_literals(code)

    assert result.lifted[0].default == -0.5
    assert result.lifted[0].type == "float"
    assert "offset=-0.5" not in result.code


def test_bool_literal_is_lifted_with_bool_type():
    code = "oc.run('x', adata, verbose=True)"
    result = lift_oc_run_literals(code)

    assert result.lifted[0].default is True
    assert result.lifted[0].type == "bool"


def test_no_oc_run_calls_returns_code_unchanged():
    code = "adata = adata.copy()\nprint('no facade calls here')"
    result = lift_oc_run_literals(code)

    assert result.code == code
    assert result.lifted == []
    assert result.skipped == []


def test_unparseable_code_is_left_untouched():
    code = "def broken(:\n"
    result = lift_oc_run_literals(code)

    assert result.code == code
    assert result.lifted == []
    assert "does not parse" in result.skipped[0]


def test_custom_oc_name_is_respected():
    """The facade is always bound as `oc` in generated scripts, but the
    function itself doesn't hardcode that — verify the `oc_name` parameter
    actually changes which calls are matched."""
    code = "handle.run('x', adata, min_genes=200)"
    assert lift_oc_run_literals(code).lifted == []
    result = lift_oc_run_literals(code, oc_name="handle")
    assert result.lifted[0].name == "min_genes"


# --- Regression tests for issues an adversarial codex review found ---------


def test_non_ascii_source_does_not_corrupt_the_splice():
    """CPython's ast reports col_offset/end_col_offset in UTF-8 BYTES, not
    characters, for any line containing multi-byte characters. Naively
    treating them as character offsets silently rewrites the WRONG span —
    confirmed: with a preceding 2-character/6-byte CJK string literal on the
    same line, the byte-vs-char drift caused `a`'s value to be skipped
    entirely and `b`'s span to bleed into the unrelated statement `y=3`."""
    code = "p='你好'; x=oc.run('s', adata, a=1,b=2); y=3"
    result = lift_oc_run_literals(code)

    names = {p.name: p.default for p in result.lifted}
    assert names == {"a": 1, "b": 2}
    assert result.code == "p='你好'; x=oc.run('s', adata, a=args.a,b=args.b); y=3"


def test_non_ascii_string_literal_kwarg_is_lifted_correctly():
    code = "res = oc.run('x', adata, label='你好')"
    result = lift_oc_run_literals(code)

    assert result.skipped == []
    assert result.lifted[0].default == "你好"
    assert result.code == "res = oc.run('x', adata, label=args.label)"


def test_repeated_reserved_collision_with_identical_value_shares_one_flag():
    """A kwarg literally named `method` always suffixes (§ reserved-flag
    test above); a SECOND occurrence with the SAME value must reuse that
    same suffixed flag, not mint yet another one (`--method-3`, etc.) each
    time it recurs."""
    code = "a=oc.run('x', adata, method=42)\nb=oc.run('y', adata, method=42)"
    result = lift_oc_run_literals(code)

    assert len(result.lifted) == 1
    assert result.lifted[0].flag == "--method-2"
    assert result.code.count("args.method_2") == 2


def test_repeated_reserved_collision_with_different_values_gets_distinct_flags():
    code = "a=oc.run('x', adata, method=1)\nb=oc.run('y', adata, method=2)"
    result = lift_oc_run_literals(code)

    flags = {p.flag: p.default for p in result.lifted}
    assert flags == {"--method-2": 1, "--method-3": 2}


def test_bool_true_and_int_one_are_not_collapsed_into_one_flag():
    """Python's `==`/hash treat `True == 1` (and `1 == 1.0`) — the collision
    cache key must include the literal's TYPE, not just its value, or a
    `cutoff=True` followed by an unrelated `cutoff=1` would wrongly share
    one flag/default despite meaning different things."""
    code = "a=oc.run('x', adata, cutoff=True)\nb=oc.run('y', adata, cutoff=1)"
    result = lift_oc_run_literals(code)

    by_flag = {p.flag: (p.default, p.type) for p in result.lifted}
    assert by_flag == {"--cutoff": (True, "bool"), "--cutoff-2": (1, "int")}


def test_int_one_and_float_one_are_not_collapsed_into_one_flag():
    code = "a=oc.run('x', adata, cutoff=1)\nb=oc.run('y', adata, cutoff=1.0)"
    result = lift_oc_run_literals(code)

    by_flag = {p.flag: (p.default, p.type) for p in result.lifted}
    assert by_flag == {"--cutoff": (1, "int"), "--cutoff-2": (1.0, "float")}
