"""key_part: deterministic, ArangoDB-safe key fragments."""

from __future__ import annotations

import re

import pytest

from arango_connector.keys import key_part

HASHED = re.compile(r"^h-[0-9a-f]{40}$")


def test_int_and_str_keys_are_plain() -> None:
    assert key_part([42]) == "42"
    assert key_part(["abc-1"]) == "abc-1"


def test_int_and_its_string_give_the_same_key() -> None:
    assert key_part([1]) == key_part(["1"])
    assert key_part([1, 2]) == key_part(["1", "2"])


def test_composite_keys_join_with_colon() -> None:
    assert key_part([1, 2]) == "1:2"
    assert key_part(["a", 3]) == "a:3"


def test_component_with_colon_is_hashed_so_it_cannot_collide() -> None:
    hashed = key_part(["a:b"])
    assert HASHED.match(hashed)
    assert key_part(["a:b"]) == hashed  # deterministic
    assert key_part(["a", "b"]) == "a:b" != hashed


@pytest.mark.parametrize("value", ["x" * 129, "has space", "slash/key", "ünïcode"])
def test_long_or_invalid_values_are_hashed(value: str) -> None:
    assert HASHED.match(key_part([value]))


def test_128_characters_still_plain() -> None:
    assert key_part(["x" * 128]) == "x" * 128


@pytest.mark.parametrize("value", [True, 1.5, None, [1], {"a": 1}, ""])
def test_invalid_values_raise(value: object) -> None:
    with pytest.raises(ValueError):
        key_part([value])


def test_no_values_raise() -> None:
    with pytest.raises(ValueError):
        key_part([])
