"""Decimal parsing that preserves payload digits."""

from __future__ import annotations

from decimal import Decimal


def as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError("expected a number")
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip()
    if text == "":
        raise ValueError("expected a number")
    return Decimal(text)
