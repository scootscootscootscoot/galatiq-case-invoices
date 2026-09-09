"""Decimal arithmetic at money boundaries; public invoice JSON remains numeric."""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal


def product(quantity: float, price: float) -> float:
    return float(
        (Decimal(str(quantity)) * Decimal(str(price))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def total(values: Iterable[float]) -> float:
    return float(sum((Decimal(str(value)) for value in values), Decimal(0)))


def difference(left: float, right: float) -> float:
    return float(Decimal(str(left)) - Decimal(str(right)))
