"""货币单位：链上金额一律使用整数聪（sat），避免浮点误差。"""
from __future__ import annotations

SAT_PER_COIN = 100_000_000


class UnitError(ValueError):
    """金额文本无法解析为聪。"""


def parse_coins(text: str | int | float) -> int:
    """把 "1.5" 这样的币值解析为整数聪，拒绝超过 8 位小数。"""
    if isinstance(text, int):
        return text * SAT_PER_COIN
    if isinstance(text, float):
        text = repr(text)
    raw = str(text).strip()
    if not raw:
        raise UnitError("金额不能为空")
    negative = raw.startswith("-")
    if negative:
        raw = raw[1:]
    if raw.count(".") > 1:
        raise UnitError(f"金额格式错误：{text}")
    whole, _, fraction = raw.partition(".")
    if not whole and not fraction:
        raise UnitError(f"金额格式错误：{text}")
    if not whole.isdigit() and whole != "":
        raise UnitError(f"金额格式错误：{text}")
    if not fraction.isdigit() and fraction != "":
        raise UnitError(f"金额格式错误：{text}")
    if len(fraction) > 8:
        raise UnitError("最多支持 8 位小数")
    sat = int(whole or "0") * SAT_PER_COIN + int(fraction.ljust(8, "0") or "0")
    return -sat if negative else sat


def format_coins(sat: int, *, digits: int = 4, grouping: bool = False) -> str:
    """把聪格式化成币值文本，默认保留 4 位有效小数。"""
    sign = "-" if sat < 0 else ""
    value = abs(int(sat))
    whole, fraction = divmod(value, SAT_PER_COIN)
    shown = f"{fraction:08d}"[:digits].rstrip("0")
    whole_text = f"{whole:,}" if grouping else str(whole)
    return f"{sign}{whole_text}.{shown}" if shown else f"{sign}{whole_text}"
