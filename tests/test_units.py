from __future__ import annotations

import pytest

from app.units import UnitError, format_coins, parse_coins


def test_parse_coins_accepts_plain_values() -> None:
    assert parse_coins("1") == 100_000_000
    assert parse_coins("0.00000001") == 1
    assert parse_coins("12.34") == 1_234_000_000
    assert parse_coins(7) == 700_000_000


@pytest.mark.parametrize("text", ["", "abc", "1.123456789", "1.2.3", "1e3"])
def test_parse_coins_rejects_bad_values(text: str) -> None:
    with pytest.raises(UnitError):
        parse_coins(text)


def test_format_coins_keeps_meaningful_digits() -> None:
    assert format_coins(100_000_000) == "1"
    assert format_coins(123_456_789) == "1.2345"
    assert format_coins(1, digits=8) == "0.00000001"
    assert format_coins(0) == "0"
    assert format_coins(123_456_789, digits=2) == "1.23"
