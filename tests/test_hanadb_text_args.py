"""The ``text`` argument kind (``section``, ``key``): an empty value is refused.

``LOCATE(x, '')`` is true for every row, so an empty key would silently mean
"no filter" and an empty section would match nothing; ``None`` stays "not given".
"""
from __future__ import annotations

import pytest

from agents.hana.checks import get_check
from agents.hana.checks.base import ARGS, InvalidArgument, resolve_args


@pytest.mark.parametrize("name", ["section", "key"])
@pytest.mark.parametrize("value", ["", " ", "   "])
def test_an_empty_text_argument_is_refused_without_the_value(name, value):
    with pytest.raises(InvalidArgument) as info:
        resolve_args(get_check("iniContents"), **{name: value})
    assert info.value.argument == name
    assert info.value.rule == ARGS[name].rule
    assert "not empty" in info.value.rule
    assert repr(value) not in str(info.value)


@pytest.mark.parametrize("name", ["section", "key"])
def test_none_stays_no_filter_and_a_value_passes(name):
    check = get_check("iniContents")
    assert resolve_args(check, **{name: None})[name] is None
    assert resolve_args(check, **{name: "x y"})[name] == "x y"
